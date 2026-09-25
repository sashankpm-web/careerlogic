"""
full-eval-batch.py -- Full A-G evaluation report for each job in a Tier-A list.

Usage: python src/full-eval-batch.py [tierA-list.tsv] [first-report-number]
       (defaults: data/tierA-list-<today>.tsv; highest existing report number + 1)

Reads the Tier-A TSV from extract-tier-a.py (score, title, location, url; company comes
from the JD cache or a trailing " - Company" in the title), pulls cached JD text from
data/jd_cache.json, matches it against the profile.json project library by keyword, and
writes one report per job to reports/ plus a tracker line to batch/tracker-additions/.

Rule-based matching and JD-signal legitimacy: every fact traces to profile.json or the cached
JD. Compensation (Block D) and live company-hiring signals (Block G) are NOT researched here,
and each report says so explicitly. Deterministic: no LLM calls.
"""

import json, re, sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).parent.parent
JD_CACHE = ROOT / "data" / "jd_cache.json"
REPORTS_DIR = ROOT / "reports"
TRACKER_ADD_DIR = ROOT / "batch" / "tracker-additions"
TODAY = date.today().isoformat()
# Tier-A input TSV: argv[1], else data/tierA-list-<today>.tsv
TIERA_TSV = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / ("tierA-list-%s.tsv" % TODAY)


def _next_report_num():
    """Highest existing report number + 1 (the default first number for this run)."""
    nums = [int(p.name.split("-")[0]) for p in REPORTS_DIR.glob("*.md")
            if p.name.split("-")[0].isdigit()]
    return (max(nums) + 1) if nums else 1


START_NUM = int(sys.argv[2]) if len(sys.argv) > 2 else _next_report_num()

# ---- Project library keyword scoring (profile.json -> projects) ----
PROFILE = json.loads((ROOT / ('profile.json' if (ROOT / 'profile.json').exists()
                              else 'profile.example.json')).read_text(encoding='utf-8'))
PROJECT_KEYWORDS = {k: (v['keywords'], v['weight'], v['display'])
                    for k, v in PROFILE['projects'].items()}
GENERIC_BOOST = (PROFILE['generic_boost']['keywords'], PROFILE['generic_boost']['weight'])

EVAL = PROFILE['evaluation']
ARCHETYPE_LABELS = EVAL['archetype_labels']
ARCHETYPE_KEYWORDS = EVAL['archetype_keywords']


def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s.lower())
    return re.sub(r"[\s_]+", "-", s).strip("-")


def project_matches(jd_text):
    jd = jd_text.lower()
    scores = {}
    for pid, (kws, weight, _) in PROJECT_KEYWORDS.items():
        hits = sum(1 for k in kws if k in jd)
        scores[pid] = hits * weight
    gkws, gweight = GENERIC_BOOST
    ghits = sum(1 for k in gkws if k in jd)
    for pid in scores:
        scores[pid] += ghits * gweight
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    top3 = [pid for pid, sc in ranked[:3] if sc > 0] or [ranked[0][0]]
    # Pad to 3, or to however many projects exist -- with a smaller library a bare
    # `< 3` never terminates.
    while len(top3) < min(3, len(ranked)):
        for pid, _ in ranked:
            if pid not in top3:
                top3.append(pid)
                break
    return top3[:3], scores


def detect_archetype(jd_text, title):
    combined = f"{title} {jd_text}".lower()
    best, best_hits = EVAL['default_archetype'], 0
    for name, kws in ARCHETYPE_KEYWORDS.items():
        hits = sum(1 for k in kws if k in combined)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def extract_job_id(url):
    m = re.search(r"/jobs/view/(\d+)", url)
    return m.group(1) if m else None


def load_jobs():
    cache = json.loads(JD_CACHE.read_text(encoding="utf-8"))
    jobs = []
    for line in TIERA_TSV.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        score_str, title_raw, loc, url = parts[0], parts[1], parts[2], parts[3]
        jid = extract_job_id(url)
        entry = cache.get(jid, {})
        jd_text = entry.get("description", "")
        title = title_raw
        company = entry.get("company", "")
        if not company:
            for sep in (" \u2013 ", " - "):   # "Title – Company" or "Title - Company"
                if sep in title_raw:
                    title, company = title_raw.rsplit(sep, 1)
                    break
        jobs.append({
            "quick_score": float(score_str), "title": title.strip(), "company": company.strip(),
            "location": loc.strip(), "url": url.strip(), "job_id": jid, "jd_text": jd_text,
        })
    jobs.sort(key=lambda j: -j["quick_score"])
    return jobs


