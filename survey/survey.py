"""
Catalog scrapeability survey: for every active, degree-granting institution in IPEDS, find its
online course catalog, identify the catalog platform, and check whether automated reading is
allowed and practical. Light on schools: robots.txt plus 1-3 pages per site, honest user agent.

Buckets
  ready          CourseLeaf, SmartCatalogIQ or Coursedog (scrapers exist), allowed, no bot check, short crawl delay
  needs_adapter  another platform or a custom site, allowed and readable, but no scraper yet
  slow           allowed, but robots.txt asks for a crawl delay over 15 s (Acalog asks 120 s)
  blocked        robots.txt disallows the catalog, or a bot check / access denied
  pdf_only       the catalog link is a PDF
  not_found      no catalog located on the website
  unreachable    the website itself did not respond

Usage:
  python survey.py HD2023.csv DAPIP_Institutionally_Accredited.csv out.csv [--limit N] [--workers 24]
Resumable: schools already in out.csv are skipped.
"""
import csv, re, sys, time, ssl, argparse, threading, urllib.request, urllib.error, urllib.robotparser
from urllib.parse import urlparse, urljoin
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "SNHU-TransferCredit-Prototype/0.1 (catalog availability survey)"
CTX = ssl.create_default_context()
TIMEOUT = 15
MAX_BYTES = 2_000_000
MAX_DELAY = 15

PLATFORMS = [   # (name, pattern, where to look: "any" = final URL or page HTML, "url" = final URL only)
    ("Acalog", r"acalog|modern campus catalog|content\.php\?catoid=|preview_course(_nopop)?\.php|navoid=", "any"),
    ("CourseLeaf", r"courseleaf|leepfrog|/courses-az/|class=\"courseblock", "any"),
    ("SmartCatalogIQ", r"smartcatalogiq", "any"),
    ("Coursedog", r"coursedog", "any"),
    ("Kuali", r"kuali\.co|kuali catalog|app\.kuali", "any"),
    ("Clean Catalog", r"cleancatalog|clean catalog", "any"),
    # these names appear in ordinary pages (portal links), so only the address counts
    ("Banner", r"bwckctlg|p_disp_dyn_ctlg|bwckschd", "url"),
    ("Ellucian Self-Service", r"/student/courses|selfservice\.", "url"),
    ("PeopleSoft", r"/psc/|/psp/", "url"),
]
# Challenge-page markers only (a reCAPTCHA script on a normal page is not a bot check).
CHALLENGE = re.compile(r"awswaf|aws-waf-token|cf-chl|challenge-platform|just a moment\.\.\.|attention required! \| cloudflare|"
                       r"_incapsula_resource|request unsuccessful\. incapsula|px-captcha|perimeterx|"
                       r"access denied.{0,200}reference #", re.I | re.S)
# Pages on a catalog.* host that are not the academic catalog (e.g. Canvas Catalog course storefronts)
NOT_ACADEMIC = re.compile(r"canvas catalog|catalog-cdn-[a-z0-9-]+\.inscloudgate\.net", re.I)   # not "instructure.com": many sites link Canvas
PLATFORM_HOSTS = re.compile(r"smartcatalogiq\.com|acalog|coursedog\.com|courseleaf|kuali\.co|cleancatalog\.com", re.I)
CATALOG_WORDS = re.compile(r"(academic|course|college|undergraduate|university)?\s*(catalog|catalogue|bulletin)|course descriptions", re.I)
NOT_CATALOG = re.compile(r"library|librar|worldcat|primo|opac|alma|ebsco|lib\.", re.I)

_robots, _lock = {}, threading.Lock()

