"""
Claude web-research provider for "Find description", modeled on DATUM's internet research tool.

One Claude request runs Anthropic's server-side web search + web fetch tools, so the whole
search-and-read loop happens on Anthropic's side. Claude is a finder, not the judge: the pages
it fetched come back as full text (PDFs as base64), and finder.py runs its own extraction and
checks on exactly that text (verbatim on the page, on the school's domain, about this course).
Nothing Claude writes is trusted as a description.

Design choices carried over from DATUM:
  - hard caps via max_uses on both tools; a smaller soft budget in the prompt
  - allowed_callers ["direct"]: no dynamic-filtering sandbox (latency over token savings)
  - pause_turn resumed by sending the paused assistant turn back; content from every turn kept
  - its own credentials (ANTHROPIC_API_KEY + ANTHROPIC_API_URL in app/.env), base URL passed
    explicitly so the process never inherits someone else's ANTHROPIC_BASE_URL
Added for this app: both tools restricted to the school's official domain.

Cost: web search is $10 per 1,000 searches plus tokens; web fetch is tokens only. With the caps
below, worst case per course is about 4 searches and 4 fetches of up to 20k tokens each.
"""
import os, re, base64
import azure_client as az        # only for reading app/.env (no Azure calls here)
import fetcher

MODEL = az.ENV.get("CLAUDE_RESEARCH_MODEL") or "claude-opus-5-5"
DEFAULT_URL = "https://api.anthropic.com"
MAX_SEARCHES = 4
MAX_FETCHES = 4
MAX_FETCH_CONTENT_TOKENS = 20_000
MAX_PAUSE_TURN_RESUMES = 5
MAX_TOKENS = 16_000
TIMEOUT_S = 300.0

SYSTEM = """\
You are a research agent that finds college course catalog entries. You receive one course from a
student's transcript and find the official catalog page that describes it. You are a reporter, not
an evaluator: you never judge transfer equivalency or add your own descriptions.

Today's date is {today}.

# Process
Work in at most two rounds of search-then-read, then report:
1. Run 1-2 searches in parallel: the course code with the title, and the title alone. Transcript
   codes are often misread or the school has renumbered (ECON 1130 on a transcript may be ECON 1134
   in the catalog; MUSC 1110 "College Algebra" may really be MATH 1110), so trust the title more
   than the code.
2. Fetch the most promising pages in one parallel batch: a catalog course page, a catalog subject
   page that lists the course with its description, or a catalog PDF. Prefer the catalog edition
   closest to the term the student took the course; older archived catalogs are fine.
3. One more search-and-fetch round only if the description is still missing.

# Report
If you found the description, report exactly these lines, then the description quoted word for
word from the fetched page:
COURSE CODE: <code as printed in the catalog>
COURSE TITLE: <title as printed>
CATALOG YEAR: <edition, if the page states one>
SOURCE URL: <the fetched page that contains the description>
DESCRIPTION: <quoted from the page>

If you did not find it, start with NOT FOUND: and say what you did find (for example, the course
listed without a description, or only in a program requirement list).
"""

def available():
    return bool(az.ENV.get("ANTHROPIC_API_KEY", "").strip())

def _client():
    import anthropic                  # imported lazily so the app runs without the SDK
    # A key that isn't scoped to one workspace must name the workspace on every request.
    workspace = az.ENV.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    return anthropic.Anthropic(api_key=az.ENV["ANTHROPIC_API_KEY"].strip(),
                               base_url=(az.ENV.get("ANTHROPIC_API_URL") or DEFAULT_URL).strip(),
                               default_headers={"anthropic-workspace-id": workspace} if workspace else None,
                               timeout=TIMEOUT_S, max_retries=2)

def _tools(domain):
    search = {"type": "web_search_20260318", "name": "web_search", "max_uses": MAX_SEARCHES,
              "allowed_callers": ["direct"]}
    fetch = {"type": "web_fetch_20260318", "name": "web_fetch", "max_uses": MAX_FETCHES,
             "max_content_tokens": MAX_FETCH_CONTENT_TOKENS, "citations": {"enabled": True},
             "allowed_callers": ["direct"]}
    if domain:
        search["allowed_domains"] = [domain]
        fetch["allowed_domains"] = [domain]
    return [search, fetch]

