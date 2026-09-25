"""
pipeline-quickscreen.py — Screen every pending [ ] entry in data/pipeline.md.

1. Title screen: role-family match, excluded title patterns / companies, nationality-restricted
   tags, and the home-country onsite rule (rule 5) — no JD fetch needed.
2. Cached-JD lookup (data/jd_cache.json) — free, no paid fetch.
3. Eligibility gates over cached JD text: hard residency, citizenship/clearance, sponsorship
   refusal (skipped for US roles when config says you are US work-authorized), local language.
4. Rule-based fit score from profile.json -> scoring -> Tier A (>=3.5) / B (3.0-3.4) / C (<3.0).
5. Writes data/quick-screen-<today>.md with the full funnel.
6. Marks pipeline.md: [x] EVAL for scored, [-] SKIP: reason for screened-out.

Candidate-specific rules (exclusions, home location, work authorization, archetypes, domain
strengths and gaps) come from config.json -> screening and profile.json -> scoring.
Deterministic: no LLM calls.
"""

import json
import re
from pathlib import Path
from datetime import date

ROOT = Path(__file__).parent.parent
PIPELINE = ROOT / "data" / "pipeline.md"
JD_CACHE = ROOT / "data" / "jd_cache.json"
TODAY = date.today().isoformat()
OUTPUT = ROOT / "data" / f"quick-screen-{TODAY}.md"


def _load_json(*names):
    for name in names:
        path = ROOT / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise SystemExit("careerlogic: none of %s found at %s" % (", ".join(names), ROOT))


CFG = _load_json("config.json", "config.example.json")
PROFILE = _load_json("profile.json", "profile.example.json")
SCREEN = CFG["screening"]
SCORING = PROFILE["scoring"]

# ---------------- Title screening ----------------

EXCLUDE_TITLE_PATTERNS = SCREEN.get("exclude_title_patterns", [])

NATIONALITY_RESTRICTED = [
    r"\(UAE National", r"UAE National Only", r"Emirati National\)", r"National Only\)",
]


EXCLUDE_COMPANIES = [c.lower() for c in SCREEN.get("exclude_companies", [])]
EXCLUDE_TITLE_LANGUAGE = [p.lower() for p in SCREEN.get("exclude_title_language_patterns", [])]

POSITIVE_TITLE_MATCH = [
    "product owner", "product manager", "business analyst", "business execution",
    "product analyst", "product developer", "product architect", "product line manager",
    "product management", "product ownership", "functional analyst", "business systems analyst",
    "business intelligence analyst", "business data analyst", "business requirement analyst",
    "technical product", "product/owner", "product/manager",
]

# Semantic (non-literal-phrase) match for rule 1:
# a product/business word + an ownership/leadership word, in ANY order, or a standalone PM/PO/BA
# abbreviation, or a known foreign-language equivalent. Catches "Manager - Product - X",
# "Product Lead", "Head of Product", "Product Manger" (typo), "Produktägare", "产品负责人", etc.
PRODUCT_WORDS = [r"product", r"produkt\w*", r"produto", r"producto", r"产品", r"业务"]
OWNERSHIP_WORDS = [r"owner", r"manager", r"manger", r"lead", r"director", r"head",
                    r"chef", r"ägare", r"ansvarlig", r"负责人", r"经理", r"analyst", r"analytiker"]
STANDALONE_ABBR = [r"\bPM\b", r"\bPO\b(?!\s*box)", r"\bBA\b"]
FOREIGN_EQUIVALENTS = [
    r"produktägare", r"produktchef", r"produktmanager", r"produktansvarlig",
    r"forretningsanalytiker", r"virksomhedsanalytiker",
    r"产品负责人", r"产品经理", r"业务分析师",
]


def is_ambiguous_product_title(title):
    """Broad semantic check: product/business word + ownership word in any order,
    or standalone PM/PO/BA, or a known foreign-language equivalent."""
    t = title.lower()
    has_product = any(re.search(pw, t, re.IGNORECASE) for pw in PRODUCT_WORDS)
    has_owner = any(re.search(ow, t, re.IGNORECASE) for ow in OWNERSHIP_WORDS)
    if has_product and has_owner:
        return True
    if any(re.search(pat, title) for pat in STANDALONE_ABBR):
        return True
    if any(re.search(pat, title, re.IGNORECASE) for pat in FOREIGN_EQUIVALENTS):
        return True
    return False