def get(url):
    """(status, final url, content-type, text) or (None, url, '', error)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read(MAX_BYTES) if "pdf" not in ctype else b""
            return r.status, r.geturl(), ctype, data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, url, e.headers.get("Content-Type", "") if e.headers else "", body
    except Exception as e:
        return None, url, "", f"{type(e).__name__}: {str(e)[:80]}"

def robots_for(url):
    u = urlparse(url)
    key = f"{u.scheme}://{u.hostname}"
    with _lock:
        if key in _robots:
            return _robots[key]
    rp = urllib.robotparser.RobotFileParser()
    status, _, _, text = get(key + "/robots.txt")
    rp.fetch_status = status                     # 401/403 here usually means bot protection, not a policy
    if status == 200:
        rp.parse(text.splitlines())
    elif status in (401, 403):
        rp.disallow_all = True
    else:
        rp.allow_all = True                      # no robots.txt (404) or unreachable: no stated rule
    with _lock:
        _robots[key] = rp
    return rp

def allowed(url):
    rp = robots_for(url)
    return rp.can_fetch(UA, url), (rp.crawl_delay(UA) or 0)

def refused_note(url, what):
    st = getattr(robots_for(url), "fetch_status", None)
    if st in (401, 403):
        return f"robots.txt request refused (HTTP {st}), likely bot protection"
    return f"robots.txt disallows the {what}"

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []
    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None

def catalog_link(base, html):
    p = Links()
    try:
        p.feed(html)
    except Exception:
        return None
    best = None
    for href, text in p.links:
        blob = f"{text} {href}"
        if not CATALOG_WORDS.search(blob) or NOT_CATALOG.search(blob) or href.startswith(("mailto:", "tel:", "#")):
            continue
        score = 2 if re.search(r"academic catalog|course catalog|catalog", text, re.I) else 1
        if re.search(r"catalog\.", href, re.I):
            score += 1
        if PLATFORM_HOSTS.search(href):
            score += 3                           # a link straight to a catalog platform
        if not best or score > best[0]:
            best = (score, urljoin(base, href))
    return best[1] if best else None

def platform(url, html, ctype):
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return "PDF"
    blob = (url + " " + html[:400_000]).lower()
    for name, pat, where in PLATFORMS:
        if re.search(pat, url.lower() if where == "url" else blob, re.I):
            return name
    return "Custom/other"

def onward_link(base, html):
    """On a catalog landing page, the link to the real catalog: a catalog platform or a catalog PDF."""
    p = Links()
    try:
        p.feed(html)
    except Exception:
        return None
    for href, text in p.links:
        full = urljoin(base, href)
        if PLATFORM_HOSTS.search(full):
            return full
    for href, text in p.links:
        full = urljoin(base, href)
        if full.lower().split("?")[0].endswith(".pdf") and CATALOG_WORDS.search(f"{text} {href}"):
            return full
    return None

def registrable(host):
    parts = (host or "").lower().split(".")
    if len(parts) >= 3 and parts[-1] == "us":     # state-style domains like cc.mn.us keep more labels
        return ".".join(parts[-4:])
    return ".".join(parts[-2:])

def check_catalog(url):
    """Classify one candidate catalog URL. Returns dict or None if it isn't a usable page."""
    ok, delay = allowed(url)
    if not ok:
        return {"catalog_url": url, "robots_allowed": "no", "crawl_delay": delay, "bot_check": "", "platform": "",
                "status": "", "bucket": "blocked", "note": refused_note(url, "catalog")}
    status, final, ctype, html = get(url)
    if status is None:
        return None
    plat = platform(final, html if status == 200 else "", ctype)
    challenge = status in (202, 403, 429, 503) and bool(CHALLENGE.search(html or "")) or \
        (status == 200 and len(html) < 20000 and bool(CHALLENGE.search(html)))
    row = {"catalog_url": final, "robots_allowed": "yes", "crawl_delay": delay, "bot_check": "yes" if challenge else "no",
           "platform": plat, "status": status, "_html": html if status == 200 else ""}
    if challenge or status in (401, 403):
        row.update(bucket="blocked", note="bot check" if challenge else f"HTTP {status}")
    elif status != 200:
        return None
    elif plat == "Custom/other" and (not CATALOG_WORDS.search(html[:300_000]) or NOT_ACADEMIC.search(html[:300_000])):
        return None                              # catalog.x.edu redirecting home, or a Canvas course storefront
    elif plat == "PDF":
        row.update(bucket="pdf_only", note="catalog is a PDF")
    elif delay > MAX_DELAY:
        row.update(bucket="slow", note=f"crawl delay {int(delay)} s")
    elif plat in ("CourseLeaf", "SmartCatalogIQ"):
        row.update(bucket="ready", note="scraper exists")
    elif plat == "Coursedog":
        row.update(bucket="ready", note="Bristol scraper (Coursedog); verify per site")
    else:
        row.update(bucket="needs_adapter", note="readable; no scraper yet")
    return row

