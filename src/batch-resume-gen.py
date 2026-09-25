"""
batch-resume-gen.py -- Generate a tailored CV and cover letter (HTML) for each job in a batch file.

Input:   data/batch_jobs.json (or BATCH_FILE=path, relative to the repository root). Each entry
         needs company, role, location, title_bar, summary and projects (P-keys from
         profile.json); optional overrides: competencies, jd_keywords, lead_title, cl_bullets,
         current_role_title, current_role_bullets, sponsorship_confirmed, doc_type, force.
Content: every sentence comes from profile.json / config.json or the batch entry -- the
         generator selects and orders material, it never writes new claims.
Style:   black/white Calibri, bold lead verbs, a metrics line per Key Achievement.
Output:  output/resumes/, output/cover-letters/, and a batch manifest in output/.
"""
import io, json, os, sys, re, glob
from datetime import date

sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(__file__))

# --- identity + profile config (see config.example.json / profile.example.json) ---
def _load_json(*names):
    for name in names:
        path = os.path.join(BASE, name)
        if os.path.exists(path):
            with io.open(path, encoding='utf-8') as fh:
                return json.load(fh)
    raise SystemExit('careerlogic: none of %s found at %s' % (', '.join(names), BASE))


CFG = _load_json('config.json', 'config.example.json')
PROFILE = _load_json('profile.json', 'profile.example.json')
_ID = CFG['identity']
NAME = _ID['name']
EMAIL = _ID['email']
PHONE = _ID['phone']
LOCATION = _ID['location']
LINKEDIN_DISPLAY = _ID['linkedin_display']
FILE_PREFIX = CFG['output']['file_prefix']
CL_EXPERIENCE = CFG['cover_letter']['experience_summary']
SIGNOFF = CFG['cover_letter'].get('signoff', 'Sincerely')
CURRENT_ROLE = PROFILE['current_role']
LINESEP = '\n'

RESUME_DIR = os.path.join(BASE, 'output', 'resumes')
CL_DIR = os.path.join(BASE, 'output', 'cover-letters')
TODAY = date.today().strftime('%Y-%m-%d')

os.makedirs(RESUME_DIR, exist_ok=True)
os.makedirs(CL_DIR, exist_ok=True)


def esc_amp(s):
    """Escape a bare & (one not already starting a valid HTML entity) so hand- or tool-authored
    title_bar/summary fields are valid HTML. Existing entities (&nbsp; &amp; &mdash; &#x21b3;)
    are left intact, e.g. 'Payments & Fintech' becomes 'Payments &amp; Fintech'."""
    return re.sub(r'&(?!(?:[A-Za-z][A-Za-z0-9]*|#\d+|#x[0-9A-Fa-f]+);)', '&amp;', s)


# --- Cover-letter guardrails baked into the generator ---
# gen_cl() used to emit cl_bullets verbatim (no bold) and had no work-authorization line, so
# both rules drifted batch to batch. These helpers enforce both in code, so a batch can no
# longer ship a non-conformant cover letter.

# Delimiters that end the lead verb phrase for auto-bolding a bullet (house style: bold the
# opening verb phrase, ~2-9 words, up to the first natural break).
_BOLD_DELIMS = [';', ',', ' &mdash;', ' —', ' &#8212;', ' &ndash;', ' –',
                ' with ', ' for ', ' by ', ' across ', ' on ', ' to ', ' translating ',
                ' cutting ', ' coordinating ', ' reducing ', ' improving ', ' automating ',
                ' matching ', ' serving ', ' spanning ', ' returning ', ' directly ',
                ' aligned ', ' delivering ', ' enabling ', ' driving ']