JD_PRODUCT_ROLE_SIGNALS = [
    "product owner", "product manager", "backlog", "product roadmap", "roadmap",
    "agile", "scrum", "stakeholder management", "user stories", "sprint planning",
    "product vision", "product strategy", "prioritiz", "product backlog",
]


def jd_confirms_product_role(jd_text):
    jd = jd_text.lower()
    hits = sum(1 for sig in JD_PRODUCT_ROLE_SIGNALS if sig in jd)
    return hits >= 2


HOME_ALLOWED_CITY_TOKENS = [t.lower() for t in SCREEN.get("home_allowed_city_tokens", [])]
REMOTE_TOKENS = ["remote", "work from home", "wfh"]
# City names that appear in location strings WITHOUT the country name attached (e.g. a
# "Greater X Area" metro name). Without them, a home-country onsite role outside your allowed
# cities slips past rule 5 and can reach Tier A on keyword strength alone.
HOME_COUNTRY = (SCREEN.get("home_country") or "").lower()
HOME_COUNTRY_CITY_TOKENS = [t.lower() for t in SCREEN.get("home_country_city_tokens", [])]


def in_home_country(loc):
    """True when a (lower-cased) location is in your home country. The country is matched as a
    whole word -- "india" must not match "indianapolis" -- and an empty home_country matches
    nothing."""
    by_name = bool(HOME_COUNTRY) and re.search(r"\b" + re.escape(HOME_COUNTRY) + r"\b", loc) is not None
    return by_name or any(city in loc for city in HOME_COUNTRY_CITY_TOKENS + HOME_ALLOWED_CITY_TOKENS)


def title_screen(title, location, jd_text, has_jd, company=""):
    """Returns (outcome, reason). outcome in {"SKIP", "FLAG", None}. None = proceed to scoring."""
    t = title.lower()
    loc = location.lower()

    c = company.lower()
    for banned in EXCLUDE_COMPANIES:
        if banned and banned in c:
            return "SKIP", f"SKIP: excluded company ({company.strip()}) per config.json"

    for pat in EXCLUDE_TITLE_LANGUAGE:
        if pat and pat in t:
            return "SKIP", f"SKIP: local-language requirement in title ({pat})"

    for pat in NATIONALITY_RESTRICTED:
        if re.search(pat, title, re.IGNORECASE):
            return "SKIP", "SKIP: nationality-restricted title tag (rule 7)"

    for pat in EXCLUDE_TITLE_PATTERNS:
        if re.search(pat, t):
            return "SKIP", f"SKIP: excluded title pattern ({pat.strip(chr(92)+'b')})"

    if not any(kw in t for kw in POSITIVE_TITLE_MATCH):
        # Literal phrase match failed -- do NOT auto-skip. Check semantic/ambiguous match next.
        if is_ambiguous_product_title(title):
            if has_jd:
                if jd_confirms_product_role(jd_text):
                    pass  # JD confirms real product-role substance -- proceed to scoring
                else:
                    return "SKIP", "SKIP: ambiguous title, JD-verified non-product-role (rule 1)"
            else:
                return "FLAG", "FLAG: ambiguous title (product+ownership word, no literal PM/PO/BA phrase), no cached JD to verify -- needs full-JD check before scoring/discarding (rule 1)"
        else:
            return "SKIP", "SKIP: title doesn't match PM/PO/BA family, incl. semantic check (rule 1)"

    if in_home_country(loc) and not any(tok in loc for tok in HOME_ALLOWED_CITY_TOKENS) and not any(tok in loc for tok in REMOTE_TOKENS):
        return "SKIP", "SKIP: home-country onsite outside allowed cities (rule 5)"

    return None, None


# ---------------- Sponsorship / language gate (cached JD only) ----------------

SPONSOR_REFUSAL_PATTERNS = [
    r"unable to.{0,20}sponsor", r"will not sponsor", r"not\s+(?:able|eligible)\s+to\s+sponsor",
    r"no\s+sponsorship", r"cannot sponsor", r"does not sponsor",
    r"right to work in",
    r"must have.{0,20}permit", r"stamp\s*[-]?\s*[14]\b",
    r"must be authorized to work",
]

