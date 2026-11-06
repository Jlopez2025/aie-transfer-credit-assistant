"""
"Find description": search the sending school's own website for a course, then extract and
validate its catalog description.

Steps
  1. Official domain: look it up once per school (web search for its .edu site), saved in
     data/domains.json so an evaluator can correct it.
  2. Search that domain for the course code and title. Search engines are tried in order
     (SEARCH_PROVIDERS, default "tavily"; Brave stays off until its no-storage terms are reviewed); the next one runs only when the previous one
     found nothing acceptable. Each engine has its own index: Tavily missed a Bristol catalog PDF
     that other engines still list.
  3. Excerpt the page around the course code (or title) and ask GPT-4o-mini to copy out the
     description only if the page is about the same course. Results without page text are fetched
     politely (fetcher.py) when their summary mentions the course.
  4. Validate before accepting: the source is on the school's domain, the description appears
     verbatim on the page, and the code or title agrees with the transcript.

Only the institution name, course code and course title are sent to the search services.
Never send student names, IDs or grades.
"""
import os, re, json, time, urllib.request, urllib.error, urllib.parse, ssl
from urllib.parse import urlparse
from difflib import SequenceMatcher
import azure_client as az
import coverage as cov             # title-word helpers shared with the catalog lookup
import fetcher
import archive
import claude_search      # Claude research provider (Anthropic SDK lives in that module only)
import azure_research     # Azure OpenAI research agent (function calling over our own tools)

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_CTX = ssl.create_default_context()

def tavily_key():
    return az.ENV.get("TAVILY_API_KEY", "").strip()

def brave_key():
    return az.ENV.get("BRAVE_API_KEY", "").strip()