def autobold_bullet(inner):
    """Ensure a cover-letter bullet leads with a <strong> phrase (house style).
    No-op if the bullet already contains emphasis. Otherwise wrap the lead verb phrase
    (start -> earliest natural break, clamped to 2..9 words) in <strong>."""
    s = inner.strip()
    if '<strong>' in s or '<b>' in s:
        return s
    low = s.lower()
    cuts = [i for i in (low.find(d.lower()) for d in _BOLD_DELIMS) if i > 0]
    cut = min(cuts) if cuts else len(s)
    words = s.split(' ')
    nine = len(' '.join(words[:9]))
    if cut > nine:                       # no early break -> clamp to 9 words
        cut = nine
    lead = s[:cut].rstrip()
    if len(lead.split(' ')) < 2:         # never bold a single word
        lead = ' '.join(words[:3])
    rest = s[len(lead):]
    return f"<strong>{lead}</strong>{rest}"


_US_STATES = {'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
              'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
              'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
              'VA','WA','WV','WI','WY','DC'}


def _is_us_location(loc):
    """A state code only counts as the LAST part of the location, optionally followed by a ZIP
    and/or "United States" ("Austin, TX", "Dover, DE 19901", "Austin, TX, USA"). Matching it
    anywhere made "Perth, WA, Australia", "Paris, Ile-de-France" and "Paris La Defense" US roles."""
    if not loc:
        return False
    u = loc.upper().strip()
    if re.search(r'\b(UNITED STATES|U\.?S\.?A?|USA)\b', u):
        return True
    return any(re.search(r',\s*' + st + r'(?:\s+\d{5}(?:-\d{4})?)?\s*$', u) for st in _US_STATES)


_HOME_RE = re.compile(PROFILE['eligibility']['home_market_pattern'], re.I)


def _clean_where(loc):
    """City, Country from a verbose location, e.g. 'Amsterdam, North Holland, Netherlands'
    -> 'Amsterdam, Netherlands'; 'Singapore' -> 'Singapore'."""
    loc = re.sub(r'\s*\([^)]*\)\s*$', '', loc).strip()   # drop trailing '(Hybrid)' etc
    parts = [p.strip() for p in loc.split(',') if p.strip()]
    if not parts:
        return loc
    return parts[0] if len(parts) == 1 else f"{parts[0]}, {parts[-1]}"


def eligibility_line(job):
    """Work-authorization / relocation sentence for the CL closing, keyed off ROLE location.
    Sentences come from profile.json -> eligibility (use "" to omit a case):
    - home market (home_market_pattern)      -> no line
    - US role                                -> eligibility.us
    - elsewhere, sponsorship confirmed       -> eligibility.abroad_sponsored
    - elsewhere, sponsorship NOT confirmed   -> eligibility.abroad_unsponsored
      (must not claim the employer sponsors -- never fabricate)
    {where} is replaced with the destination."""
    loc = (job.get('location') or '').strip()
    if not loc:
        return ""
    elig = PROFILE['eligibility']
    # Home market -> already there, no relocation line (unless it's a remote-abroad role)
    if _HOME_RE.search(loc) and not re.search(r'remote\s*[-,/]?\s*(u\.?s|usa|eu|uk|europe)', loc, re.I):
        return ""
    if _is_us_location(loc):
        m = re.match(r'\s*([A-Za-z .\-]+?)\s*,?\s*[A-Z]{2}\b', loc)
        city = m.group(1).strip() if m else None
        where = f"to {city}" if city and city.lower() not in ('remote', 'united states') else "within the U.S."
        return elig.get('us', '').replace('{where}', where)
    where = _clean_where(loc)
    key = 'abroad_sponsored' if job.get('sponsorship_confirmed') else 'abroad_unsponsored'
    return elig.get(key, '').replace('{where}', where)


