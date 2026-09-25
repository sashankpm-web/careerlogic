"""
manual-scan.py -- Optional, on-demand job-board aggregation through a third-party data
provider (Apify), plus a free portal pass.

Never scheduled: run it when you want fresh postings. Every later stage also works on a
data/pipeline.md and data/jd_cache.json you fill some other way, so this stage is optional.

  python src/manual-scan.py              normal run, last-24h window
  python src/manual-scan.py --catchup    catch-up run, last-7-days window

Optional flags:
  --markets=Name1,Name2   limit Stage 1 to these config.json markets (case-insensitive)
  --skip-stage2           skip the portal pass (Stage 2)

Stage 1: an Apify job-listing actor, one call per market, with a live check of your
         remaining monthly Apify budget before spending. Recruiter contact details that some
         listings carry are deliberately NOT stored.
Stage 2: `node scan.mjs` -- the career-ops portal scanner (Greenhouse/Ashby/Lever and
         others). Requires a career-ops checkout at the repository root; free.

Requires: pip install -r requirements.txt, APIFY_API_TOKEN in the environment, and the
actor to call in config.json -> scan -> apify_actor.

Outputs:
  data/pipeline.md     new job URLs appended as a markdown list
  data/seen_jobs.json  dedup registry of all seen job IDs/URLs
  data/jd_cache.json   full JD text cache, keyed by job_id
"""

import os
import sys
import json
import time
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from apify_client import ApifyClient

# --- CONFIG -------------------------------------------------------------------

APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
PIPELINE_FILE = DATA_DIR / "pipeline.md"
SEEN_JOBS_FILE = DATA_DIR / "seen_jobs.json"
JD_CACHE_FILE = DATA_DIR / "jd_cache.json"

# Abort the run if less than this would remain in your monthly Apify cap afterward.
BUDGET_SAFETY_MARGIN_USD = 0.30

# --- MARKETS --------------------------------------------------------------------
# One Apify call per market, from config.json -> markets. Each entry:
#   name, bucket ("A" home market / "B" relocation -- a label only), location,
#   jobs_titles (bundled into a single call), jobs_entries (max results for that call).
# experience=Mid-Senior + employment_type=Full-time filter server-side, so
# junior/intern/contract noise isn't scraped-then-billed-then-discarded.


def _load_config():
    for name in ("config.json", "config.example.json"):
        path = ROOT_DIR / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise SystemExit("careerlogic: no config.json or config.example.json found at " + str(ROOT_DIR))


MARKET_QUERIES = _load_config()["markets"]

_SCAN = _load_config().get("scan", {})
# The Apify actor to call (config.json -> scan -> apify_actor). It must accept the input built in
# scrape_market() (jobs_titles, location, jobs_entries, experience, employment_type,
# job_post_time) and return job_url, job_title, company_name, location, job_id,
# job_description, seniority_level and search_keyword.
ACTOR_ID = _SCAN.get("apify_actor", "")
# The actor's price per result (config.json -> scan -> cost_per_result_usd). Prices change --
# re-check the actor's pricing page before trusting this for budget math.
COST_PER_RESULT_USD = float(_SCAN.get("cost_per_result_usd", 0))


# --- SPONSORSHIP SIGNAL (advisory only) ----
# Keyword presence in the JD text. NOT a filter -- never used to skip/exclude a job.
SPONSORSHIP_SIGNALS = ["visa sponsorship", "sponsorship available",
                       "relocation assistance", "relocation support",
                       "work permit", "immigration support"]

# --- EXCLUSION FILTERS --------------------------------------------------------
# Local safety net on top of the server-side experience/employment_type filters
# above: titles matching these are never added (config.json -> screening ->
# scan_exclude_title_patterns).

EXCLUDE_TITLE_PATTERNS = _load_config()["screening"].get("scan_exclude_title_patterns", [])

# --- HELPERS ------------------------------------------------------------------

def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("seen", []))
    return set()


def save_seen_jobs(seen: set) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(list(seen)), "last_updated": datetime.now().isoformat()}, f, indent=2)


def is_excluded_title(title: str) -> bool:
    title_lower = title.lower()
    for pattern in EXCLUDE_TITLE_PATTERNS:
        if re.search(pattern, title_lower):
            return True
    return False