def _http_json(req, label):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60, context=_CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(f"{label} search failed: HTTP {e.code} {body}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(f"{label} search failed: {e}")
    return {}

def brave_search(query, domain=None, count=10):
    """Brave web search. Results carry a summary and extra snippets, never page text."""
    q = f"{query} site:{domain}" if domain else query
    url = BRAVE_URL + "?" + urllib.parse.urlencode({"q": q, "count": count, "extra_snippets": "true"})
    req = urllib.request.Request(url, headers={"Accept": "application/json", "X-Subscription-Token": brave_key()})
    results = (_http_json(req, "Brave").get("web") or {}).get("results", [])
    return [{"url": r.get("url", ""), "title": r.get("title", ""),
             "text": " … ".join([r.get("description") or ""] + (r.get("extra_snippets") or [])),
             "page_text": False} for r in results]

def tavily_results(query, domain=None, depth="basic"):
    rs = tavily_search(query, include_domains=[domain] if domain else None, depth=depth)
    return [{"url": r.get("url", ""), "title": r.get("title", ""),
             "text": r.get("raw_content") or r.get("content") or "", "page_text": bool(r.get("raw_content"))}
            for r in rs]

# name -> (key check, search(query, domain, depth), queries to run for one course)
def claude_results(task, domain, depth):
    """One Claude research run (claude_search.py). Returns (fetched pages, Claude's report for the log)."""
    results, report = claude_search.research(task["inst"], task["code"], task["title"], domain, task["year"])
    u = report["usage"]
    note = (f"Claude report ({u['searches']} searches, {u['fetches']} fetches, {u['input_tokens']:,} input tokens): "
            + (report["text"][:600] or "no text"))
    return results, note

def azure_agent_results(task, domain, depth):
    """One Azure OpenAI research run (azure_research.py). Returns (pages it read, report for the log)."""
    results, rep = azure_research.research(task["inst"], task["code"], task["title"], domain, task["year"])
    r, u = rep["report"], rep["usage"]
    named = " ".join(x for x in (r.get("course_code"), r.get("course_title")) if x)
    note = (f"Azure agent report ({u['searches']} searches, {u['reads']} pages read, {u['tokens']:,} tokens): "
            f"{'found' if r.get('found') else 'not found'}{' · ' + named if named else ''} · {r.get('notes', '')}")
    return results, note[:700]

PROVIDERS = {
    # Azure OpenAI researches like DATUM's tool (plans searches, reads pages and PDFs, a second
    # round if needed) using Tavily + our fetcher + archives as its tools; no Bing grounding.
    "azure": (azure_research.available, azure_agent_results,
              lambda inst, code, title, year=None: [({"inst": inst, "code": code, "title": title, "year": year}, "")]),
    # Claude researches like DATUM's tool (search + fetch on Anthropic's side, multi-round, PDFs);
    # its fetched pages still go through the same extraction and checks as every other engine.
    "claude":(claude_search.available, claude_results,
               lambda inst, code, title, year=None: [({"inst": inst, "code": code, "title": title, "year": year}, "")]),
    "tavily": (tavily_key, tavily_results,
               lambda inst, code, title, year=None: [(f'{inst} "{code}" {title}', "basic"),
                                                     (f"{inst} {title} course description catalog", "advanced"),
                                                     (f"{inst} {title} course", "advanced")]),   # plain title: misread codes
    "brave": (brave_key, lambda q, domain, depth: brave_search(q, domain),
              lambda inst, code, title, year=None: [(f'"{code}" {title}', ""), (f"{title} course description", "")]),
}

def active_providers():
    order = [p.strip().lower() for p in az.ENV.get("SEARCH_PROVIDERS", "azure").split(",") if p.strip()]
    return [p for p in order if p in PROVIDERS and PROVIDERS[p][0]()]

def search_ready():
    return bool(active_providers())

def tavily_search(query, include_domains=None, depth="basic", max_results=5, raw=True):
    payload = {"query": query, "search_depth": depth, "max_results": max_results,
               "include_raw_content": "text" if raw else False}
    if include_domains:
        payload["include_domains"] = include_domains
    req = urllib.request.Request(TAVILY_URL, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {tavily_key()}")
    return _http_json(req, "Tavily").get("results", [])

# ---------------- helpers ----------------
def norm(t):
    return re.sub(r"\s+", " ", (t or "").lower().replace("&", "and")).strip()

def host(url):
    return (urlparse(url).hostname or "").lower()

def registrable(h):
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h

NAME_STOP = {"of", "the", "at", "and", "university", "college", "community", "institute", "state"}

def name_words(name):
    return [w for w in re.findall(r"[a-z]+", name.lower()) if w not in NAME_STOP]

def code_variants(code):
    m = re.match(r"\s*([A-Za-z]+)[\s\-]*([0-9]+[A-Za-z]?)", code or "")
    if not m:
        return [code]
    s, n = m.group(1).upper(), m.group(2).upper()
    return [f"{s} {n}", f"{s}-{n}", f"{s}{n}", f"{s} {n}"]

def title_pos(text, title):
    """Position of the passage that best covers the title's words ("Intro to Microeconomics" finds
    "Introduction to Microeconomics"), or -1 when no passage covers at least 75% of them."""
    want = cov.words(title)
    if not want:
        return -1
    best, best_pos = 0.0, -1
    for m in re.finditer(r"[^\n.|•;]{3,160}", text):
        seg = cov.words(m.group(0))
        score = sum(cov.word_in(w, seg) for w in want) / len(want)
        if score > best:
            best, best_pos = score, m.start()
    return best_pos if best >= 0.75 else -1

def excerpt(text, code, title, before=400, after=2600, hint_code=""):
    """Window of page text around the course code (or a code the researcher reported, for misread
    transcript codes), else around the title; None if none appears."""
    low = text.lower()
    pos = -1
    for v in code_variants(code) + (code_variants(hint_code) if hint_code else []):
        pos = low.find(v.lower())
        if pos >= 0:
            break
    if pos < 0 and title:
        pos = title_pos(text, title)
    if pos < 0:
        return None
    return text[max(0, pos - before): pos + after]

# ---------------- step 1: official domain ----------------
def find_domain(institution, city="", state=""):
    """Best guess at the school's official .edu domain from a web search. Returns (domain, evidence url)."""
    q = f"{institution} {city} {state} official website".replace("  ", " ")
    # Tavily is also the Azure agent's search tool, so it does this lookup when the agent is the only engine.
    if tavily_key():
        results = tavily_search(q, depth="basic", max_results=8, raw=False)
    elif "brave" in active_providers():
        results = brave_search(q)
    else:
        return "", ""
    want = name_words(institution)
    for r in results:
        h = host(r.get("url", ""))
        if not h.endswith(".edu"):
            continue
        title = (r.get("title") or "").lower()
        hit = sum(w in title or w in h for w in want)
        if not want or hit / len(want) >= 0.5:
            return registrable(h), r["url"]
    return "", ""

# ---------------- step 3: extract ----------------
EXTRACT_SYS = (
    "You extract college course catalog descriptions. You get a course from a student's transcript "
    "(institution, course code, course title) and an excerpt of a web page. Decide whether the excerpt "
    "contains the catalog entry for that SAME course. Transcript codes are often misread or renumbered "
    "(ECON 1130 on a transcript may be ECON 1134 in the catalog), so a page course whose TITLE means the "
    "same thing (\"Intro to\" = \"Introduction to\", abbreviations expanded) counts as the same course even "
    "when the code differs; a matching code with an unrelated title does not. If it does, copy the course description "
    "VERBATIM and COMPLETE from the excerpt, from its first sentence (including opening questions) to its last: "
    "do not paraphrase, summarize, skip, or add words; leave out prerequisite, "
    "corequisite, credit, and fee lines. Report the code and title as printed on the page, the first "
    "year of the catalog edition as a number if the page states one (2022 for a 2022-2023 catalog; 0 if "
    "not stated), and credits if shown. If the excerpt is a different course, a list "
    "without descriptions, or a schedule/syllabus index, set found to false. Respond ONLY as JSON: "
    '{"found":true|false,"code":"","title":"","description":"","catalog_start_year":0,"credits":"","reason":"<=20 words"}'
)
# Why a start year, not "2022-2023": SNHU's Azure OpenAI content filter reads that pattern in a
# response as a phone number and blanks the whole answer. The app builds the range itself.

def catalog_year(ext, url=""):
    """Catalog edition: a range in the source address wins ("2019-2020-catalog.pdf"), then the model's start year."""
    m = re.search(r"((?:19|20)\d\d)[-_]((?:19|20)\d\d)", url or "")
    if m and int(m.group(2)) - int(m.group(1)) in (1, 2):
        return f"{m.group(1)}-{m.group(2)}"
    try:
        y = int(ext.get("catalog_start_year") or 0)
    except (TypeError, ValueError):
        return ""
    return f"{y}-{y + 1}" if 1950 < y < 2100 else ""

PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

def redact(text):
    """Drop phone numbers and email addresses (catalog headers and footers carry the school's).
    SNHU's Azure OpenAI deployment filters PII from responses, and a filtered response comes back
    empty, so leaving them in loses the whole answer. Course descriptions don't need them."""
    return EMAIL.sub("[email]", PHONE.sub("[phone]", text or ""))

def extract(institution, code, title, url, text):
    user = (f"INSTITUTION: {institution}\nTRANSCRIPT COURSE: {code} {title}\nPAGE URL: {url}\n\n"
            f"EXCERPT:\n{redact(text)}")
    try:
        out = az.chat([{"role": "system", "content": EXTRACT_SYS}, {"role": "user", "content": user}],
                      json_mode=True, max_tokens=700)
        if not out:
            return {"found": False, "reason": "empty response (Azure content filter)"}
        return json.loads(out)
    except Exception as e:
        return {"found": False, "reason": f"extract error: {str(e)[:80]}"}

# ---------------- step 4: validate ----------------
# Title-to-description similarity. On the sample: real descriptions 0.52-0.59, unrelated page text
# that the extractor returned (accreditation blurbs, program lists) 0.05-0.25.
MIN_RELEVANCE = 0.40
_emb_cache = None

def relevance(title, desc):
    global _emb_cache
    from embed_and_match import load_cache, embed_texts, normalize
    if _emb_cache is None:
        _emb_cache = load_cache()
    E = normalize(embed_texts([cov.norm_title(title), desc], _emb_cache))
    return float(E[0] @ E[1])

def validate(ext, page_text, url, domain, code, title):
    """Return a list of problems; empty means accept."""
    problems = []
    desc = ext.get("description") or ""
    # A list entry ("* C5T1: UNIV 1141 - Introduction to Academic Research ...") is not a description:
    # after removing the code and title words, real prose must remain.
    named = set(cov.words(ext.get("title"))) | set(cov.words(title))
    rest = [w for w in re.findall(r"[a-z]{3,}", desc.lower()) if w not in named and w not in cov.STOP]
    if len(desc) < 40 or len(rest) < 5:
        problems.append("no real description (only a course listing or title)")
    elif relevance(title, desc) < MIN_RELEVANCE:
        problems.append("description is not about this course")
    page = norm(page_text)
    d = norm(desc)
    if d and d[:80] not in page:
        # tolerate small extraction differences: most of the description's words must be on the page
        words = set(re.findall(r"[a-z]{4,}", d))
        on_page = sum(w in page for w in words) / max(1, len(words))
        if on_page < 0.9:
            problems.append("description not found verbatim on the page")
    if domain and not host(url).endswith(domain):
        problems.append(f"source is not on {domain}")
    # Transcript codes are often misread (ECON 1130 for ECON 1134), and some schools reuse numbers,
    # so the title must agree either way; a matching number only lowers the bar.
    t_digits = re.findall(r"\d+", code or "")
    p_digits = re.findall(r"\d+", ext.get("code") or "")
    same_num = bool(t_digits and p_digits and t_digits[0] == p_digits[0])
    page_title = ext.get("title") or ""
    title_sim = SequenceMatcher(None, cov.norm_title(title), cov.norm_title(page_title)).ratio()
    title_ok = title_sim >= 0.75 or cov.covered(title, page_title) or (same_num and title_sim >= 0.4)
    if not title_ok:
        problems.append(f'page title "{page_title}" does not match the transcript title')
    return problems

def trim_snippet(desc):
    """Search summaries end mid-sentence ("... an introduction to ...Read more")."""
    d = re.sub(r"\s*\.{3}\s*Read m(ore)?\s*$", "", desc.strip())
    return re.sub(r"\s*\.{3}\s*$", "", d)

# ---------------- orchestration ----------------
def check_result(r, institution, code, title, domain, log, year=None):
    """Excerpt, extract and validate one search result. Returns (record or None, log entry)."""
    url = r["url"]
    # the result title often carries the code and course name ("MATH 1110 - College Algebra - ...")
    text = (r["title"] or "") + "\n" + r["text"]
    win = excerpt(text, code, title, hint_code=r.get("hint_code", ""))
    page_text = r["page_text"]
    arch = r.get("archive_meta")          # the researcher already read an archived copy
    note = f"live page unavailable; read the {arch['archive']} copy from {arch['captured']}" if arch else ""
    if not page_text and (win or url.lower().endswith(".pdf")):
        # Only a summary came back. Fetch the page if the site allows it; never work around a bot check.
        log(f"Fetching {host(url)}")
        fetched, note = fetcher.fetch_text(url)
        if not fetched:
            # Can't read it live (dead link, long crawl delay, bot check): use an archived copy instead.
            fetched, src, when, name = archive.archived_copy(url, year, log)
            if fetched:
                arch = {"archive": name, "captured": when, "archive_url": src}
                note = f"live page unavailable ({note}); read the {name} copy from {when}"
        if fetched:
            text, page_text = (r["title"] or "") + "\n" + fetched, True
            win = excerpt(text, code, title, hint_code=r.get("hint_code", "")) or win
    if not win:
        return None, {"url": url, "outcome": "course not mentioned on page" + (f" ({note})" if note else "")}
    ext = extract(institution, code, title, url, win)
    if not ext.get("found"):
        return None, {"url": url, "outcome": f"not this course: {ext.get('reason', '')}"}
    problems = validate(ext, text, url, domain, code, title)
    if problems:
        named = f" (page names {ext.get('code', '')} {ext.get('title', '')})".replace(" ()", "")
        entry = {"url": url, "outcome": "rejected: " + "; ".join(problems) + named,
                 "description": ext.get("description", "")}
        if not any("does not match the transcript title" in p for p in problems) and ext.get("code"):
            entry["named"] = [ext["code"].strip(), (ext.get("title") or "").strip()]   # right course, no description
        return None, entry
    desc = ext["description"].strip() if page_text else trim_snippet(ext["description"])
    partial = not page_text and (desc != ext["description"].strip() or len(desc) < 250)
    outcome = "accepted" + (" (partial: search summary only" + (f"; {note}" if note else "") + ")" if partial
                            else f" ({note})" if arch else "")
    rec = {"code": ext.get("code") or code, "title": ext.get("title") or title, "description": desc,
           "catalog_year": catalog_year(ext, url), "credits": ext.get("credits") or "",
           "source_url": url, "partial": partial}
    if arch:
        # show the archived copy (the live link may be dead); keep the original address too
        rec.update(original_url=url, **arch)
        if arch["archive_url"].startswith("http"):
            rec["source_url"] = arch["archive_url"]
    return rec, {"url": url, "outcome": outcome}

def fill_template(template, code):
    """Catalog URL pattern + transcript code -> address ("https://catalog.bristolcc.edu/courses/{S}{N}", "AST 192")."""
    m = re.match(r"\s*([A-Za-z]+)[\s\-]*([0-9]+[A-Za-z]?)", code or "")
    if not m:
        return None
    s, n = m.group(1), m.group(2)
    return template.replace("{S}", s.upper()).replace("{s}", s.lower()).replace("{N}", n.upper())

def find_in_archives(institution, code, title, domain, templates, year, log):
    """Archived copies of the course's own catalog page, built from the catalog's URL pattern.
    Catches courses dropped from the live catalog. Returns (record or None, tried entries)."""
    tried = []
    for t in templates:
        url = fill_template(t, code)
        if not url:
            continue
        text, src, when, name = archive.archived_copy(url, year, log)
        if not text:
            tried.append({"url": url, "provider": "archives", "outcome": "no archived copy (Internet Archive, Common Crawl)"})
            continue
        r = {"url": url, "title": "", "text": text, "page_text": True}
        rec, entry = check_result(r, institution, code, title, domain, log, year)
        entry["outcome"] += f" ({name} copy from {when})"
        tried.append({**entry, "provider": "archives"})
        if rec:
            rec.update(original_url=url, archive=name, captured=when)
            if src.startswith("http"):
                rec["source_url"] = src
            return {**rec, "search_provider": "archives"}, tried
    return None, tried

def find_description(institution, code, title, domain, log, templates=(), year=None):
    """Archives first (cheap, exact addresses), then search engine by engine.
    Returns (record or None, candidates tried)."""
    tried = []
    # The standalone archive lookup is off unless ARCHIVE_FIRST=true; the Azure agent reads archived
    # copies itself when a page is dead or blocked.
    if templates and az.ENV.get("ARCHIVE_FIRST", "false").strip().lower() == "true":
        rec, tried = find_in_archives(institution, code, title, domain, templates, year, log)
        if rec:
            log(f"Found in the {rec['archive']} ({rec['captured']})")
            return rec, tried
    providers = active_providers()
    if not providers:
        if tried:
            return None, tried
        raise RuntimeError("No search key set: add TAVILY_API_KEY to app/.env")
    seen = {t["url"] for t in tried}
    for name in providers:
        _, search, queries = PROVIDERS[name]
        for q, depth in queries(institution, code, title, year):
            label = q if isinstance(q, str) else f"{code} {title}"
            log(f"{name.title()}: searching {domain or 'the web'} for {label}")
            try:
                results = search(q, domain, depth)
            except RuntimeError as e:
                tried.append({"url": "", "provider": name, "outcome": str(e)})
                break                                    # this engine is failing; try the next one
            if isinstance(results, tuple):               # (results, note for the search log)
                results, note = results
                tried.append({"url": "", "provider": name, "outcome": note})
            for r in results:
                if not r["url"] or r["url"] in seen:
                    continue
                seen.add(r["url"])
                rec, entry = check_result(r, institution, code, title, domain, log, year)
                tried.append({**entry, "provider": name})
                if rec:
                    log(f"Found on {host(rec['source_url'])} via {name.title()}" + (" (partial)" if rec["partial"] else ""))
                    return {**rec, "search_provider": name}, tried
        if len(providers) > 1 and name != providers[-1]:
            log(f"{name.title()} found nothing acceptable; trying the next search engine")
    return None, tried

def identified_course(tried, min_pages=2):
    """When pages agree on the course but none gave a usable description ("ECON 1134 Introduction to
    Microeconomics" named on 8 pages), return {code, title, pages} for the evaluator to request."""
    from collections import Counter
    names = Counter()
    label = {}
    for t in tried:
        if t.get("named"):
            key = re.sub(r"\s+", "", t["named"][0].upper())
            names[key] += 1
            label.setdefault(key, t["named"])
    if not names:
        return None
    key, n = names.most_common(1)[0]
    return {"code": label[key][0], "title": label[key][1], "pages": n} if n >= min_pages else None