# Headline title tracks the target role type (lead-title matching):
#   Product Owner + Business Analyst -> "Senior Product Owner / Business Analyst"
#   Product Manager + Business Analyst -> "Product Manager / Business Analyst"
#   Business Analyst only            -> "Business Analyst"
#   Product Owner only               -> "Senior Product Owner"
#   Product Manager only             -> "Product Manager"
#   otherwise (ambiguous)            -> dual "Senior Product Owner / Product Manager"
# The lead is applied to the title bar, the summary's first words, and the current-employer
# experience title, so all three stay consistent however the batch entry was written.
_TITLE_TOK = r'(?:Senior Product Owner|Product Manager|Product Owner|Business Analyst)'
_LEAD_RE = re.compile(r'^' + _TITLE_TOK + r'(?: / ' + _TITLE_TOK + r')*')


def lead_title(role, override=None):
    if override:
        return override
    r = (role or '').lower()
    ba = 'business anal' in r          # Business Analyst / Business Analysis
    po = 'product owner' in r
    pm = 'product manager' in r
    if ba and po:
        return 'Senior Product Owner / Business Analyst'
    if ba and pm:
        return 'Product Manager / Business Analyst'
    if ba:
        return 'Business Analyst'
    if po:
        return 'Senior Product Owner'
    if pm:
        return 'Product Manager'
    return 'Senior Product Owner / Product Manager'


def apply_lead(text, lead):
    """Replace a leading title token (Senior Product Owner[ / Product Manager] | Product Manager)
    at the very start of title_bar/summary with the role-matched lead. No-op if no leading title."""
    return _LEAD_RE.sub(lead, text, count=1)


def current_role_lead(lead):
    """Title lead for the current employer's experience line. Collapses a dual or BA lead
    to the matching PM/PO title configured in profile.json -> current_role."""
    if 'Product Manager' in lead:
        return CURRENT_ROLE.get('pm_title', 'Product Manager')
    return CURRENT_ROLE.get('po_title', 'Senior Product Owner')


# Business Analysis competency line (profile.json -> ba_competency), auto-added to Core
# Competencies for any BA-flavoured role. Only list terms your own history can back.
BA_COMPETENCY = PROFILE.get('ba_competency', '')


def is_ba_role(role):
    return 'business anal' in (role or '').lower()


# Domains NOT in the candidate's background (profile.json -> domains_not_held). If a
# summary/title_bar claims one as HELD experience ("owning insurance product delivery"), that is
# invented-domain fabrication; the correct pattern is "...aligned to {domain}".
# Non-blocking WARN only: whoever writes the batch entry decides; this surfaces the risk.
DOMAINS_NOT_HELD = PROFILE.get('domains_not_held', [])
_CLAIM_VERB = re.compile(r'\b(owning|delivering|delivered|led|drove|managed)\b', re.I)


def warn_invented_domain(job):
    """Print a WARN (stderr) if the summary phrases an un-held domain as held experience.
    'aligned to {domain}' framing is fine and does NOT trip this check."""
    summ = job.get('summary', '')
    low = summ.lower()
    for d in DOMAINS_NOT_HELD:
        if d not in low:
            continue
        # Skip the safe positioning phrasing.
        if re.search(r'aligned to [^.]*' + re.escape(d), low):
            continue
        # Flag only when a claim verb governs the domain in the same clause.
        seg = low.split('.')[0]
        if d in seg and _CLAIM_VERB.search(seg):
            print(f"WARN [{job.get('role','?')} @ {job.get('company','?')}]: summary claims "
                  f"un-held domain '{d}' as experience — reframe to 'aligned to {d}' "
                  f"(no-invented-domain rule).", file=sys.stderr)


def jd_kw_list(job):
    """Normalize `jd_keywords` to a list. Accepts a list OR a comma-separated string.
    Guards the char-split bug: iterating a raw string yields single characters, which
    rendered the Core Competencies line as 'R, i, s, k, ...'. Always route through this."""
    raw = job.get('jd_keywords') or []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(',')]
    return [str(k).strip() for k in raw if str(k).strip()]