# Citizenship/clearance gates. These phrases used to live in SPONSOR_REFUSAL_PATTERNS, which the
# US work-authorization carve-out exempts entirely -- but a work visa solves a sponsorship-TIMING
# problem, not a citizenship-ELIGIBILITY one. A federal-contractor role requiring a security
# clearance needs citizenship regardless of visa status; one slipped through as Tier A and got a
# CV/CL generated before it was caught by hand. So they are checked separately, with no US
# carve-out, and only waived when config.json -> screening -> us_citizen is true.
CITIZENSHIP_CLEARANCE_PATTERNS = [
    r"security clearance", r"secret clearance", r"top secret clearance",
    r"must (?:be|have).{0,15}(?:citizen|permanent resident)",
    r"u\.?s\.?\s*citizen(?:ship)?\s*(?:is\s*)?required",
]


# Hard residency gates ("must already live here"). A distinct, harder blocker than sponsorship:
# a role requiring CURRENT physical residency fails regardless of visa status, carries no
# "sponsor" language for SPONSOR_REFUSAL_PATTERNS to catch, and is not a sponsorship question
# the US carve-out should exempt (e.g. a US role limited to residents of specific states).
RESIDENCY_GATE_PATTERNS = [
    r"must (?:already )?(?:be|live|reside).{0,20}based in",
    r"should already be based in",
    r"candidates? must (?:currently )?reside",
    r"applicants? should already be based",
    r"we are hiring specifically for this market",
]

# Markets whose local language you do NOT speak (config.json -> screening ->
# local_language_gate_markets). A JD there written in the local language is skipped (rule 4).
LANGUAGE_GATE_MARKETS = {m.lower() for m in SCREEN.get("local_language_gate_markets", [])}

NON_ENGLISH_MARKET_LANG_SIGNALS = {
    "germany": ["sie verfügen", "ihre aufgaben", "wir suchen", "erfahrung als", "kenntnisse", "unser unternehmen", "gmbh sucht"],
    "netherlands": ["wij zoeken", "je bent", "onze klant", "werkzaamheden", "sollicit"],
    "switzerland": ["wir suchen", "sie verfügen", "vous êtes", "nous recherchons"],
    "france": ["nous recherchons", "vous êtes", "notre client", "poste basé"],
    "belgium": ["wij zoeken", "nous recherchons", "je bent", "vous êtes"],
    "luxembourg": ["nous recherchons", "vous êtes", "wir suchen"],
    "poland": ["poszukujemy", "oferujemy", "twoje zadania", "wymagania"],
    "portugal": ["estamos a", "procuramos", "as tuas responsabilidades", "requisitos"],
    "spain": ["buscamos", "estamos buscando", "requisitos", "nuestro cliente"],
    "sweden": ["vi söker", "dina arbetsuppgifter", "om tjänsten"],
    "denmark": ["vi søger", "dine opgaver"],
    "norway": ["vi søker", "dine oppgaver"],
    "finland": ["haemme", "tehtäväsi"],
    "estonia": ["otsime", "sinu ülesanded"],
}