def gap_analysis(jd_text, archetype):
    jd = jd_text.lower()
    gaps = []
    gap_checks = EVAL['gap_checks']
    for label, patterns, note in gap_checks:
        if any(p in jd for p in patterns):
            gaps.append((label, note))
    if not gaps:
        gaps.append(("none flagged", "No hard-blocker gaps detected from cached JD text against the profile."))
    return gaps


def legitimacy_from_text(jd_text, location):
    jd_text = jd_text or ""
    signals = []
    length = len(jd_text)
    if length > 1500:
        signals.append(("Description depth", "Detailed JD (>1500 chars), names specific tools/scope", "Positive"))
    elif length > 500:
        signals.append(("Description depth", "Moderate JD length", "Neutral"))
    else:
        signals.append(("Description depth", "Short/thin JD text in cache", "Concerning"))
    if re.search(r"\$[\d,]+|\bUSD\b|salary range|compensation range", jd_text, re.IGNORECASE):
        signals.append(("Compensation disclosed", "Salary/comp range mentioned in JD", "Positive"))
    if re.search(r"years? of experience.{0,40}(entry|junior)", jd_text, re.IGNORECASE):
        signals.append(("Internal consistency", "Possible seniority/requirement mismatch", "Concerning"))
    tier = "Proceed with Caution"
    if sum(1 for _, _, w in signals if w == "Positive") >= 2 and not any(w == "Concerning" for _, _, w in signals):
        tier = "High Confidence"
    if sum(1 for _, _, w in signals if w == "Concerning") >= 2:
        tier = "Suspicious"
    return tier, signals


def score_full(quick_score, jd_text, gaps):
    score = quick_score
    if any(g[0] == "none flagged" for g in gaps):
        score += 0.05
    else:
        score -= 0.05 * (len(gaps))
    return round(max(0.0, min(score, 5.0)), 2)