def survey_site(website):
    site = website.strip()
    if not site:
        return {"bucket": "unreachable", "note": "no website in IPEDS"}
    if not site.startswith("http"):
        site = "https://" + site
    host = urlparse(site).hostname or ""
    dom = registrable(host)
    # 1) the usual catalog subdomains
    for sub in ("catalog", "catalogs", "bulletin", "ecatalog"):
        r = check_catalog(f"https://{sub}.{dom}/")
        if r:
            return follow(r)
    # 2) a catalog link on the homepage
    ok, _ = allowed(site)
    if not ok:
        return {"bucket": "blocked", "note": refused_note(site, "homepage"), "robots_allowed": "no"}
    status, final, ctype, html = get(site)
    if status is None:
        return {"bucket": "unreachable", "note": html}
    if status in (401, 403) or CHALLENGE.search(html[:20000] if len(html) < 20000 else ""):
        return {"bucket": "blocked", "note": f"homepage HTTP {status}" + (" (bot check)" if CHALLENGE.search(html or "") else "")}
    link = catalog_link(final, html)
    if link:
        r = check_catalog(link)
        if r:
            return follow(r)
    # 3) common catalog paths on the main site
    for path in ("/catalog", "/catalogs", "/academics/catalog", "/bulletin"):
        r = check_catalog(urljoin(final, path))
        if r:
            return follow(r)
    return {"bucket": "not_found", "note": "no catalog found at common addresses or linked from the homepage"}

def follow(r):
    """A generic landing page often just links to the real catalog; follow that one link."""
    if r.get("platform") == "Custom/other" and r.get("_html"):
        nxt = onward_link(r["catalog_url"], r["_html"])
        if nxt and nxt.rstrip("/") != r["catalog_url"].rstrip("/"):
            r2 = check_catalog(nxt)
            if r2:
                r2["note"] = (r2.get("note") or "") + f" (linked from {r['catalog_url']})"
                return r2
    return r

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hd"); ap.add_argument("dapip"); ap.add_argument("out")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()

    hd = [r for r in csv.DictReader(open(a.hd, encoding="utf-8-sig", errors="replace"))
          if r["DEGGRANT"] == "1" and r["CYACTIVE"] == "1"]
    acc_ids, acc_ope = set(), set()
    for r in csv.DictReader(open(a.dapip, encoding="utf-8-sig")):
        acc_ids.update(x.strip() for x in re.split(r"[;,]", r.get("IPEDS UnitID(s)") or "") if x.strip())
        if (r.get("OPE ID") or "").strip():
            acc_ope.add(r["OPE ID"].strip()[:6])
    if a.limit:
        hd = hd[:a.limit]

    cols = ["UNITID", "Institution", "State", "Sector", "Institutionally accredited (DAPIP)", "Website", "Catalog URL",
            "Platform", "robots.txt allows", "Crawl delay (s)", "Bot check", "HTTP status", "Bucket", "Note"]
    done = set()
    try:
        done = {r["UNITID"] for r in csv.DictReader(open(a.out, encoding="utf-8-sig"))}
    except FileNotFoundError:
        with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(cols)
    todo = [r for r in hd if r["UNITID"] not in done]
    by_site = {}
    for r in todo:
        by_site.setdefault(registrable(urlparse(("https://" + r["WEBADDR"].strip()) if r["WEBADDR"].strip() and not r["WEBADDR"].startswith("http") else r["WEBADDR"]).hostname or r["UNITID"]), []).append(r)
    print(f"{len(hd)} degree-granting active schools; {len(done)} done; {len(todo)} to survey on {len(by_site)} distinct sites", flush=True)

    out_lock = threading.Lock()
    t0, n = time.time(), 0
    def work(item):
        dom, rows = item
        return rows, survey_site(rows[0]["WEBADDR"])
    with ThreadPoolExecutor(max_workers=a.workers) as ex, open(a.out, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        futs = [ex.submit(work, it) for it in by_site.items()]
        for fut in as_completed(futs):
            try:
                rows, res = fut.result()
            except Exception as e:
                continue
            with out_lock:
                for r in rows:
                    acc = "yes" if r["UNITID"] in acc_ids or r["OPEID"].strip()[:6] in acc_ope else "no"
                    w.writerow([r["UNITID"], r["INSTNM"], r["STABBR"], r["SECTOR"], acc, r["WEBADDR"],
                                res.get("catalog_url", ""), res.get("platform", ""), res.get("robots_allowed", ""),
                                res.get("crawl_delay", ""), res.get("bot_check", ""), res.get("status", ""),
                                res["bucket"], res.get("note", "")])
                f.flush()
                n += 1
                if n % 100 == 0:
                    print(f"  {n}/{len(by_site)} sites ({time.time()-t0:.0f}s)", flush=True)
    print(f"done: {n} sites in {time.time()-t0:.0f}s -> {a.out}", flush=True)

if __name__ == "__main__":
    main()
