# CareerLogic

A configurable, rule-based pipeline for running a job search like a product:
**scan → screen → evaluate → generate → render**.

CareerLogic screens every posting against a structured candidate profile with explicit
eligibility rules, writes a structured evaluation for the few roles that clear the bar, and
generates a tailored CV and cover letter for each. Every decision is a transparent rule you can
trace. There is no LLM anywhere in the decision path.

---

## Why it exists

A wide search surfaces far more postings than anyone can read carefully. The hard part isn't
finding jobs; it's deciding which ones deserve a tailored application, and then producing that
application without quality drifting across hundreds of runs.

CareerLogic's answer is to be selective by design. Across **7,547 postings** screened with their
full job descriptions, rules set aside **85%** before any application work began:

| Share | Outcome |
|---|---|
| 26% | Off-target title, excluded company or wrong location |
| 7% | Failed an eligibility or language rule |
| 52% | Scored below the bar |
| **15%** | **Cleared it — about 1 in 7 — and earned a full evaluation** |

It runs alongside [career-ops](https://github.com/santifer/career-ops) (see *Credit*), which
supplies the portal scanner and an interactive evaluation workflow.

## Design principles

**Rules, not guesses.** Every verdict comes from an explicit rule (see *Screening rules*). The
same input always gives the same result, and a surprising score can be traced to the rule that
produced it.

**One set of rules.** Rescoring imports the first-pass screener's functions instead of keeping its
own copy. An earlier duplicate drifted and silently skipped eligibility rules added later.

**Never fabricate.** Every CV and cover-letter sentence comes from `profile.json`, `config.json` or
the batch entry. The generator selects and orders existing material; it never writes new claims.

**Guardrails over trust.** A dedup gate stops a posting from being regenerated under a different
spelling or job ID. A domain guard warns when a draft claims experience the profile lists as a gap.
The renderer flags any CV longer than two pages. Each exists because the failure it prevents
happened first.

**Configuration, not code.** Everything about the candidate — identity and contact details, work
authorisation, home location, languages, career history, education, certifications, strengths,
gaps, exclusions and target markets — lives in `config.json` and `profile.json`. The source holds
the screening mechanics only. Role-family matching is tuned for product roles (Product Owner /
Product Manager / Business Analyst).

## Screening rules

Skip reasons in the pipeline cite these rule numbers where one applies.

| Rule | Checks | Result |
|---|---|---|
| 1 | Title belongs to the configured role family (literal or semantic match) | Skip, or flag an ambiguous title for a full-JD check |
| 2 | Excluded title patterns, excluded companies, local-language requirement in the title | Skip |
| 3 | Hard skill gaps and named payment-rail gaps in the job description | Score penalty (not a skip) |
| 4 | Job description written in a local language you don't speak (configured markets) | Skip |
| 5 | Onsite in your home country but outside your allowed cities | Skip |
| 6 | For roles **abroad**: must already live there, citizenship or security clearance required, or visa sponsorship refused. Never applied to home-country roles; sponsorship refusal is also waived for US roles when you're US work-authorised | Skip |
| 7 | Nationality-restricted title ("nationals only") | Skip |

## Pipeline

| Stage | Script | Reads → writes |
|---|---|---|
| Scan *(optional)* | `src/manual-scan.py` | job-board aggregation through a third-party data provider (Apify) → `data/pipeline.md`, `data/jd_cache.json` |
| Screen | `src/pipeline-quickscreen.py` | pending `pipeline.md` entries → EVAL / SKIP / FLAG verdicts, `data/quick-screen-<date>.md` |
| Rescore *(optional)* | `src/pipeline-jd-rescore.py` | title-only / flagged entries + a manual JD cache → same rules as Screen |
| Select | `src/extract-tier-a.py` | that day's entries scoring ≥ 3.5 → `data/tierA-list-<date>.tsv` |
| Evaluate | `src/full-eval-batch.py` | Tier-A list → `reports/*.md` (A–G + machine summary), `batch/tracker-additions/` |
| Batch | `src/tierA-cvcl-gen.py` | reports scoring ≥ 3.5 → `data/batch_jobs.json` |
| Generate | `src/batch-resume-gen.py` | `data/batch_jobs.json` → CV + cover-letter HTML in `output/`, plus a manifest |
| Render | `src/render-pdfs.mjs` | manifest → PDFs, warning on any CV over two pages |

The scan stage is optional: every later stage works just as well on a `data/pipeline.md` and
`data/jd_cache.json` you fill some other way. It deliberately does not store the recruiter
contact details some listings carry. Each stage's default output is the next stage's default
input, so a day's run is:

```bash
python src/manual-scan.py            # optional; add --skip-stage2 outside a career-ops checkout
python src/pipeline-quickscreen.py
python src/extract-tier-a.py
python src/full-eval-batch.py
python src/tierA-cvcl-gen.py
python src/batch-resume-gen.py
node src/render-pdfs.mjs
```

## Setup

Requires Python 3.10+ and Node 20+.

```bash
cp config.example.json config.json      # identity, screening rules, target markets
cp profile.example.json profile.json    # projects, experience, eligibility, scoring
pip install -r requirements.txt         # only the optional scan stage needs it
npm install && npx playwright install chromium    # only the render stage needs it
export APIFY_API_TOKEN=...              # only the optional scan stage needs it
```

The scan stage also needs `scan.apify_actor` in `config.json`: the Apify actor to call. The
expected input and output fields are listed at the top of `src/manual-scan.py`.

Both `.example.json` files are complete, working samples for a fictional candidate, and every
setting in them is used. `config.json`, `profile.json`, `data/`, `reports/` and `output/` are
never tracked by git.

## How it was verified

These scripts were extracted from a live pipeline and made configurable. Each one was checked
against the author's live version of the same script, on the author's own (unpublished) data,
rather than assumed equivalent:

- **Screener:** identical verdict, reason and score on 15,110 cases (7,555 cached job
  descriptions, each on both the with-JD and title-only paths).
- **Generator:** byte-identical CV, cover letter, eligibility line and filenames across 16 job
  shapes covering every eligibility branch.
- **Evaluation:** 308 of 308 report and tracker files identical, apart from six deliberate
  wording changes that remove references to files outside this repository.
- **Tier-A selection and batch building:** identical on a real day's data (153 and 138 entries).
- **End to end:** 27 checks from a fresh clone using only the example files. They exercise every
  screening rule and confirm that no real-candidate data appears in any generated file.

## A note on the ATS finding

`batch-resume-gen.py` renders the current employer as a single blended experience block rather
than one entry per sub-role. That is deliberate, and it was learned the hard way. Splitting one
employer into three adjacent entries caused a target ATS's résumé parser to populate **zero**
experience entries instead of three imperfect ones. The parser appears to key on company name for
overlap detection, and it abandoned the section rather than degrading. The source comment records
this so the "improvement" doesn't get reintroduced.

## Credit

CareerLogic is built to run alongside [career-ops](https://github.com/santifer/career-ops), an
open-source job-search evaluation framework by Santiago Fernández de Valderrama (santifer),
MIT licensed.

The Python sources are original and import nothing from career-ops. `src/render-pdfs.mjs` adapts
two functions from career-ops's `generate-pdf.mjs` and is distributed under its MIT licence
(`licenses/career-ops-LICENSE.txt`). The optional scan stage's portal pass calls career-ops's
`scan.mjs`, and the pipeline uses the on-disk layout career-ops establishes. See
[NOTICE](NOTICE) for the details.

## Author

Built by Sashank Vemuri — [LinkedIn](https://www.linkedin.com/in/sashank-vemuri).

## License

MIT — see [LICENSE](LICENSE). Portions of `src/render-pdfs.mjs` are adapted from career-ops,
MIT — see [licenses/career-ops-LICENSE.txt](licenses/career-ops-LICENSE.txt).