def sponsorship_and_language_check(location, jd_text):
    """Returns SKIP reason or None. Only called when we have a cached JD."""
    loc_lower = location.lower()
    jd_lower = jd_text.lower()

    # Home-country roles: you already have the right to live and work there, so the residency,
    # citizenship and sponsorship gates below (all about FOREIGN roles) do not apply.
    is_home = in_home_country(loc_lower)

    # Hard residency gate - applies regardless of US/non-US, no sponsorship carve-out (see note above)
    for pat in ([] if is_home else RESIDENCY_GATE_PATTERNS):
        if re.search(pat, jd_lower):
            return f"SKIP: hard residency gate in cached JD ('{pat}') - candidate not currently based there (rule 6 extension)"

    # Citizenship/clearance gate - applies regardless of US/non-US, no sponsorship carve-out
    for pat in ([] if is_home or SCREEN.get("us_citizen") else CITIZENSHIP_CLEARANCE_PATTERNS):
        if re.search(pat, jd_lower):
            return f"SKIP: citizenship/clearance requirement in cached JD ('{pat}') - requires citizenship the candidate does not hold (rule 6 extension)"

    # Sponsorship refusal - US roles exempt only when config marks you US work-authorized
    is_us = bool(SCREEN.get("us_work_authorized")) and ("united states" in loc_lower or re.search(r",\s*(ca|ny|tx|il|ma|wa|fl|nj|pa|oh|ga|nc|va|az|co|mi|md|mn|wi|or|ct|ok|ut|ia|nv|ar|ms|ks|nm|ne|wv|id|hi|nh|me|mt|ri|de|sd|nd|ak|dc|vt|wy|al|in|ky|la|mo|sc|tn)(?:\s+\d{5}(?:-\d{4})?)?\s*$", loc_lower.strip()))
    if not is_us and not is_home:
        for pat in SPONSOR_REFUSAL_PATTERNS:
            if re.search(pat, jd_lower):
                return f"SKIP: sponsorship-refusal signal in cached JD ('{pat}') + no local work rights (rule 6)"

    # Local-language gate (rule 4) - only flag if we have specific strong signal
    for market, signals in NON_ENGLISH_MARKET_LANG_SIGNALS.items():
        if market in LANGUAGE_GATE_MARKETS and market in loc_lower:
            hits = sum(1 for s in signals if s in jd_lower)
            if hits >= 2:
                return f"SKIP: mandatory local-language JD ({market}, {hits} language signals) — candidate doesn't speak it (rule 4)"

    return None


# ---------------- Rule-based fit scoring ----------------

ARCHETYPES = SCORING["archetypes"]
DOMAIN_BOOST = SCORING["domain_boost"]
SENIORITY_BOOST = ["senior", "lead", "principal", "staff", "vp", "vice president", "director", "head of", "avp"]
HARD_GAP_PATTERNS = SCORING["hard_gap_patterns"]

# Named payment-rail / card-network gaps (profile.json -> scoring -> payment_rail_gap_patterns).
# DOMAIN_BOOST's flat "payments" keyword can't tell a specific rail or card network the JD
# requires apart from general payments experience, so a JD naming one you haven't worked on as
# required is a genuine gap. This does NOT auto-skip -- a strong generalist background can still
# clear it with an honest bridge -- it applies a moderate penalty and a visible tag, so buzzword
# density doesn't read as confirmed fit.
PAYMENT_RAIL_GAP_PATTERNS = SCORING["payment_rail_gap_patterns"]


def score_job(title, company, location, jd_text, has_jd):
    t = title.lower()
    loc = location.lower()
    jd = jd_text.lower() if jd_text else ""
    combined = f"{t} {jd}"

    score = 2.0  # base for a title-matched PM/PO/BA role
    reasons = []

    best_arch, best_hits = None, 0
    for name, arch in ARCHETYPES.items():
        hits = sum(1 for kw in arch["keywords"] if kw in combined)
        if hits > best_hits:
            best_hits, best_arch = hits, name
        score += hits * 0.15 * arch["weight"]
    if best_arch and best_hits:
        reasons.append(f"arch:{best_arch}({best_hits})")

    domain_hits = sum(1 for kw in DOMAIN_BOOST if kw in combined)
    if domain_hits:
        score += min(domain_hits * 0.2, 1.0)
        reasons.append(f"domain({domain_hits})")

    if any(s in t for s in SENIORITY_BOOST):
        score += 0.4
        reasons.append("seniority")

    if has_jd:
        for pat in HARD_GAP_PATTERNS:
            if re.search(pat, jd):
                score -= 1.5
                reasons.append(f"hard_gap_flag({pat})")
                break
        rail_hits = [pat for pat in PAYMENT_RAIL_GAP_PATTERNS if re.search(pat, jd)]
        if rail_hits:
            score -= 1.0
            reasons.append(f"payment_rail_gap({len(rail_hits)}:{rail_hits[0].strip(chr(92)+'b')})")
    else:
        # title-only confidence penalty (no JD substance to confirm fit)
        score -= 0.3
        reasons.append("title-only(no cached JD)")

    score = max(0.0, min(round(score, 2), 5.0))
    return score, "; ".join(reasons)


# ---------------- Pipeline parsing ----------------

