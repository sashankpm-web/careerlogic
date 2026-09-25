"""
extract-tier-a.py -- Pull one day's Tier-A candidates out of data/pipeline.md into the TSV
that full-eval-batch.py reads.

Usage: python src/extract-tier-a.py [quick-screen-date YYYY-MM-DD] [min-score]
       (defaults: today; 3.5)

Selects [x] entries quick-screened on that date with an EVAL score >= min-score, sorts them
by score, and writes data/tierA-list-<date>.tsv as tab-separated: score, title, location, url.
"""

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
PIPELINE = ROOT / "data" / "pipeline.md"
RUN_DATE = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
MIN_SCORE = float(sys.argv[2]) if len(sys.argv) > 2 else 3.5
OUT = ROOT / "data" / f"tierA-list-{RUN_DATE}.tsv"

LINE_RE = re.compile(
    r"^\s*-\s*\[x\]\s*\[(?P<title_company>.+?)\]\((?P<url>https?://[^)]+)\)\s*\|\s*(?P<location>[^|]+)\|"
    r".*?EVAL:\s*(?P<score>[\d.]+)/5\s*\(quick-screen " + re.escape(RUN_DATE) + r"\)"
)


def main():
    rows = []
    for line in PIPELINE.read_text(encoding="utf-8").splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        score = float(m.group("score"))
        if score < MIN_SCORE:
            continue
        rows.append((score, m.group("title_company").strip(), m.group("location").strip(), m.group("url").strip()))

    rows.sort(key=lambda r: -r[0])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for score, title_company, loc, url in rows:
            f.write(f"{score}\t{title_company}\t{loc}\t{url}\n")

    print(f"Wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
