"""
pipeline-jd-rescore.py -- Rescore pipeline.md entries that were quick-screened title-only
(no cached JD) or FLAGged, once real JD text has been fetched for them into a manual
JD cache (url -> {"jd": ..., "hard_skip": ...}).

Screening and scoring are NOT reimplemented here: title_screen, the eligibility gates
(sponsorship / language / residency / clearance) and score_job are imported from
pipeline-quickscreen.py, so a rescored job is judged by exactly the same current rules
as a first-pass one. (An earlier version kept private copies of those functions; they
drifted and silently skipped gates added later.)

Usage:  python src/pipeline-jd-rescore.py [path/to/manual_jd_cache.json]
"""

import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
PIPELINE = ROOT / "data" / "pipeline.md"
MANUAL_CACHE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "jd_cache_manual.json"
TODAY = date.today().isoformat()
OUTPUT = ROOT / "data" / f"jd-rescore-{TODAY}.md"

# ---- single source of truth: the first-pass screener's current rules ----
_spec = importlib.util.spec_from_file_location("pipeline_quickscreen", Path(__file__).parent / "pipeline-quickscreen.py")
_qs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_qs)
title_screen = _qs.title_screen
sponsorship_and_language_check = _qs.sponsorship_and_language_check
score_job = _qs.score_job


LINE_RE_NEW = re.compile(
    r"^(?P<prefix>\s*-\s*)\[(?P<box>[x!-])\]\s*\[(?P<title_company>.+?)\]\((?P<url>https?://[^)]+)\)\s*\|\s*(?P<location>[^|]+)\|(?P<rest>.*)$"
)
LINE_RE_OLD = re.compile(
    r"^(?P<prefix>\s*-\s*)\[(?P<box>[x!-])\]\s*(?P<url>https?://\S+)\s*\|\s*(?P<company>[^|]+)\|\s*(?P<title>[^|]+?)\s*\|(?P<rest>.*)$"
)


def parse_line(line):
    m = LINE_RE_NEW.match(line)
    if m:
        title_company = m.group("title_company")
        if "–" in title_company:
            parts = title_company.rsplit("–", 1)
            title, company = parts[0].strip(), parts[1].strip()
        elif " - " in title_company:
            parts = title_company.rsplit(" - ", 1)
            title, company = parts[0].strip(), parts[1].strip()
        else:
            title, company = title_company.strip(), ""
        return {
            "title": title, "company": company, "location": m.group("location").strip(),
            "url": m.group("url").strip(), "rest": m.group("rest"), "prefix": m.group("prefix"),
            "format": "new",
        }
    m = LINE_RE_OLD.match(line)
    if m:
        return {
            "title": m.group("title").strip(), "company": m.group("company").strip(),
            "location": "", "url": m.group("url").strip(), "rest": m.group("rest"),
            "prefix": m.group("prefix"), "format": "old",
        }
    return None


