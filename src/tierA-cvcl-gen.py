"""
tierA-cvcl-gen.py -- Build the CV/CL batch file from one day's evaluation reports, as input to
batch-resume-gen.py.

Usage: python src/tierA-cvcl-gen.py [report-date YYYY-MM-DD] [output.json]
       (defaults: today's reports; data/batch_jobs.json)

Takes every report for that date scoring 3.5 or higher. Collapses literal same-company /
same-role / different-country reposts to one representative posting: three near-identical
CV/CL packets for one role is low-value volume, and the generator's own dedup gate only
compares against PRIOR output, not within a single run.

Cover-letter bullets are pulled VERBATIM from profile.json (per-project cl_bullets) -- no
invented content, no per-job LLM prose. They vary with the projects each evaluation matched,
so packets differ in real, traceable ways rather than boilerplate.
"""

import glob, json, re, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent

TODAY = date.today().isoformat()
# argv[1] = report-date to harvest (YYYY-MM-DD), argv[2] = output json path
RUN_DATE = sys.argv[1] if len(sys.argv) > 1 else TODAY
REPORTS_GLOB = str(ROOT / "reports" / ("*-%s.md" % RUN_DATE))
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data" / "batch_jobs.json"

PROFILE = json.loads((ROOT / ('profile.json' if (ROOT / 'profile.json').exists()
                              else 'profile.example.json')).read_text(encoding='utf-8'))
PROJECT_BULLETS = {k: v['cl_bullets'] for k, v in PROFILE['projects'].items()}
PROJECT_LABELS = {k: v['label'] for k, v in PROFILE['projects'].items()}
CLOSING_LINE = json.loads((ROOT / ("config.json" if (ROOT / "config.json").exists()
                                   else "config.example.json")).read_text(encoding="utf-8"))["cover_letter"]["closing_line"]

POS = PROFILE['positioning']
SAFE_KEYWORDS = POS['safe_keywords']
# Key Achievements padding when a job matches fewer than 3 projects
DEFAULT_PROJECTS = POS.get('default_projects') or list(PROFILE['projects'])[:3]

def role_core(role):
    r = role.lower()
    r = re.sub(r"\b(senior|sr\.?|principal|staff|lead|director|vp|head of|avp)\b", "", r)
    r = re.sub(r"[^a-z]+", " ", r).strip()
    return r


def load_reports():
    entries = []
    for f in sorted(glob.glob(REPORTS_GLOB)):
        txt = Path(f).read_text(encoding="utf-8")
        m = re.search(r"```yaml\n(.*?)\n```", txt, re.DOTALL)
        if not m:
            continue
        yml = m.group(1)
        def field(name, default=""):
            fm = re.search(rf'^{name}:\s*"?(.*?)"?$', yml, re.MULTILINE)
            return fm.group(1) if fm else default
        score = float(field("score", "0"))
        if score < 3.5:
            continue
        company = field("company")
        role = field("role")
        projects_m = re.search(r"top_projects:\s*\[(.*?)\]", yml)
        projects = re.findall(r"'([^']+)'", projects_m.group(1)) if projects_m else DEFAULT_PROJECTS[:1]
        entries.append({
            "report_file": f, "num": re.match(r"(\d+)-", Path(f).name).group(1),
            "company": company, "role": role, "location": field("location"),
            "url": field("url"), "score": score, "job_id": field("job_id"),
            "projects": projects,
        })
    return entries


def main():
    entries = load_reports()
    # Load the JD cache ONCE -- it can be tens of MB.
    try:
        jd_cache = json.loads((ROOT / "data" / "jd_cache.json").read_text(encoding="utf-8"))
    except Exception:
        jd_cache = {}
    batch = []
    skipped_dups = []
    for e in sorted(entries, key=lambda x: -x["score"]):
        role_clean = e["role"]
        co = (e["company"] or "").strip().lower()
        # Strip EVERY trailing " - Company" / " – Company" / " — Company" / " | Company". A posting
        # title can already end in the company name before the pipeline appends it again, so a
        # single strip left "Role - Co" behind -- and the letter read "Role - Co position at Co".
        while co:
            for sep in (" - ", " – ", " — ", " | "):
                if role_clean.lower().endswith(sep + co):
                    role_clean = role_clean[: -(len(sep) + len(co))].strip()
                    break
            else:
                break

        collapse_key = (e["company"].lower().strip(), role_core(role_clean))
        # Collapse literal same-company/same-role reposts across countries -- keep first (highest score) only
        already = [b for b in batch if (b["_company_lower"], role_core(b["role"])) == collapse_key]
        if already:
            skipped_dups.append((e["company"], e["role"], e["location"], e["url"]))
            continue

        top_projects = e["projects"][:2] if len(e["projects"]) >= 2 else e["projects"]
        bullets = []
        for p in top_projects:
            bullets.extend(PROJECT_BULLETS.get(p, [])[:2 if len(top_projects) > 1 else 3])
        bullets = bullets[:3]
        bullets.append(CLOSING_LINE)

        domain_desc = " and ".join(PROJECT_LABELS.get(p, p) for p in top_projects)
        summary = POS['summary_template'].replace('{domain_desc}', domain_desc)

        jd_cache_text = jd_cache.get(e["job_id"], {}).get("description", "")
        jd_lower = jd_cache_text.lower()
        matched_kw = [k for k in SAFE_KEYWORDS if k.lower() in jd_lower][:7]
        if not matched_kw:
            matched_kw = list(POS['fallback_keywords'])

        title_bar = f"{POS['headline_title']} | {matched_kw[0].title() if matched_kw else 'Product Delivery'} | {POS['title_bar_suffix']}"

        job_entry = {
            "job_id": e["num"], "company": e["company"], "role": role_clean,
            "location": e["location"], "title_bar": title_bar, "summary": summary,
            "projects": top_projects + [p for p in DEFAULT_PROJECTS if p not in top_projects][:3 - len(top_projects)],
            "cl_bullets": bullets, "jd_keywords": matched_kw,
            "jd_keywords_label": "Role-Aligned Delivery",
        }
        job_entry["_company_lower"] = e["company"].lower().strip()
        batch.append(job_entry)

    for b in batch:
        del b["_company_lower"]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(batch)} unique job configs to {OUT}")
    print(f"Skipped {len(skipped_dups)} same-company/same-role reposts (different country/URL only):")
    for c, r, loc, u in skipped_dups:
        print(f"  - {c} / {r} / {loc}")


if __name__ == "__main__":
    main()
