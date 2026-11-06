"""
Azure OpenAI research agent for "Find description": a DATUM-style researcher (plan, search, read,
one more round if needed, report) driven by our own Azure OpenAI deployment through function
calling. It does not use Azure's Bing-based web search, whose terms forbid building a database
from results or fetching the result links; instead its tools are the ones this app may store
results from:

  search_catalog(query)   Tavily, limited to the school's official domain
  read_page(url)          page text: Tavily's copy, else our polite fetcher (robots.txt, no bot-check
                          workarounds), else an Internet Archive / Common Crawl copy
  read_archived(url)      the archived copy closest to the term the course was taken
  finish(...)             the agent's report: which page holds the description, and the code/title

The agent is a finder, not the judge: every page it read goes back to finder.check_result, which
extracts the description itself and applies the usual checks. Nothing the agent writes is used as
a description. Budgets are hard limits in code (searches, reads, turns), not just prompt advice.

Set AZURE_OPENAI_RESEARCH_DEPLOYMENT in app/.env to use a stronger deployment than the chat one.
"""
import json
import azure_client as az
import fetcher
import archive

DEPLOYMENT = az.ENV.get("AZURE_OPENAI_RESEARCH_DEPLOYMENT") or az.CHAT_DEP
MAX_SEARCHES = 4
MAX_READS = 6
MAX_TURNS = 8
PAGE_CHARS = 6000          # text the agent sees per page; the full page is kept for checking