def _create(client, base_url, **kw):
    # Server-side refusal fallback where it exists (Claude API); other endpoints (e.g. Foundry)
    # don't take the parameter, and a declined request just returns no result.
    if base_url.rstrip("/") == DEFAULT_URL and MODEL in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"):
        return client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
    return client.messages.create(**kw)

def _document_text(doc):
    src = (doc or {}).get("source") or {}
    if src.get("type") == "text":
        return src.get("data") or ""
    if src.get("type") == "base64" and "pdf" in (src.get("media_type") or ""):
        try:
            return fetcher.pdf_text(base64.b64decode(src.get("data") or ""))
        except Exception:
            return ""
    return ""

def research(institution, code, title, domain, year=None, log=print):
    """Run one research request. Returns (results, report) where results are fetched pages as
    {url, title, text, page_text: True, hint_code} for finder.check_result, and report is Claude's
    final text plus usage, for the search log."""
    import datetime
    import anthropic
    client = _client()
    base_url = (az.ENV.get("ANTHROPIC_API_URL") or DEFAULT_URL).strip()
    term = f" (taken {year})" if year else ""
    user = (f"Institution: {institution}\nOfficial website: {domain or 'unknown'}\n"
            f"Transcript course: {code} {title}{term}\n\nFind this course's catalog description.")
    messages = [{"role": "user", "content": user}]
    blocks = []
    usage = {"searches": 0, "fetches": 0, "input_tokens": 0, "output_tokens": 0}
    try:
        for _ in range(MAX_PAUSE_TURN_RESUMES + 1):
            resp = _create(client, base_url, model=MODEL, max_tokens=MAX_TOKENS,
                           system=SYSTEM.format(today=datetime.date.today().isoformat()),
                           output_config={"effort": "low"}, tools=_tools(domain), messages=messages)
            blocks.extend(resp.content)
            u = resp.usage
            usage["input_tokens"] += u.input_tokens or 0
            usage["output_tokens"] += u.output_tokens or 0
            stu = getattr(u, "server_tool_use", None)
            if stu:
                usage["searches"] += getattr(stu, "web_search_requests", 0) or 0
                usage["fetches"] += getattr(stu, "web_fetch_requests", 0) or 0
            if resp.stop_reason == "refusal":
                return [], {"text": "Claude declined this request.", "usage": usage}
            if resp.stop_reason != "pause_turn":
                break
            messages = messages[:1] + [{"role": "assistant", "content": blocks}]   # resume the paused turn
    except anthropic.AuthenticationError:
        raise RuntimeError("Claude search failed: the Anthropic API key was rejected")
    except anthropic.PermissionDeniedError as e:
        raise RuntimeError(f"Claude search failed: not permitted ({e.message[:120]})")
    except anthropic.BadRequestError as e:
        raise RuntimeError(f"Claude search failed: {e.message[:200]}")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude search failed: HTTP {e.status_code}")
    except anthropic.APIConnectionError:
        raise RuntimeError("Claude search failed: could not reach the Anthropic endpoint")

    report = "".join(b.text for b in blocks if b.type == "text").strip()
    m = re.search(r"COURSE CODE:\s*(.+)", report)
    hint = m.group(1).strip() if m else ""
    src = re.search(r"SOURCE URL:\s*(\S+)", report)
    preferred = src.group(1).strip() if src else ""
    results = []
    for b in blocks:
        if b.type != "web_fetch_tool_result":
            continue
        c = b.to_dict().get("content") or {}
        if c.get("type") != "web_fetch_result":
            continue                                   # fetch error (blocked by robots.txt, 404, ...)
        doc = c.get("content") or {}
        text = _document_text(doc)
        if text:
            results.append({"url": c.get("url", ""), "title": doc.get("title") or "", "text": text,
                            "page_text": True, "hint_code": hint})
    results.sort(key=lambda r: r["url"] != preferred)  # the page Claude cited first
    log(f"Claude: {usage['searches']} searches, {usage['fetches']} fetches, "
        f"{usage['input_tokens']:,} input tokens")
    return results, {"text": report, "usage": usage}
