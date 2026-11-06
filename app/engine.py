"""
Glue between the app and the matcher: catalogs, the evaluation pipeline, and found descriptions.
"""
import os, sys, re, csv, json, glob, statistics, functools, collections

APP = os.path.dirname(os.path.abspath(__file__))
OKF = os.path.dirname(APP)                       # external_okf/: one <School>.jsonl per sending catalog
os.environ.setdefault("TRANSFER_ENV", os.path.join(APP, ".env"))
sys.path.insert(0, os.path.join(OKF, "matcher"))

import coverage as cov                            # noqa: E402  (needs the matcher path above)
import transcript_match as tm                     # noqa: E402
from embed_and_match import load_cache            # noqa: E402
import store                                      # noqa: E402
import finder                                     # noqa: E402

# SNHU course OKF folder (one .md per course); set SNHU_OKF_DIR in app/.env
SNHU_DIR = finder.az.ENV.get("SNHU_OKF_DIR") or os.path.join(os.path.dirname(OKF), "okf_courses")
DAPIP_CSV = os.path.join(os.path.dirname(OKF), "DAPIP_Institutionally_Accredited.csv")
SAMPLE = os.path.join(os.path.dirname(OKF), "sample_transcript_parsed.csv")

def year_of(text):
    m = re.search(r"(19|20)\d\d", str(text or ""))
    return int(m.group(0)) if m else None

def discover_catalogs():
    """{institution lowercased: [{year, label, path, records}]} from external_okf/*.jsonl."""
    out = {}
    for path in sorted(glob.glob(os.path.join(OKF, "*.jsonl"))):
        recs = cov.load_catalog(path)
        if not recs:
            continue
        years = [r.get("catalog_year") for r in recs if r.get("catalog_year")]
        label = max(set(years), key=years.count) if years else "undated"
        out.setdefault(recs[0]["institution"].strip().lower(), []).append(
            {"year": year_of(label), "label": label, "path": path, "records": recs})
    return out

def pick_catalog(options, terms):
    """The catalog whose year is closest to the student's terms (Trevecca 2011 -> 2015-16, not 2025-26)."""
    if len(options) == 1:
        return options[0]
    yrs = [y for y in (year_of(t) for t in terms) if y]
    target = statistics.median(yrs) if yrs else 9999
    return min(options, key=lambda o: abs((o["year"] or 9999) - target))

def dapip_lookup(name):
    """City/state for an institution name (helps the domain search). First exact-name match."""
    if not os.path.exists(DAPIP_CSV):
        return {}
    with open(DAPIP_CSV, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["Institution Name"].strip().lower() == name.strip().lower():
                return r
    return {}

def school_domain(inst, log=print):
    """Official domain for a school: saved value first, else a web search (saved for next time)."""
    domains = store.load("domains.json")
    key = inst.strip().lower()
    if key in domains:
        return domains[key]["domain"]
    meta = dapip_lookup(inst)
    log(f"Looking up the official website for {inst}")
    domain, evidence = finder.find_domain(inst, meta.get("City", ""), meta.get("State", ""))
    domains[key] = {"domain": domain, "evidence": evidence, "method": "web search", "found": store.now()}
    store.save("domains.json", domains)
    return domain

@functools.lru_cache(maxsize=1)
def _catalogs_cached():
    return discover_catalogs()

def url_templates(records, min_share=0.3):
    """Catalog page address patterns, learned from the saved catalog's source links:
    https://catalog.bristolcc.edu/courses/AST111 -> https://catalog.bristolcc.edu/courses/{S}{N}.
    A pattern must cover at least `min_share` of the courses to count."""
    counts, n = collections.Counter(), 0
    for r in records:
        url, s, num = r.get("source_url") or "", r.get("subject") or "", r.get("number") or ""
        if not url or not s:
            continue
        n += 1
        for pat, rep in ((rf"{re.escape(s)}[-_ ]?{re.escape(num)}", "{S}{N}"), (re.escape(s), "{S}")):
            m = re.search(rf"(?<![A-Za-z]){pat}(?![A-Za-z0-9])", url, re.I) if num or rep == "{S}" else None
            if m:
                piece = m.group(0)
                rep2 = rep.replace("{S}", "{s}") if piece.islower() or piece[:len(s)].islower() else rep
                counts[url[:m.start()] + rep2 + url[m.end():]] += 1
                break
    return [t for t, c in counts.most_common(3) if n and c / n >= min_share]

def find_and_store(inst, code, title, log=print, term=""):
    """Run "Find description" for one transcript course and save the outcome (found or not)."""
    domain = school_domain(inst, log)
    templates = [t for opt in _catalogs_cached().get(inst.strip().lower(), []) for t in url_templates(opt["records"])]
    rec, tried = finder.find_description(inst, code, title, domain, log,
                                         templates=list(dict.fromkeys(templates)), year=year_of(term))
    found = store.load("found_descriptions.json")
    found[store.course_key(inst, code, title)] = {
        "institution": inst, "transcript_code": code, "transcript_title": title, "domain": domain,
        "found": rec, "tried": tried, "identified": None if rec else finder.identified_course(tried),
        "searched": store.now()}
    store.save("found_descriptions.json", found)
    return rec, tried

def load_snhu():
    cache = load_cache()
    snhu, S = tm.load_snhu(SNHU_DIR, cache)
    return snhu, S, cache

def evaluate(docs, catalogs, snhu, S, cache):
    """Run the matcher on transcript rows, using any descriptions found by web search."""
    chosen, lookups = {}, {}
    for doc in docs:
        inst = doc["institution_name"].strip().lower()
        if inst in catalogs:
            terms = [c.get("term") for c in json.loads(doc["courses_json"])]
            pick = pick_catalog(catalogs[inst], terms)
            chosen[inst] = pick
            lookups[inst] = (pick["records"], cov.index_catalog(pick["records"]))
    cat_maps = {k: {r["code"].upper(): r for r in v["records"]} for k, v in chosen.items()}

    def lookup(inst, c):
        recs, by_code = lookups[inst.lower()]
        status, match, _ = cov.map_course(c["course_code"], c["course_title"], recs, by_code)
        return status, match["code"] if match else ""

    items = tm.build_items(docs, cat_maps, lookup)
    found = store.load("found_descriptions.json")
    for it in items:
        it["catalog_label"] = chosen[it["inst"].lower()]["label"] if it["inst"].lower() in chosen else ""
        rec = found.get(store.course_key(it["inst"], it["code"], it["title"]), {}).get("found")
        needs = it["handling"].startswith("Not in catalog") or it["mode"] == "title only"
        if rec and needs:
            it.update(handling="", desc=rec["description"], cat_code=rec["code"], cat_title=rec["title"],
                      mode="web search", source_url=rec["source_url"], catalog_year=rec.get("catalog_year", ""),
                      partial=bool(rec.get("partial")), archive=rec.get("archive", ""),
                      captured=rec.get("captured", ""), original_url=rec.get("original_url", ""))
    todo = [it for it in items if not it["handling"]]
    tm.add_candidates(todo, snhu, S, cache)
    tm.judge_all(todo)
    tm.add_flags(items)
    for it in items:
        if it.get("partial"):
            note = "CHECK: verdict based on a partial description (search summary only)"
            it["flags"] = "; ".join(filter(None, [note, it["flags"]]))
    return items, {k: v["label"] for k, v in chosen.items()}