SYSTEM = """\
You find the official catalog description of one college course from a student's transcript.
You are a researcher, not an evaluator: never judge transfer equivalency and never write a
description yourself.

Transcript codes are often misread or the school has renumbered (ECON 1130 on a transcript may be
ECON 1134 in the catalog; "MUSC 1110 College Algebra" may really be MATH 1110), so trust the title
more than the code.

Process (at most two rounds, then finish):
1. Plan 2 searches for distinct angles (code with title; title alone) and run them in parallel
   with search_catalog. Queries are short and plain.
2. Read the most promising pages with read_page, in parallel: a catalog course page, a catalog
   subject page that lists the course with its description, or a catalog PDF. Prefer the catalog
   edition closest to the term the course was taken.
3. If a page is unreadable or a link is dead, try read_archived on it. Only if the description is
   still missing, run one more round of searches and reads targeting the gap.
4. Call finish exactly once. Report found=false honestly if no page you read contains the
   description; say what you did find (for example, the course listed without a description).
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_catalog",
        "description": "Search the school's official website. Returns result titles, URLs and short snippets.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_page",
        "description": "Read a page or PDF from the school's website, focused on the passage about the course.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "read_archived",
        "description": "Read an archived copy of a page (Internet Archive, Common Crawl) near the term the course was taken.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Report the result. Call once, at the end.",
        "parameters": {"type": "object", "properties": {
            "found": {"type": "boolean"},
            "source_url": {"type": "string", "description": "page that contains the description"},
            "course_code": {"type": "string", "description": "code as printed in the catalog"},
            "course_title": {"type": "string"},
            "catalog_start_year": {"type": "integer", "description": "first year of the catalog edition, e.g. 2022 for 2022-23; 0 if unknown"},
            "notes": {"type": "string", "description": "what you found, in one or two sentences"}},
            "required": ["found", "notes"]}}},
]

def available():
    return bool(az.ENV.get("AZURE_OPENAI_API_KEY")) and bool(az.ENV.get("TAVILY_API_KEY"))

def research(institution, code, title, domain, year=None, log=print):
    """Returns (results, report): every page the agent read as {url, title, text, page_text, hint_code}
    for finder.check_result, and a report dict for the search log."""
    import finder                        # finder imports this module; import lazily to avoid a cycle
    pages, snippets = {}, {}             # url -> full text; url -> search-result text
    used = {"searches": 0, "reads": 0, "tokens": 0}
    term = f" (taken {year})" if year else ""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Institution: {institution}\nOfficial website: {domain or 'unknown'}\n"
                                            f"Transcript course: {code} {title}{term}"}]

    archived = {}                        # url -> archive details, when the agent read an archived copy

    def focus(text):
        win = finder.excerpt(text, code, title, before=600, after=PAGE_CHARS - 600)
        return finder.redact(win or text[:PAGE_CHARS])     # phone/email trip SNHU's PII filter

    def search_catalog(query):
        if used["searches"] >= MAX_SEARCHES:
            return "Search budget used up. Read pages or finish."
        used["searches"] += 1
        log(f"Azure agent: searching {domain or 'the web'} for {query}")
        try:
            rs = finder.tavily_results(f"{institution} {query}", domain, "advanced")
        except RuntimeError as e:
            return f"Search failed: {e}"
        out = []
        for r in rs[:6]:
            snippets[r["url"]] = r
            out.append({"url": r["url"], "title": r["title"], "snippet": (r["text"] or "")[:300]})
        return json.dumps(out) if out else "No results."

    def read_page(url):
        if used["reads"] >= MAX_READS:
            return "Reading budget used up. Finish now."
        if domain and not finder.host(url).endswith(domain):
            return f"Only pages on {domain} can be read."
        used["reads"] += 1
        log(f"Azure agent: reading {finder.host(url)}")
        hit = snippets.get(url)
        text = hit["text"] if hit and hit["page_text"] else None
        note = ""
        if not text:
            text, note = fetcher.fetch_text(url)
        if not text:
            text, src, when, name = archive.archived_copy(url, year, log)
            if not text:
                return f"Could not read the page ({note}) and no archived copy was found."
            archived[url] = {"archive": name, "captured": when, "archive_url": src}
            note = f"live page unavailable; this is the {name} copy from {when}"
        pages[url] = text
        return (f"[{note}]\n" if note else "") + focus(text)

    def read_archived(url):
        if used["reads"] >= MAX_READS:
            return "Reading budget used up. Finish now."
        if domain and not finder.host(url).endswith(domain):
            return f"Only pages on {domain} can be read."
        used["reads"] += 1
        text, src, when, name = archive.archived_copy(url, year, log)
        if not text:
            return "No archived copy found."
        archived[url] = {"archive": name, "captured": when, "archive_url": src}
        pages[url] = text
        return f"[{name} copy from {when}]\n" + focus(text)

    handlers = {"search_catalog": search_catalog, "read_page": read_page, "read_archived": read_archived}
    report = {"found": False, "notes": "The agent stopped without reporting."}
    for _ in range(MAX_TURNS):
        try:
            msg, usage = az.chat_tools(messages, TOOLS, deployment=DEPLOYMENT)
        except RuntimeError as e:
            raise RuntimeError(f"Azure research failed: {str(e)[:160]}")
        used["tokens"] += usage.get("total_tokens", 0)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls} if calls
                        else {"role": "assistant", "content": msg.get("content") or ""})
        if not calls:
            break
        done = False
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            if name == "finish":
                report = {**report, **args}
                result, done = "Recorded.", True
            elif name in handlers and args.get("query" if name == "search_catalog" else "url"):
                result = handlers[name](args["query" if name == "search_catalog" else "url"])
            else:
                result = "Unknown tool or missing argument."
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if done:
            break

    hint = (report.get("course_code") or "").strip()
    preferred = (report.get("source_url") or "").strip()
    if not report.get("notes") or report["notes"].startswith("The agent stopped"):
        last = next((m.get("content") for m in reversed(messages) if m["role"] == "assistant" and m.get("content")), "")
        report["notes"] = last or "The agent stopped without reporting (its last reply may have been filtered)."
    results = [{"url": u, "title": "", "text": t, "page_text": True, "hint_code": hint,
                "archive_meta": archived.get(u)} for u, t in pages.items()]
    results.sort(key=lambda r: r["url"] != preferred)        # the page the agent named first
    log(f"Azure agent: {used['searches']} searches, {used['reads']} pages read, {used['tokens']:,} tokens")
    return results, {"report": report, "usage": used}