def warn_body_domain(job):
    """Enforce the A-vs-B split. `jd_keywords` route Class-A
    (truthful, JD-phrased skill/functional terms) into the resume body for ATS.
    A Class-B industry-domain noun the candidate lacks must NEVER be routed to the
    body — it belongs in the title-bar target label and the cover letter, not a
    competency line. Non-blocking WARN."""
    for k in jd_kw_list(job):
        low = str(k).lower()
        for d in DOMAINS_NOT_HELD:
            if d in low:
                print(f"WARN [{job.get('role','?')} @ {job.get('company','?')}]: jd_keywords "
                      f"entry '{k}' contains un-held domain '{d}' — Class-B domain nouns must "
                      f"NOT be routed into the resume body (A-vs-B split). "
                      f"Keep it in the title-bar label / cover letter.", file=sys.stderr)
                break

CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
html { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
body { font-family:'Calibri','Arial',sans-serif; font-size:10.5pt; line-height:1.45; color:#000; background:#fff; }
.page { width:210mm; margin:0 auto; padding:0.6in 0.7in; }
.cv-name { text-align:center; font-size:21pt; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:3px; }
.cv-contact { text-align:center; font-size:9.5pt; color:#222; margin-bottom:7px; }
.cv-title-bar { border-top:1.5px solid #000; border-bottom:1.5px solid #000; padding:5px 0 4px; text-align:center; font-size:10.5pt; font-weight:700; letter-spacing:0.02em; text-transform:uppercase; margin-bottom:11px; }
.sec { margin-bottom:2px; }
.sec-head { font-size:10.5pt; font-weight:700; text-transform:uppercase; border-bottom:1.5px solid #000; padding-bottom:1px; margin-bottom:5px; margin-top:9px; letter-spacing:0.02em; }
.summary { font-size:10.5pt; line-height:1.5; }
.comp-line { font-size:10.5pt; line-height:1.48; margin-bottom:1px; }
.cl { font-weight:700; }
.ach { margin-bottom:9px; }
.ach-hdr { font-size:10.5pt; line-height:1.4; }
.ach-title { font-weight:700; }
.ach-co { font-style:italic; }
.ach-metrics { font-size:10.5pt; font-weight:700; margin-bottom:1px; }
.ach-desc { font-size:10.5pt; line-height:1.5; }
.job { margin-bottom:8px; }
.job-hdr { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:0; }
.job-co { font-size:10.5pt; font-weight:700; text-transform:uppercase; }
.job-dt { font-size:10.5pt; font-style:italic; white-space:nowrap; margin-left:8px; }
.job-ttl { font-size:10.5pt; font-style:italic; margin-bottom:3px; }
.buls { list-style:none; padding:0; margin:0; }
.buls li { font-size:10.5pt; line-height:1.48; margin-bottom:2px; padding-left:13px; text-indent:-13px; }
.edu-ln { font-size:10.5pt; line-height:1.5; }
.cert-list { list-style:none; padding:0; margin:0; }
.cert-list li { font-size:10.5pt; line-height:1.48; margin-bottom:2px; padding-left:13px; text-indent:-13px; }
"""

# ── Project library ──────────────────────────────────────────────────────────
PROJECTS = PROFILE['projects']


def build_experience_html(job, lead):
    """Render Professional Experience from profile.json -> experience. The entry flagged
    "current": true takes its title/bullets from the job override when present, else from
    current_role defaults."""
    out = []
    for e in PROFILE['experience']:
        if e.get('current'):
            title = job.get('current_role_title',
                    current_role_lead(lead) + CURRENT_ROLE.get('title_suffix', ''))
            bullets = job.get('current_role_bullets', CURRENT_ROLE['default_bullets'])
        else:
            title, bullets = e['title'], e['bullets']
        li = LINESEP.join('<li>&bull; %s</li>' % b for b in bullets)
        out.append(
            '<div class="job">' + LINESEP
            + '<div class="job-hdr"><span class="job-co">%s</span><span class="job-dt">%s</span></div>' % (e['company'], e['dates']) + LINESEP
            + '<div class="job-ttl">%s</div>' % title + LINESEP
            + '<ul class="buls">' + LINESEP + li + LINESEP + '</ul>' + LINESEP + '</div>')
    head = '<div class="sec">' + LINESEP + '<div class="sec-head">Professional Experience</div>' + LINESEP + LINESEP
    return head + (LINESEP + LINESEP).join(out) + LINESEP + '</div>'


def make_ach(pk):
    p = PROJECTS[pk]
    return f"""<div class="ach">
<div class="ach-hdr"><span class="ach-title">{p['title']}</span> &nbsp;|&nbsp; <span class="ach-co">{p['company']} | {p['year']}</span></div>
<div class="ach-metrics">&#x21b3; {p['metrics']}</div>
<div class="ach-desc">{p['desc']}</div>
</div>"""


def resume_contact_line(job):
    """Resume contact line -- your location is shown for home-market roles (a plus there) and
    omitted for roles abroad (ATS geo-distance filters can down-rank or reject on a mismatched
    location before a human ever reads the cover letter's relocation line)."""
    loc = job.get('location') or ''
    parts = [PHONE, EMAIL, LINKEDIN_DISPLAY]
    if _HOME_RE.search(loc):
        parts.insert(0, LOCATION)
    return ' &nbsp;|&nbsp; '.join(parts)


def gen_resume(job):
    projects = job['projects']
    achs = "\n".join(make_ach(p) for p in projects)
    lead = lead_title(job.get('role'), job.get('lead_title'))
    contact_line = resume_contact_line(job)
    title_bar = esc_amp(apply_lead(job['title_bar'], lead))
    summary = esc_amp(apply_lead(job['summary'], lead))
    # The current employer is ONE blended experience block, on purpose. Splitting it into three
    # sub-role entries (own title/bullets each) made a target ATS's "autofill from resume" parser
    # go from 3 experiences populated (with imperfect blended bullets) to ZERO. Best-supported
    # theory: the parser keys on company name for overlap detection, and the same company three
    # times in a row with adjacent date ranges made it abandon the whole Experience section rather
    # than degrade. The imperfect-bullets issue is real but far smaller than a total parse failure.
    # Earlier-career entries keep their own title lines, which is safe: no company name repeats.
    experience_html = build_experience_html(job, lead)
    education_html = LINESEP.join('<div class="edu-ln">%s</div>' % e for e in PROFILE.get('education', []))
    certs_html = LINESEP.join('<li>&bull; %s</li>' % c for c in PROFILE.get('certifications', []))

    comps = list(job.get('competencies', PROFILE['competencies']))
    # A-vs-B split: jd_keywords = Class-A truthful, JD-phrased skill
    # terms routed into the body for ATS match. Class-B domain nouns are blocked by
    # warn_body_domain() and belong in the title-bar label / cover letter, not here.
    jd_kw = jd_kw_list(job)
    if jd_kw:
        label = job.get('jd_keywords_label', 'Role-Aligned Delivery')
        comps.append(f'<span class="cl">{esc_amp(label)}:</span> '
                     + ', '.join(esc_amp(k) for k in jd_kw))
    # BA-flavored role -> ensure the Business Analysis keyword block is present (ATS: BA reqs
    # lead with BRD/SRS/UML/requirements-engineering vocabulary the base template lacks).
    if BA_COMPETENCY and is_ba_role(job.get('role')) and not any('Business Analysis:' in c for c in comps):
        comps.append(BA_COMPETENCY)
    comp_html = "\n".join(f'<div class="comp-line">{esc_amp(c)}</div>' for c in comps)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{NAME} &ndash; CV</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">

<div class="cv-name">{NAME}</div>
<div class="cv-contact">{contact_line}</div>
<div class="cv-title-bar">{title_bar}</div>

<div class="sec">
<div class="sec-head">Professional Summary</div>
<div class="summary">{summary}</div>
</div>

<div class="sec">
<div class="sec-head">Core Competencies</div>
{comp_html}
</div>

<div class="sec">
<div class="sec-head">Key Achievements</div>
{achs}
</div>

{experience_html}

<div class="sec">
<div class="sec-head">Education</div>
{education_html}
</div>

<div class="sec">
<div class="sec-head">Certifications &amp; Training</div>
<ul class="cert-list">
{certs_html}
</ul>
</div>

</div>
</body>
</html>"""


# Continental EU/EEA (+ CH) markets that use "Motivation Letter" (not "Cover Letter") and
# expect a GDPR consent line. Anglophone EU (UK, Ireland) keeps the Cover Letter convention.
_CONTINENTAL_EU = re.compile(r'\b(netherlands|germany|deutschland|france|luxembourg|denmark|'
                             r'sweden|estonia|poland|belgium|austria|switzerland|suisse|portugal|'
                             r'finland|norway|italy|spain|czech|slovak|hungary|romania|greece|'
                             r'lithuania|latvia|amsterdam|berlin|munich|frankfurt|paris|copenhagen|'
                             r'stockholm|tallinn|warsaw|krak|brussels|zurich|geneva|lisbon|helsinki|oslo)\b',
                             re.I)


def is_motivation_letter(job):
    """True -> Continental EU/EEA employer: use 'Motivation Letter' + GDPR line. An explicit
    job['doc_type'] overrides location detection."""
    dt = (job.get('doc_type') or '').lower()
    if dt in ('motivation_letter', 'motivation', 'ml'):
        return True
    if dt in ('cover_letter', 'cover', 'cl'):
        return False
    loc = job.get('location') or ''
    if re.search(r'\b(united kingdom|uk|england|scotland|wales|ireland|dublin|london|cork)\b', loc, re.I):
        return False
    return bool(_CONTINENTAL_EU.search(loc))


def gen_cl(job):
    company = job['company']
    role = job['role']
    ml = is_motivation_letter(job)
    doc_label = "Motivation Letter" if ml else "Cover Letter"
    gdpr_html = ('\n  <div class="closing" style="margin-top:12px;font-size:9.5pt;color:#333;">'
                 'I consent to the processing of my personal data for recruitment purposes in '
                 'accordance with the EU General Data Protection Regulation (GDPR).</div>') if ml else ''
    cl_bullets = job.get('cl_bullets', PROFILE['cl_default_bullets'])
    if any('<strong>' not in b and '<b>' not in b for b in cl_bullets):
        print(f"WARN [{role} @ {company}]: cl_bullets authored without <strong> — "
              "auto-bolding lead phrases (author bold metrics for best control)", file=sys.stderr)
    bul_html = "\n".join(f'<li>&bull; {autobold_bullet(b)}</li>' for b in cl_bullets)
    elig = eligibility_line(job)
    elig_html = (elig + " ") if elig else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{doc_label} &ndash; {company}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Calibri','Arial',sans-serif; font-size:11pt; line-height:1.6; color:#000; background:#fff; }}
.page {{ width:210mm; margin:0 auto; padding:0.8in 0.8in; }}
.name {{ font-size:18pt; font-weight:700; text-transform:uppercase; letter-spacing:0.04em; }}
.contact {{ font-size:9.5pt; color:#222; margin-bottom:15px; }}
.divider {{ border-top:1.5px solid #000; margin-bottom:15px; }}
.hook {{ font-size:11pt; margin-bottom:12px; }}
.bullets {{ list-style:none; padding:0; margin-bottom:12px; }}
.bullets li {{ font-size:11pt; line-height:1.6; margin-bottom:6px; padding-left:14px; text-indent:-14px; }}
.closing {{ font-size:11pt; margin-top:8px; }}
</style>
</head>
<body>
<div class="page">
  <div class="name">{NAME}</div>
  <div class="contact">{LOCATION} &nbsp;|&nbsp; {PHONE} &nbsp;|&nbsp; {EMAIL} &nbsp;|&nbsp; {LINKEDIN_DISPLAY}</div>
  <div class="divider"></div>
  <div class="hook">I am writing to express my interest in the <strong>{role}</strong> position at <strong>{company}</strong>. With {CL_EXPERIENCE}</div>
  <ul class="bullets">
{bul_html}
  </ul>
  <div class="closing">{elig_html}I would welcome the opportunity to discuss how my experience aligns with {company}'s goals. I am available at your convenience for a conversation.</div>{gdpr_html}
  <div class="closing" style="margin-top:20px;">{SIGNOFF},<br><strong>{NAME}</strong></div>
</div>
</body>
</html>"""


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:40]


def make_resume_filename(job):
    role_slug = re.sub(r'[^A-Za-z0-9]+', '_', job['role']).strip('_')
    co_slug = re.sub(r'[^A-Za-z0-9]+', '_', job['company']).strip('_')
    return f"{FILE_PREFIX}_{role_slug}_{co_slug}_Resume"


def make_cl_filename(job):
    role_slug = re.sub(r'[^A-Za-z0-9]+', '_', job['role']).strip('_')
    co_slug = re.sub(r'[^A-Za-z0-9]+', '_', job['company']).strip('_')
    suffix = 'MotivationLetter' if is_motivation_letter(job) else 'CoverLetter'
    return f"{FILE_PREFIX}_{role_slug}_{co_slug}_{suffix}"


# --- Dedup gate (closes the loophole where the same posting, re-scanned under a slightly
# different company/role spelling and a fresh job_id, was regenerated with no check against
# the tracker / prior output — e.g. "ACME Corp / Payments ... PM (VP)" vs "Acme Corporation /
# Payments ... Product Manager"). Normalizes company + role via config.json -> company_aliases and
# skips a job that matches something already generated (prior manifests) or tracked
# (applications.md). Per-job "force": true overrides; BATCH_NO_DEDUP=1 disables globally.
_ROLE_STOP = {'the', 'a', 'an', 'of', 'and', 'for', 'to', 'vp', 'avp', 'svp', 'evp', 'senior',
              'sr', 'junior', 'jr', 'lead', 'staff', 'principal', 'i', 'ii', 'iii', 'iv',
              'product', 'manager', 'owner', 'analyst', 'business', 'technical', 'digital',
              'specialist', 'director', 'associate', 'consultant', 'po', 'pm', 'ba', 'bsa'}


_COMPANY_ALIASES = CFG.get('company_aliases', {})


def _norm_company(c):
    """Collapse spelling variants of the same employer to one canonical token so the dedup
    gate catches a re-scanned posting. Variants come from config.json -> company_aliases."""
    t = re.sub(r'[^a-z0-9]', '', (c or '').lower())
    for canon, forms in _COMPANY_ALIASES.items():
        if any(t == f or t.startswith(f) for f in forms):
            return canon
    return t


def _role_core(r):
    toks = re.sub(r'[^a-z0-9]', ' ', (r or '').lower()).split()
    return frozenset(t for t in toks if t not in _ROLE_STOP and len(t) > 2)


def _existing_keys():
    """(_norm_company, _role_core, source, row_num) for every job already generated or tracked.

    row_num (applications.md's leading `#` column, or None for manifest entries) lets
    _dup_match exclude a job's OWN tracker row from matching against itself -- see note there.
    """
    keys = []
    for mp in glob.glob(os.path.join(BASE, 'output', 'batch-manifest-*.json')):
        try:
            with open(mp, encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            continue
        for e in (data if isinstance(data, list) else []):
            if isinstance(e, dict) and e.get('company') and e.get('role'):
                keys.append((_norm_company(e['company']), _role_core(e['role']), os.path.basename(mp), None))
    ap = os.path.join(BASE, 'data', 'applications.md')
    if os.path.exists(ap):
        for ln in open(ap, encoding='utf-8'):
            cells = [c.strip() for c in ln.split('|')]
            if len(cells) >= 6 and cells[1].isdigit():
                keys.append((_norm_company(cells[3]), _role_core(cells[4]), 'applications.md', cells[1]))
    return keys


def _dup_match(company, role, existing, job_id=None):
    """job_id is the CURRENT job's own report/tracker number (batch_jobs.json's "job_id").

    The full evaluation writes the tracker row BEFORE CV/CL generation runs, usually in a
    separate later pass. So applications.md already contains THIS job's own row when this
    dedup check runs, and without the row-number exclusion below the job would jaccard-match
    itself (jacc=1.0) and be skipped as a "duplicate" of itself -- which did happen, and was
    only worked around with a manual "force": true. Skip any existing row whose number equals
    this job's job_id, so a job can never be a duplicate of its own evaluation.
    """
    ck, core = _norm_company(company), _role_core(role)
    if not core:
        return None
    for eck, ecore, src, row_num in existing:
        if eck != ck or not ecore:
            continue
        if job_id is not None and row_num == str(job_id):
            continue
        jacc = len(core & ecore) / len(core | ecore)
        if jacc >= 0.6 or core <= ecore or ecore <= core:
            return src
    return None


def main():
    config_path = os.path.join(BASE, os.environ.get('BATCH_FILE', os.path.join('data', 'batch_jobs.json')))
    with open(config_path, encoding='utf-8') as f:
        jobs = json.load(f)

    dedup_on = os.environ.get('BATCH_NO_DEDUP') != '1'
    existing = _existing_keys() if dedup_on else []

    generated = []
    skipped = []
    for job in jobs:
        if dedup_on and not job.get('force'):
            src = _dup_match(job['company'], job['role'], existing, job_id=job.get('job_id'))
            if src:
                print(f"SKIP dup [{job['role']} @ {job['company']}]: matches existing entry "
                      f"in {src}. Add \"force\": true to regenerate anyway.", file=sys.stderr)
                skipped.append(f"{job['role']} @ {job['company']} (dup of {src})")
                continue
        existing.append((_norm_company(job['company']), _role_core(job['role']), 'this-run', str(job.get('job_id')) if job.get('job_id') is not None else None))
        warn_invented_domain(job)
        warn_body_domain(job)
        cv_base = make_resume_filename(job)
        cl_base = make_cl_filename(job)

        cv_html_name = f"{cv_base}.html"
        cl_html_name = f"{cl_base}.html"

        cv_html = gen_resume(job)
        cl_html = gen_cl(job)

        cv_path = os.path.join(RESUME_DIR, cv_html_name)
        cl_path = os.path.join(CL_DIR, cl_html_name)

        with open(cv_path, 'w', encoding='utf-8') as f:
            f.write(cv_html)
        with open(cl_path, 'w', encoding='utf-8') as f:
            f.write(cl_html)

        generated.append({
            'job_id': job.get('job_id', ''),
            'company': job['company'],
            'role': job['role'],
            'cv_html': cv_html_name,
            'cl_html': cl_html_name,
            'cv_pdf': cv_html_name.replace('.html', '.pdf'),
            'cl_pdf': cl_html_name.replace('.html', '.pdf'),
            'cv_dir': 'output/resumes',
            'cl_dir': 'output/cover-letters',
        })
        print("OK: " + cv_html_name)

    manifest_path = os.path.join(BASE, 'output', f'batch-manifest-{TODAY}.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(generated, f, indent=2)
    print("\nGenerated " + str(len(generated)) + " resumes + CLs. Manifest: " + manifest_path)
    if skipped:
        print(f"Skipped {len(skipped)} duplicate(s):", file=sys.stderr)
        for s in skipped:
            print("  - " + s, file=sys.stderr)


if __name__ == '__main__':
    main()
