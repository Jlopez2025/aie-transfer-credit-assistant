"""
Polite page fetcher for search results that come back without page text (Brave returns links and
short summaries only).

Rules: honor robots.txt (including long crawl delays), identify the tool honestly, pause between
requests to the same host, cap the download size, and never work around a bot check: a challenge
page or a block means "not available", and the search summary is used instead.
"""
import io, re, json, time, ssl, urllib.request, urllib.error, urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urlparse

UA = "SNHU-TransferCredit-Prototype/0.1 (internal course-equivalency research)"
MAX_BYTES = 15_000_000
MAX_CRAWL_DELAY = 15         # seconds; sites asking for longer (Acalog asks 120) are not fetched
PAUSE = 1.0                  # seconds between requests to one host
_CTX = ssl.create_default_context()
_robots, _last = {}, {}

CHALLENGE = re.compile(r"awswaf|captcha|cf-chl|challenge-platform|just a moment\.\.\.|attention required|"
                       r"access denied|request unsuccessful", re.I)

class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header"}
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "td", "dd", "dt"}

    def __init__(self):
        super().__init__()
        self.out, self.skip, self.data_json, self.in_json = [], 0, [], False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and ("json" in (a.get("type") or "") or a.get("id") in ("__NUXT_DATA__", "__NEXT_DATA__")):
            self.in_json = True            # app-style catalogs keep course text in embedded JSON
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_json = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.in_json:
            self.data_json.append(data)
        elif not self.skip:
            self.out.append(data)

def json_strings(blob, min_len=40):
    """Readable sentences inside embedded JSON (e.g. a course description in __NUXT_DATA__)."""
    out = []
    for m in re.finditer(r'"((?:[^"\\]|\\.){%d,})"' % min_len, blob):
        s = m.group(1)
        if " " in s and not s.startswith(("http", "/", "data:")):
            try:
                s = json.loads(f'"{s}"')
            except ValueError:
                pass
            out.append(re.sub(r"<[^>]+>", " ", s))
    return out

def html_text(html):
    p = _Text()
    p.feed(html)
    text = "".join(p.out)
    extra = json_strings("".join(p.data_json))
    if extra:
        text += "\n" + "\n".join(extra)
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()

def pdf_text(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)

def robots(host, scheme):
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{scheme}://{host}/robots.txt")
        try:
            req = urllib.request.Request(rp.url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code not in (401, 403):
                rp.allow_all = True        # no robots.txt: crawling allowed
            else:
                rp.disallow_all = True     # robots.txt blocked or server error: don't fetch
        except Exception:
            rp.disallow_all = True         # unreachable: don't guess
        _robots[host] = rp
    return _robots[host]

def fetch_text(url):
    """Return (text, note). text is None when the page can't or shouldn't be fetched; note says why."""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        return None, "not a web page"
    rp = robots(u.hostname, u.scheme)
    if not rp.can_fetch(UA, url):
        return None, "robots.txt disallows fetching"
    delay = rp.crawl_delay(UA) or 0
    if delay > MAX_CRAWL_DELAY:
        return None, f"site asks for a {int(delay)} s crawl delay; used the search summary"
    wait = max(delay, PAUSE) - (time.time() - _last.get(u.hostname, 0))
    if wait > 0:
        time.sleep(wait)
    _last[u.hostname] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read(MAX_BYTES + 1)
            status = r.status
    except urllib.error.HTTPError as e:
        return None, f"site returned HTTP {e.code}"
    except Exception as e:
        return None, f"fetch failed: {str(e)[:60]}"
    if len(data) > MAX_BYTES:
        return None, "file too large"
    if "pdf" in ctype or data[:5] == b"%PDF-":
        try:
            return pdf_text(data), "fetched PDF"
        except Exception as e:
            return None, f"PDF unreadable: {str(e)[:60]}"
    html = data.decode("utf-8", "replace")
    if status == 202 or (len(html) < 20000 and CHALLENGE.search(html)):
        return None, "site shows a bot check; not bypassed"
    return html_text(html), "fetched page"
