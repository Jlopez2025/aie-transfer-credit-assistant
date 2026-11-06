"""
Archived copies of catalog pages: the Internet Archive (Wayback Machine) and Common Crawl.

Both are free and their terms allow research use and keeping what you retrieve (respecting
the original publishers' copyright), unlike the search APIs that forbid storing results.

Used two ways by finder.py:
  - a page found by search that can't be read live (dead link, long crawl delay, bot check):
    read the archived copy of that exact address instead;
  - a course dropped from the live catalog: build its address from the catalog's URL pattern
    (Bristol: catalog.bristolcc.edu/courses/AST192) and look for an archived copy.

Only exact-address lookups are made (fast, light on the archives); captures closest to the term
the student took the course are preferred. Common Crawl's index is often overloaded, so it is
best-effort: two failures in a row and it is skipped for the rest of the session.
"""
import io, re, json, gzip, time, urllib.request, urllib.error, urllib.parse
import fetcher

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_DATA = "https://data.commoncrawl.org/"
PAUSE = 1.0
_last = {}
_cc = {"crawls": None, "failures": 0}

def _get(url, timeout=30, headers=None):
    host = urllib.parse.urlparse(url).hostname
    wait = PAUSE - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": fetcher.UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=fetcher._CTX) as r:
        return r.read(fetcher.MAX_BYTES + 1), r.headers.get("Content-Type", "")

def _bare(url):
    return re.sub(r"^https?://", "", url)

def _closest(rows, year, ts_of):
    target = f"{year}0601" if year else "99999999"
    return sorted(rows, key=lambda r: abs(int(ts_of(r)[:8]) - int(target)))

def _text(data, ctype):
    if len(data) > fetcher.MAX_BYTES:
        return None
    if "pdf" in ctype or data[:5] == b"%PDF-":
        try:
            return fetcher.pdf_text(data)
        except Exception:
            return None
    return fetcher.html_text(data.decode("utf-8", "replace"))

# ---------------- Internet Archive ----------------
def wayback(url, year=None):
    """(text, archived url, capture date) for the capture of this exact address closest to `year`."""
    try:
        raw, _ = _get(WAYBACK_CDX + "?" + urllib.parse.urlencode(
            {"url": _bare(url), "output": "json", "fl": "timestamp,statuscode,mimetype", "limit": 200}), timeout=40)
        rows = json.loads(raw.decode("utf-8") or "[]")[1:]
    except Exception:
        return None, "", ""
    rows = [r for r in rows if r[1] == "200"]
    for ts, _, _ in _closest(rows, year, lambda r: r[0])[:2]:
        archived = f"https://web.archive.org/web/{ts}/{url}"
        try:
            data, ctype = _get(f"https://web.archive.org/web/{ts}id_/{url}", timeout=60)   # id_ = original bytes
        except Exception:
            continue
        text = _text(data, ctype)
        if text:
            return text, archived, f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    return None, "", ""

# ---------------- Common Crawl ----------------
def _crawls():
    if _cc["crawls"] is None:
        try:
            raw, _ = _get(CC_COLLINFO, timeout=20)
            _cc["crawls"] = json.loads(raw.decode("utf-8"))
        except Exception:
            _cc["crawls"] = []
    return _cc["crawls"]

def _warc_body(record):
    """HTTP response body from one gzipped WARC record."""
    data = gzip.decompress(record)
    parts = data.split(b"\r\n\r\n", 2)              # WARC headers | HTTP headers | body
    if len(parts) < 3:
        return b"", ""
    ctype = next((l.split(b":", 1)[1].strip().decode("latin-1") for l in parts[1].split(b"\r\n")
                  if l.lower().startswith(b"content-type:")), "")
    return parts[2], ctype

def common_crawl(url, year=None, max_crawls=4):
    """(text, source note, capture date) from the Common Crawl crawl nearest `year` holding this address."""
    if _cc["failures"] >= 2:
        return None, "", ""
    crawls = _crawls()
    if year:
        crawls = sorted(crawls, key=lambda c: abs(int(re.search(r"(\d{4})", c["id"]).group(1)) - year))
    for c in crawls[:max_crawls]:
        try:
            raw, _ = _get(c["cdx-api"] + "?" + urllib.parse.urlencode({"url": _bare(url), "output": "json"}), timeout=20)
            _cc["failures"] = 0
        except urllib.error.HTTPError as e:
            if e.code == 404:                        # not in this crawl
                _cc["failures"] = 0
                continue
            _cc["failures"] += 1
            if _cc["failures"] >= 2:
                return None, "", ""
            continue
        except Exception:
            _cc["failures"] += 1
            if _cc["failures"] >= 2:
                return None, "", ""
            continue
        rows = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
        rows = [r for r in rows if r.get("status") == "200"]
        if not rows:
            continue
        r = rows[0]
        start, length = int(r["offset"]), int(r["length"])
        try:
            rec, _ = _get(CC_DATA + r["filename"], timeout=60,
                          headers={"Range": f"bytes={start}-{start + length - 1}"})
            body, ctype = _warc_body(rec)
        except Exception:
            continue
        text = _text(body, ctype or r.get("mime", ""))
        if text:
            ts = r.get("timestamp", "")
            return text, f"Common Crawl {c['id']} copy of {url}", f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    return None, "", ""

def archived_copy(url, year=None, log=print):
    """Try the Internet Archive, then Common Crawl. Returns (text, source url or note, capture date, archive name)."""
    log(f"Checking the Internet Archive for {fetcher.urlparse(url).hostname}")
    text, src, when = wayback(url, year)
    if text:
        return text, src, when, "Internet Archive"
    log("Checking Common Crawl")
    text, src, when = common_crawl(url, year)
    if text:
        return text, src, when, "Common Crawl"
    return None, "", "", ""