def append_to_pipeline(jobs: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PIPELINE_FILE, "a", encoding="utf-8") as f:
        for job in jobs:
            title = job.get("title", "Unknown Title")
            company = job.get("company", "Unknown Company")
            location = job.get("location", "")
            url = job.get("url", "")
            source_query = job.get("_source_query", "")
            matched_title = job.get("search_keyword", "")
            timestamp = datetime.now().strftime("%Y-%m-%d")
            sponsorship = "yes" if job.get("sponsorship_flag") else "no"
            line = ("- [ ] [{title} - {company}]({url}) | {location} | {timestamp} | "
                    "via: {source_query} | matched-title: {matched_title} | "
                    "sponsorship-mentioned: {sponsorship}\n").format(
                title=title, company=company, url=url, location=location,
                timestamp=timestamp, source_query=source_query,
                matched_title=matched_title or "n/a", sponsorship=sponsorship,
            )
            f.write(line)


def load_jd_cache() -> dict:
    if JD_CACHE_FILE.exists():
        with open(JD_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_jd_cache(new_jobs: list) -> None:
    cache = load_jd_cache()
    added = 0
    for job in new_jobs:
        jid = job.get("job_id", "")
        desc = job.get("description", "")
        if jid and desc and jid not in cache:
            cache[jid] = {
                "job_id": jid,
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "seniority": job.get("seniority", ""),
                "description": desc,
                "url": job.get("url", ""),
                "matched_title": job.get("search_keyword", ""),
                "fetched": datetime.now().isoformat(),
            }
            added += 1
    with open(JD_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print("  JD cache: " + str(added) + " new JDs saved (" + str(len(cache)) + " total)")


def ensure_pipeline_header() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PIPELINE_FILE.exists():
        with open(PIPELINE_FILE, "w", encoding="utf-8") as f:
            f.write("# Job Pipeline\n\n")
            f.write("Format: `- [ ] [Title - Company](URL) | Location | Date | via: Query | matched-title: X | sponsorship-mentioned: Y`\n\n")
            f.write("Legend: `[ ]` = pending review | `[x]` = processed | `[-]` = skipped\n\n")
            f.write("---\n\n")


# --- BUDGET GUARD --------------------------------------------------------------

def check_remaining_budget_usd():
    """Live remaining USD in the monthly Apify cap, or None if the check failed."""
    req = urllib.request.Request(
        "https://api.apify.com/v2/users/me/limits",
        headers={"Authorization": "Bearer " + APIFY_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        limit = data["data"]["limits"]["maxMonthlyUsageUsd"]
        used = data["data"]["current"]["monthlyUsageUsd"]
        return limit - used
    except Exception as e:
        print("  WARNING: budget check failed (" + str(e) + ") -- proceeding without live guard")
        return None


# --- SCRAPER ------------------------------------------------------------------

def scrape_market(client: ApifyClient, market: dict, job_post_time: str) -> list:
    print("  Searching: " + market["name"] + " (" + ", ".join(market["jobs_titles"]) + ")")

    actor_input = {
        "jobs_titles": market["jobs_titles"],
        "location": market["location"],
        "jobs_entries": market["jobs_entries"],
        "experience": "Mid-Senior",
        "employment_type": "Full-time",
        "job_post_time": job_post_time,
    }

    try:
        run = client.actor(ACTOR_ID).call(run_input=actor_input)

        dataset_id = run.default_dataset_id if hasattr(run, "default_dataset_id") else run["defaultDatasetId"]

        results = []
        for item in client.dataset(dataset_id).iterate_items():
            job_url = item.get("job_url", "")
            title = item.get("job_title", "")
            company = item.get("company_name", "")
            location = item.get("location", "")
            job_id = item.get("job_id", "")
            description = item.get("job_description", "")
            seniority = item.get("seniority_level", "")
            search_keyword = item.get("search_keyword", "")

            if not job_url or not title:
                continue

            desc_lower = description.lower()
            sponsorship_flag = any(s in desc_lower for s in SPONSORSHIP_SIGNALS)

            results.append({
                "url": job_url,
                "title": title,
                "company": company,
                "location": location,
                "job_id": job_id,
                "description": description,
                "seniority": seniority,
                "search_keyword": search_keyword,
                "sponsorship_flag": sponsorship_flag,
                "_source_query": market["name"] + " (Bucket " + market["bucket"] + ")",
            })

        print("    -> " + str(len(results)) + " results")
        return results

    except Exception as e:
        print("    ERROR for '" + market["name"] + "': " + str(e))
        return []


# --- MAIN ---------------------------------------------------------------------

def main():
    # Force UTF-8 stdout so emoji in job titles (e.g. 🟠 in an excluded-title
    # print) don't crash on Windows cp1252 consoles. A Polish job
    # title once crashed a run this way.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = sys.argv[1:]
    catchup = "--catchup" in args
    job_post_time = "r604800" if catchup else "r86400"

    markets_filter = None
    for arg in args:
        if arg.startswith("--markets="):
            markets_filter = [m.strip() for m in arg.split("=", 1)[1].split(",") if m.strip()]

    active_markets = MARKET_QUERIES
    if markets_filter:
        wanted = {m.lower() for m in markets_filter}
        active_markets = [m for m in MARKET_QUERIES if m["name"].lower() in wanted]
        missing = wanted - {m["name"].lower() for m in active_markets}
        if missing:
            print("WARNING: unknown market name(s), skipping: " + ", ".join(sorted(missing)))
        if not active_markets:
            print("ERROR: --markets matched no entries in MARKET_QUERIES. Aborting.")
            return

    print("=" * 60)
    print("CareerLogic manual scan -- " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("Mode: " + ("CATCH-UP (7-day window)" if catchup else "NORMAL (24h window)"))
    if markets_filter:
        print("Scoped to markets: " + ", ".join(m["name"] for m in active_markets))
    print("=" * 60)

    if not APIFY_TOKEN:
        print("\nERROR: APIFY_API_TOKEN environment variable not set.")
        print("  macOS/Linux: export APIFY_API_TOKEN=your_token")
        print("  Windows:     setx APIFY_API_TOKEN your_token   (then open a new terminal)")
        return
    if not ACTOR_ID:
        print("\nERROR: no Apify actor configured -- set config.json -> scan -> apify_actor.")
        return

    client = ApifyClient(APIFY_TOKEN)
    seen_jobs = load_seen_jobs()
    ensure_pipeline_header()

    worst_case_cost = sum(m["jobs_entries"] for m in active_markets) * COST_PER_RESULT_USD
    remaining = check_remaining_budget_usd()
    if remaining is not None:
        print("\n  Apify budget remaining this cycle: $" + "{:.4f}".format(remaining))
        print("  Worst-case cost for this run:       $" + "{:.4f}".format(worst_case_cost))
        if remaining - worst_case_cost < BUDGET_SAFETY_MARGIN_USD:
            print("\nABORTING Stage 1 (Apify): would risk your monthly cap (would leave <$"
                  + "{:.2f}".format(BUDGET_SAFETY_MARGIN_USD) + " safety margin).")
            print("Check the Apify console before trying again. Proceeding to Stage 2 (scan.mjs) only.")
            run_stage1 = False
        else:
            run_stage1 = True
    else:
        run_stage1 = True

    all_new_jobs = []
    total_found = 0
    total_excluded_title = 0
    total_duplicate = 0

    if run_stage1:
        print("\n--- Stage 1: Apify job listings (" + str(len(active_markets)) + " markets) ---\n")
        for i, market in enumerate(active_markets, 1):
            print("[" + str(i) + "/" + str(len(active_markets)) + "]", end=" ")
            raw_results = scrape_market(client, market, job_post_time)
            total_found += len(raw_results)

            market_new_jobs = []
            for job in raw_results:
                url = job["url"]
                title = job["title"]

                if url in seen_jobs:
                    total_duplicate += 1
                    continue

                if is_excluded_title(title):
                    total_excluded_title += 1
                    print("    - Excluded (title filter): " + title)
                    continue

                seen_jobs.add(url)
                market_new_jobs.append(job)

            # Save after EVERY market, not once at the end -- Apify is billed
            # per market regardless of whether the run finishes, so if this
            # gets interrupted (kill, crash, timeout) after market N, markets
            # 1..N must already be on disk. Losing paid-for results to an
            # in-memory-only batch write has happened once already.
            if market_new_jobs:
                append_to_pipeline(market_new_jobs)
                save_seen_jobs(seen_jobs)
                save_jd_cache(market_new_jobs)
                all_new_jobs.extend(market_new_jobs)

            if i < len(active_markets):
                time.sleep(3)

        print("\n" + "=" * 60)
        print("STAGE 1 SUMMARY (Apify job listings)")
        print("=" * 60)
        print("  Markets queried:     " + str(len(active_markets)))
        print("  Raw results:         " + str(total_found))
        print("  Duplicates skipped:  " + str(total_duplicate))
        print("  Title-filtered:      " + str(total_excluded_title))
        print("  NEW jobs added:      " + str(len(all_new_jobs)))

    # --- Stage 2: scan.mjs (portals.yml -- free, zero Apify cost) ---
    if "--skip-stage2" in args:
        print("\n--- Stage 2: skipped (--skip-stage2) ---")
    else:
        print("\n--- Stage 2: node scan.mjs (career-ops portal scanner) ---\n")
        result = subprocess.run(["node", "scan.mjs"], cwd=str(ROOT_DIR))
        if result.returncode != 0:
            print("  WARNING: scan.mjs exited with code " + str(result.returncode))

    print("\n" + "=" * 60)
    print("DONE. Pipeline file: " + str(PIPELINE_FILE))
    print("=" * 60)


if __name__ == "__main__":
    main()