LINE_RE_NEW = re.compile(
    r"^(?P<prefix>\s*-\s*)\[(?P<box> )\]\s*\[(?P<title_company>.+?)\]\((?P<url>https?://[^)]+)\)\s*\|\s*(?P<location>[^|]+)\|(?P<rest>.*)$"
)
LINE_RE_OLD = re.compile(
    r"^(?P<prefix>\s*-\s*)\[(?P<box> )\]\s*(?P<url>https?://\S+)\s*\|\s*(?P<company>[^|]+)\|\s*(?P<title>[^|]+?)\s*$"
)


def load_jd_cache():
    with open(JD_CACHE, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_job_id(url):
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    return None


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
            "url": m.group("url").strip(), "format": "new",
        }
    m = LINE_RE_OLD.match(line)
    if m:
        return {
            "title": m.group("title").strip(), "company": m.group("company").strip(),
            "location": "", "url": m.group("url").strip(), "format": "old",
        }
    return None


def main():
    jd_cache = load_jd_cache()
    lines = PIPELINE.read_text(encoding="utf-8").split("\n")

    results = []  # (line_idx, parsed, tier_or_skip_or_flag, score, reason)
    stats = {"total": 0, "skip_title": 0, "skip_sponsor": 0, "skip_residency": 0,
              "skip_citizenship": 0, "skip_lang": 0, "flagged": 0,
              "cached": 0, "not_cached": 0, "tier_a": 0, "tier_b": 0, "tier_c": 0}

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("- [ ]"):
            continue
        parsed = parse_line(line)
        if not parsed:
            continue  # malformed line, leave untouched
        stats["total"] += 1

        job_id = extract_job_id(parsed["url"])
        jd_entry = jd_cache.get(job_id) if job_id else None
        jd_text = jd_entry.get("description", "") if jd_entry else ""
        has_jd = bool(jd_text)
        if has_jd:
            stats["cached"] += 1
        else:
            stats["not_cached"] += 1

        outcome, reason = title_screen(parsed["title"], parsed["location"], jd_text, has_jd, parsed.get("company", ""))
        if outcome == "SKIP":
            stats["skip_title"] += 1
            results.append((idx, parsed, "SKIP", None, reason))
            continue
        if outcome == "FLAG":
            stats["flagged"] += 1
            results.append((idx, parsed, "FLAG", None, reason))
            continue

        if has_jd:
            gate_skip = sponsorship_and_language_check(parsed["location"], jd_text)
            if gate_skip:
                if "sponsorship-refusal" in gate_skip:
                    stats["skip_sponsor"] += 1
                elif "residency" in gate_skip:
                    stats["skip_residency"] += 1
                elif "citizenship" in gate_skip:
                    stats["skip_citizenship"] += 1
                else:
                    stats["skip_lang"] += 1
                results.append((idx, parsed, "SKIP", None, gate_skip))
                continue

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
        results.append((idx, parsed, tier, score, reasons))

    # ---- Mark pipeline.md ----
    for idx, parsed, tier, score, reason in results:
        line = lines[idx]
        if tier == "SKIP":
            new_line = re.sub(r"^(\s*-\s*)\[ \]", r"\1[-]", line)
            new_line = new_line.rstrip() + f" | {reason} (quick-screen-batch-{TODAY})"
        elif tier == "FLAG":
            new_line = re.sub(r"^(\s*-\s*)\[ \]", r"\1[!]", line)
            new_line = new_line.rstrip() + f" | {reason} (quick-screen-batch-{TODAY})"
        else:
            new_line = re.sub(r"^(\s*-\s*)\[ \]", r"\1[x]", line)
            new_line = new_line.rstrip() + f" | EVAL: {score}/5 (quick-screen {TODAY}) [{reason}]"
        lines[idx] = new_line

    PIPELINE.write_text("\n".join(lines), encoding="utf-8")

    tier_a = sorted(
        [(score, f"{p['title']} – {p['company']}", p['location'], reasons)
         for _, p, tier, score, reasons in results if tier == "A"],
        key=lambda r: -r[0])
    tier_b = sorted(
        [(score, f"{p['title']} – {p['company']}", p['location'], reasons)
         for _, p, tier, score, reasons in results if tier == "B"],
        key=lambda r: -r[0])
    flagged = [(f"{p['title']} – {p['company']}", p['location'], reasons)
               for _, p, tier, score, reasons in results if tier == "FLAG"]

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(f"# Quick Screen — {TODAY}\n\n")
        f.write("Quick-screen of all pending pipeline.md entries against cached JD text "
                "(data/jd_cache.json). Rule-based scoring, no LLM calls.\n\n")
        f.write("## Funnel\n\n")
        f.write(f"- Total pending screened: **{stats['total']}**\n")
        f.write(f"- Skipped — title mismatch/excluded pattern/nationality-tag/home-country onsite outside allowed cities: **{stats['skip_title']}**\n")
        f.write(f"- Skipped — sponsorship-refusal signal in cached JD: **{stats['skip_sponsor']}**\n")
        f.write(f"- Skipped — hard residency requirement in cached JD: **{stats['skip_residency']}**\n")
        f.write(f"- Skipped — citizenship/security-clearance requirement in cached JD: **{stats['skip_citizenship']}**\n")
        f.write(f"- Skipped — mandatory local-language JD (cached, 2+ language signals): **{stats['skip_lang']}**\n")
        f.write(f"- **Flagged `[!]` — ambiguous title (semantic product+ownership match, no literal PM/PO/BA phrase), no cached JD to resolve it: {stats['flagged']}** (needs full-JD fetch, not scored/discarded)\n")
        f.write(f"- Had cached JD available: **{stats['cached']}** | Title-only (no cache, needs fetch to confirm): **{stats['not_cached']}**\n")
        f.write(f"- **Tier A (>=3.5): {stats['tier_a']}** | Tier B (3.0-3.4): {stats['tier_b']} | Tier C (<3.0): {stats['tier_c']}\n\n")
        f.write("**Caveat:** scores for entries without a cached JD "
                "(`title-only` in reasons) are estimates based on title/company/location keyword "
                "matching alone — not verified against actual JD text, and did NOT get the eligibility "
                "or local-language checks (those only run against cached JD text). Treat Tier-A title-only "
                "entries as candidates for full-JD verification before generating a CV, not as final scores.\n\n")

        for label, tier_list in [("Tier A (>=3.5 — full-eval + CV candidates)", tier_a),
                                   ("Tier B (3.0-3.4 — apply-if-interested)", tier_b)]:
            f.write(f"## {label} — {len(tier_list)}\n\n")
            f.write("| Score | Title / Company | Location | Notes |\n|---|---|---|---|\n")
            for score, title_company, location, reasons in tier_list:
                f.write(f"| {score} | {title_company[:75]} | {location[:35]} | {reasons} |\n")
            f.write("\n")

        f.write(f"## Flagged `[!]` — needs full-JD fetch to resolve — {len(flagged)}\n\n")
        f.write("| Title / Company | Location |\n|---|---|\n")
        for title_company, location, reasons in flagged:
            f.write(f"| {title_company[:75]} | {location[:35]} |\n")
        f.write("\n")

        f.write(f"## Tier C (<3.0 — {stats['tier_c']}, not printed individually, see pipeline.md)\n\n")
        f.write(f"## Skipped ({stats['skip_title'] + stats['skip_sponsor'] + stats['skip_residency'] + stats['skip_citizenship'] + stats['skip_lang']}, "
                f"not printed individually, see pipeline.md `[-]` lines)\n\n")

    print(f"Screened {stats['total']} pending entries.")
    print(f"Skip (title/pattern/nationality/home-onsite): {stats['skip_title']}")
    print(f"Skip (sponsorship refusal, cached JD): {stats['skip_sponsor']}")
    print(f"Skip (residency requirement, cached JD): {stats['skip_residency']}")
    print(f"Skip (citizenship/clearance, cached JD): {stats['skip_citizenship']}")
    print(f"Skip (local-language, cached JD): {stats['skip_lang']}")
    print(f"Flagged (ambiguous title, no cached JD): {stats['flagged']}")
    print(f"Cached JD available: {stats['cached']} / Title-only: {stats['not_cached']}")
    print(f"Tier A: {stats['tier_a']}  Tier B: {stats['tier_b']}  Tier C: {stats['tier_c']}")
    print(f"Report: {OUTPUT}")


if __name__ == "__main__":
    main()
