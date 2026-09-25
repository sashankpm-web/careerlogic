#!/usr/bin/env node
/**
 * render-pdfs.mjs -- Render the CVs and cover letters from a batch-resume-gen.py run to PDF.
 *
 * Usage: node src/render-pdfs.mjs [YYYY-MM-DD]   (default: today)
 * Reads output/batch-manifest-<date>.json, prints each HTML file to an A4 PDF next to it with
 * one shared headless Chromium, normalises typographic characters for ATS parsers, and warns
 * about any resume longer than 2 pages. Requires: npm install && npx playwright install chromium
 *
 * Attribution: normalizeTextForATS() and convert() are adapted from generate-pdf.mjs in career-ops
 * (https://github.com/santifer/career-ops), Copyright (c) 2026 Santiago Fernandez de Valderrama,
 * MIT licensed -- see licenses/career-ops-LICENSE.txt.
 */
import { chromium } from 'playwright';
import { resolve, dirname, join } from 'path';
import { readFile, writeFile } from 'fs/promises';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');
const RESUME_DIR = join(ROOT, 'output', 'resumes');
const CL_DIR = join(ROOT, 'output', 'cover-letters');
const fontsDir = resolve(ROOT, 'fonts');

function normalizeTextForATS(html) {
  const masks = [];
  const masked = html.replace(/<(style|script)\b[^>]*>[\s\S]*?<\/\1>/gi, (m) => { masks.push(m); return `\u0000MASK${masks.length - 1}\u0000`; });
  let out = '', i = 0;
  while (i < masked.length) {
    const lt = masked.indexOf('<', i);
    if (lt === -1) { out += sanitize(masked.slice(i)); break; }
    out += sanitize(masked.slice(i, lt));
    const gt = masked.indexOf('>', lt);
    if (gt === -1) { out += masked.slice(lt); break; }
    out += masked.slice(lt, gt + 1);
    i = gt + 1;
  }
  return out.replace(/\u0000MASK(\d+)\u0000/g, (_, n) => masks[Number(n)]);
  function sanitize(t) {
    if (!t) return t;
    return t.replace(/—/g, '-').replace(/–/g, '-')
      .replace(/[“”„‟]/g, '"').replace(/[‘’‚‛]/g, "'")
      .replace(/…/g, '...').replace(/[\u200B\u200C\u200D\u2060\uFEFF]/g, '').replace(/\u00A0/g, ' ');
  }
}

async function convert(browser, inputPath, outputPath) {
  let html = await readFile(inputPath, 'utf-8');
  html = html.replace(/url\(['"]?\.\/fonts\//g, `url('file://${fontsDir}/`);
  html = html.replace(/file:\/\/([^'")]+)\.(woff2?|ttf|otf)['"]?\)/g, `file://$1.$2')`);
  html = normalizeTextForATS(html);
  const page = await browser.newPage();
  try {
    await page.setContent(html, { waitUntil: 'networkidle', baseURL: `file://${dirname(inputPath)}/` });
    await page.evaluate(() => document.fonts.ready);
    const pdfBuffer = await page.pdf({
      format: 'a4', printBackground: true,
      margin: { top: '0.6in', right: '0.6in', bottom: '0.6in', left: '0.6in' },
      preferCSSPageSize: false,
    });
    await writeFile(outputPath, pdfBuffer);
    const pdfString = pdfBuffer.toString('latin1');
    const pageCount = (pdfString.match(/\/Type\s*\/Page[^s]/g) || []).length;
    return pageCount;
  } finally {
    await page.close();
  }
}

// Local calendar date (YYYY-MM-DD). Must match Python's date.today(), which names the
// manifest -- toISOString() is UTC and disagrees for part of every day east/west of UTC.
function localDate(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function main() {
  const manifestPath = join(ROOT, 'output', `batch-manifest-${process.argv[2] || localDate()}.json`);
  const manifest = JSON.parse(await readFile(manifestPath, 'utf-8'));
  const browser = await chromium.launch({ headless: true });
  const overPage = [];
  let count = 0;
  try {
    for (const job of manifest) {
      for (const [htmlKey, pdfKey, dir] of [['cv_html', 'cv_pdf', RESUME_DIR], ['cl_html', 'cl_pdf', CL_DIR]]) {
        if (!job[htmlKey] || !job[pdfKey]) continue;
        const inputPath = join(dir, job[htmlKey]);
        const outputPath = join(dir, job[pdfKey]);
        try {
          const pages = await convert(browser, inputPath, outputPath);
          count++;
          if (htmlKey === 'cv_html' && pages > 2) {
            overPage.push({ job: job.job_id, company: job.company, pages });
          }
        } catch (e) {
          console.error(`FAIL [${job.job_id}] ${htmlKey}: ${e.message}`);
        }
      }
    }
  } finally {
    await browser.close();
  }
  console.log(`\nConverted ${count} PDFs.`);
  if (overPage.length) {
    console.log(`\n⚠️  ${overPage.length} resumes exceed 2 pages:`);
    for (const o of overPage) console.log(`  #${o.job} ${o.company}: ${o.pages} pages`);
  } else {
    console.log('All resumes within 2-page limit.');
  }
}

main();