def write_report(num, job, top_projects, project_meta, archetype, gaps, legitimacy_tier, legitimacy_signals, full_score):
    slug = slugify(job["company"] or "company")
    fname = f"{num:04d}-{slug}-{TODAY}.md"
    path = REPORTS_DIR / fname
    arch_label = ARCHETYPE_LABELS.get(archetype, archetype)

    proj_lines = []
    for pid in top_projects:
        _, _, label = PROJECT_KEYWORDS[pid]
        proj_lines.append(f"- **{pid}** — {label}")

    star_rows = []
    for pid in top_projects[:3]:
        _, _, label = PROJECT_KEYWORDS[pid]
        star_rows.append(
            f"| Delivery under regulatory/scope constraint | {label} | Backlog/roadmap ownership under a hard deadline or multi-system dependency | Deliver measurable outcome without disruption | Owned backlog, coordinated cross-functional stakeholders, sequenced delivery | See profile.json metrics for {pid} | Reusable pattern for prioritization-under-constraint questions |"
        )

    body = f"""# Evaluation: {job['company'] or 'Unknown Company'} — {job['title']}

**Date:** {TODAY}
**URL:** {job['url']}
**Archetype:** {arch_label}
**Score:** {full_score}/5
**Legitimacy:** {legitimacy_tier}
**PDF:** pending

---

## A) Role Summary

| Field | Value |
|---|---|
| Archetype | {arch_label} |
| Domain | {EVAL['domain_scope']} |
| Function | Product ownership — backlog, roadmap, delivery |
| Seniority | {"Senior/Lead" if re.search(r"senior|lead|principal|staff|director|vp|head of", job['title'], re.IGNORECASE) else "Mid-Senior"} |
| Location | {job['location']} |
| Team size | Not specified in cached JD |
| TL;DR | {job['title']} at {job['company'] or 'this company'} — {arch_label.lower()} role matching candidate's {EVAL['background_phrase']}. |

## B) Match with CV

Top matching projects from the profile.json project library, scored against cached JD keywords:

{chr(10).join(proj_lines)}

**Gaps:**

{chr(10).join(f"- **{label}**: {note}" for label, note in gaps)}

## C) Level and Strategy

1. **Level detected:** {job['title']} maps to candidate's natural {EVAL['level_phrase']}.
2. **Sell senior without lying:** Lead with quantified outcomes ({EVAL['headline_outcomes']}) rather than generic scrum-ceremony language.
3. **If downleveled:** Accept if compensation is fair for a mid-level scope; negotiate a 6-month review tied to backlog-ownership expansion.

## D) Comp and Demand

*Not independently WebSearch-verified for this batch run (batch-mode disclosure — high job-volume batch; comp research reserved for jobs clearing the CV/CL generation threshold). Do not treat as a market-verified figure.*

## E) Customization Plan

| # | Section | Current status | Proposed change | Why |
|---|---------|-----------------|-------------------|-----|
| 1 | Title bar | Generic PO/PM dual lead | Lead with `{"Product Manager" if "product manager" in job['title'].lower() else "Senior Product Owner"}` per lead-title matching rule | Matches JD's own title composition |
| 2 | Summary | Generic | Anchor to top-matched project ({top_projects[0]}) domain | ATS + recruiter skim relevance |
| 3 | Key Achievements | Default 3-project set | Swap to {', '.join(top_projects)} | Highest keyword overlap with this JD |

Top 5 CV/LinkedIn changes: (1) title-bar lead-title match, (2) summary domain anchor, (3) Key Achievements project swap, (4) Core Competencies line matching JD's Class-A terms, (5) keyword mirroring in current-role bullets where truthful.

## F) Interview Plan

| # | JD Requirement | STAR+R Story | S | T | A | R | Reflection |
|---|-----------------|-----------------|---|---|---|---|------------|
{chr(10).join(star_rows)}

**Recommended case study:** {PROJECT_KEYWORDS[top_projects[0]][2]}.

## G) Posting Legitimacy

**Assessment:** {legitimacy_tier}

| Signal | Finding | Weight |
|---|---|---|
{chr(10).join(f"| {s[0]} | {s[1]} | {s[2]} |" for s in legitimacy_signals)}

**Context Notes:** Company hiring-signal WebSearch (layoffs/freeze) and reposting-detection not run for this batch item — see the batch-mode disclosure in Block D.

---

## Machine Summary

```yaml
job_id: "{job['job_id'] or ''}"
company: "{job['company']}"
role: "{job['title']}"
location: "{job['location']}"
url: "{job['url']}"
score: {full_score}
archetype: "{archetype}"
top_projects: {top_projects}
legitimacy: "{legitimacy_tier}"
quick_screen_score: {job['quick_score']}
eval_date: "{TODAY}"
comp_verified: false
```
"""
    path.write_text(body, encoding="utf-8")
    return fname


def write_tracker_tsv(num, job, full_score):
    slug = slugify(job["company"] or "company")
    fname = f"{num:04d}-{slug}.tsv"
    path = TRACKER_ADD_DIR / fname
    report_link = f"[{num}](reports/{num:04d}-{slug}-{TODAY}.md)"
    line = "\t".join([
        str(num), TODAY, job["company"] or "Unknown", job["title"], "Evaluated",
        f"{full_score}/5", "\u274c", report_link,
        f"Full A-G eval, quick-screen {job['quick_score']}/5, batch {TODAY}",
    ])
    path.write_text(line + "\n", encoding="utf-8")


def main():
    jobs = load_jobs()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    TRACKER_ADD_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for i, job in enumerate(jobs):
        num = START_NUM + i
        top_projects, project_scores = project_matches(job["jd_text"])
        archetype = detect_archetype(job["jd_text"], job["title"])
        gaps = gap_analysis(job["jd_text"], archetype)
        legit_tier, legit_signals = legitimacy_from_text(job["jd_text"], job["location"])
        full_score = score_full(job["quick_score"], job["jd_text"], gaps)
        fname = write_report(num, job, top_projects, PROJECT_KEYWORDS, archetype, gaps, legit_tier, legit_signals, full_score)
        write_tracker_tsv(num, job, full_score)
        results.append((num, job["company"], job["title"], full_score, fname))
        print(f"{num}\t{full_score}\t{job['company']}\t{job['title']}\t{fname}")

    print(f"\nTotal: {len(results)} reports written, numbers {START_NUM}-{START_NUM+len(results)-1}")


if __name__ == "__main__":
    main()