def main():
    with open(MANUAL_CACHE, "r", encoding="utf-8") as f:
        manual_cache = json.load(f)

    lines = PIPELINE.read_text(encoding="utf-8").split("\n")

    stats = {"total_candidates": 0, "rescored": 0, "hard_skip": 0, "now_skip": 0,
              "now_flag_resolved_skip": 0, "now_flag_resolved_score": 0,
              "tier_a": 0, "tier_b": 0, "tier_c": 0, "no_cache_match": 0}
    changes = []

    for idx, line in enumerate(lines):
        if "title-only(no cached JD)" not in line and "FLAG:" not in line:
            continue
        if "(quick-screen" not in line:
            continue
        parsed = parse_line(line)
        if not parsed:
            continue

        entry = manual_cache.get(parsed["url"])
        if entry is None:
            stats["no_cache_match"] += 1
            continue

        stats["total_candidates"] += 1
        was_flag = "FLAG:" in line

        hard_skip = entry.get("hard_skip")
        jd_text = entry.get("jd", "")

        if hard_skip:
            new_reason = f"SKIP: {hard_skip}"
            new_box = "-"
            stats["hard_skip"] += 1
            if was_flag:
                stats["now_flag_resolved_skip"] += 1
        else:
            has_jd = True
            outcome, reason = title_screen(parsed["title"], parsed["location"], jd_text, has_jd, parsed["company"])
            if outcome == "SKIP":
                new_reason = reason
                new_box = "-"
                stats["now_skip"] += 1
                if was_flag:
                    stats["now_flag_resolved_skip"] += 1
            else:
                gate_skip = sponsorship_and_language_check(parsed["location"], jd_text)
                if gate_skip:
                    new_reason = gate_skip
                    new_box = "-"
                    stats["now_skip"] += 1
                    if was_flag:
                        stats["now_flag_resolved_skip"] += 1
                else:
                    score, reasons = score_job(parsed["title"], parsed["company"], parsed["location"], jd_text, has_jd)
                    if score >= 3.5:
                        tier = "A"
                        stats["tier_a"] += 1
                    elif score >= 3.0:
                        tier = "B"
                        stats["tier_b"] += 1
                    else:
                        tier = "C"
                        stats["tier_c"] += 1
                    new_reason = f"EVAL: {score}/5 [{reasons}]"
                    new_box = "x"
                    stats["rescored"] += 1
                    if was_flag:
                        stats["now_flag_resolved_score"] += 1

        # Rebuild the line: keep everything up to and including location's pipe,
        # then keep the "via:"/date portion of rest up to the last " | " segment,
        # replacing only the trailing EVAL/SKIP/FLAG annotation.
        rest = parsed["rest"]
        # rest looks like: " Date | via: X | ... | EVAL:.../SKIP:.../FLAG:... (quick-screen...) [...]"
        # Strip from the last occurrence of a recognizable tag onward.
        cut_pattern = re.compile(r"\s*(?:\|\s*)?(EVAL:|SKIP:|FLAG:).*$", re.DOTALL)
        rest_trimmed = cut_pattern.sub("", rest)

        if parsed["format"] == "new":
            new_line = f"{parsed['prefix']}[{new_box}] [{parsed['title']} – {parsed['company']}]({parsed['url']}) | {parsed['location']} |{rest_trimmed} | {new_reason} (jd-rescore-{TODAY})"
        else:
            new_line = f"{parsed['prefix']}[{new_box}] {parsed['url']} | {parsed['company']} | {parsed['title']} |{rest_trimmed} | {new_reason} (jd-rescore-{TODAY})"
        lines[idx] = new_line
        changes.append((parsed["title"], parsed["company"], parsed["location"], new_box, new_reason))

    PIPELINE.write_text("\n".join(lines), encoding="utf-8")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(f"# JD Rescore -- {TODAY}\n\n")
        f.write("Rescored every pipeline.md entry previously marked `title-only(no cached JD)` "
                f"or `FLAG:` using real JD text from {MANUAL_CACHE.name}.\n\n")
        f.write("## Funnel\n\n")
        for k, v in stats.items():
            f.write(f"- {k}: **{v}**\n")
        f.write("\n## Changes\n\n| Box | Title / Company | Location | Result |\n|---|---|---|---|\n")
        for title, company, location, box, reason in changes:
            f.write(f"| [{box}] | {title} – {company} | {location[:30]} | {reason[:100]} |\n")

    print(f"Candidates found: {stats['total_candidates']} (no cache match: {stats['no_cache_match']})")
    print(f"Hard-skip (title/JD mismatch or dead link): {stats['hard_skip']}")
    print(f"Now skip (title/sponsor/lang gate on real JD): {stats['now_skip']}")
    print(f"Rescored to tier: A={stats['tier_a']} B={stats['tier_b']} C={stats['tier_c']}")
    print(f"Previously-FLAGged, now resolved: skip={stats['now_flag_resolved_skip']} scored={stats['now_flag_resolved_score']}")
    print(f"Report: {OUTPUT}")


if __name__ == "__main__":
    main()
