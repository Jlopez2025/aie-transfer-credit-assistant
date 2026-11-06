"""
Match parsed transcript courses to SNHU courses.

For each transcript course:
  1. Triage: AP/exam credit, withdrawn, or failed lines are set aside (not matched).
  2. Look up the sending course's catalog description (via the coverage file's catalog code).
  3. Embed and take the top-3 SNHU undergraduate candidates.
  4. Strict verdict on candidate 1; top-3 pick; strict re-judge if the pick was 2 or 3;
     keep the better strict verdict (same scheme as the BHCC FINAL run).
Schools without a catalog get title-only embedding suggestions and no LLM verdict.

Usage:
  python transcript_match.py --transcripts sample_transcript_parsed.csv --coverage Transcript_Catalog_Coverage.csv
      --snhu <okf_courses dir> --out-csv results.csv --out-md results.md  name=catalog.jsonl [name=...]

The app imports the step functions (build_items, add_candidates, judge_all, add_flags).
"""
import sys, os, csv, json, glob, re, argparse, hashlib, threading
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from embed_and_match import parse_snhu, load_cache, embed_texts, normalize, band, verify, verify_top3

csv.field_size_limit(10_000_000)
RANK = {"equivalent": 2, "partial": 1, "not_equivalent": 0}

# Verdict cache: the same sending course + SNHU candidate(s) always gets the same verdict on re-runs
# (GPT-4o-mini at temperature 0 still drifts between runs). Delete the file to re-judge everything.
VCACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verdict_cache.json")
VCACHE = json.load(open(VCACHE_PATH, encoding="utf-8")) if os.path.exists(VCACHE_PATH) else {}
VLOCK = threading.Lock()

def cached(kind, sending, codes, fn):
    key = hashlib.sha1(json.dumps([kind, sending, codes], sort_keys=True).encode()).hexdigest()
    with VLOCK:
        if key in VCACHE:
            return VCACHE[key]
    v = fn()
    with VLOCK:
        VCACHE[key] = v
    return v

def save_verdict_cache():
    with VLOCK:
        json.dump(VCACHE, open(VCACHE_PATH, "w", encoding="utf-8"))

def letter_grade(c):
    for v in (c.get("units_raw"), c.get("grade")):
        if isinstance(v, str) and re.fullmatch(r"[A-Z][+-]?|NG|W|WF|WP|U|P|S|I|NC|CR", v.strip()):
            return v.strip()
    return ""

def division(inst, code):
    """'lower' / 'upper' / '' from a course number. UNH numbers lower-division 400-500, upper 600-700."""
    m = re.search(r"(\d{3,4})", code or "")
    if not m:
        return ""
    n = m.group(1)
    first = int(n[0])
    if "new hampshire" in inst.lower() and len(n) == 3:
        return "lower" if first in (4, 5) else "upper" if first in (6, 7) else ""
    return "lower" if first in (0, 1, 2) else "upper" if first in (3, 4) else ""

def level_flag(inst, sending_code, snhu_code):
    s, t = division(inst, sending_code), division("snhu", snhu_code)
    if s == "lower" and t == "upper":
        return "CHECK: lower-division course matched to upper-division SNHU course"
    return ""

def triage(inst, c):
    title, g = c.get("course_title", ""), letter_grade(c)
    if "advanced placement" in title.lower() or (g == "NG" and re.search(r"\(\d\)\s*$", title)):
        return "AP/exam credit — apply SNHU AP policy, not course matching"
    if g in ("W", "WF", "WP"):
        return "Withdrawn — no credit earned"
    if g in ("U", "F", "NC"):
        return "Not passed — no credit earned"
    return ""

def clean_title(t):
    return re.sub(r"\(\d\)\s*$", "", re.sub(r"^T\s+", "", t)).strip()

def load_snhu(snhu_dir, cache):
    """SNHU undergraduate courses with descriptions, and their normalized embedding matrix."""
    snhu = []
    for p in glob.glob(os.path.join(snhu_dir, "*.md")):
        snhu.extend(parse_snhu(p))
    snhu = [c for c in snhu if c["desc"] and c["level"].startswith("under")]
    S = normalize(embed_texts([f"{c['title']}. {c['desc']}" for c in snhu], cache))
    return snhu, S

def build_items(docs, catalogs, lookup):
    """One work item per transcript line.

    docs: transcript rows (document_id, institution_name, program_of_study, courses_json)
    catalogs: {institution name lowercased: {CODE: record}}
    lookup(inst, course) -> (coverage status, catalog code or "")
    """
    items = []
    for doc in docs:
        inst = doc["institution_name"].strip()
        cat = catalogs.get(inst.lower())
        for c in json.loads(doc["courses_json"]):
            it = {"doc": doc["document_id"], "inst": inst, "program": doc["program_of_study"],
                  "code": c["course_code"], "title": c["course_title"], "term": c.get("term") or "",
                  "grade": letter_grade(c), "units": c.get("units"), "handling": triage(inst, c),
                  "cat_code": "", "cat_title": "", "desc": "", "mode": "", "source_url": "", "catalog_year": ""}
            status, cat_code = lookup(inst, c) if cat else ("", "")
            rec = cat.get(cat_code.upper()) if cat and cat_code else None
            if rec:
                it.update(cat_code=rec["code"], cat_title=rec["title"], desc=rec.get("description", ""),
                          mode=status, source_url=rec.get("source_url", ""), catalog_year=rec.get("catalog_year") or "")
                if it["units"] in (None, "") and rec.get("credits"):
                    it["units"] = rec["credits"]
            if not it["handling"]:
                if it["desc"]:
                    it["mode"] = it["mode"] or "catalog"
                elif cat:
                    it["handling"] = "Not in catalog — request description/syllabus"
                else:
                    it["mode"] = "title only"
            items.append(it)
    return items

def add_candidates(todo, snhu, S, cache):
    """Top-3 SNHU candidates by cosine similarity for each item to be judged."""
    texts = [f"{it['cat_title'] or clean_title(it['title'])}. {it['desc']}".strip() if it["desc"] else clean_title(it["title"])
             for it in todo]
    if not texts:
        return
    B = normalize(embed_texts(texts, cache))
    sims = B @ S.T
    for k, it in enumerate(todo):
        idx = np.argsort(-sims[k])[:3]
        it["cands"] = [(snhu[j], float(sims[k, j])) for j in idx]

def judge(it):
    if it["mode"] == "title only":
        c, sc = it["cands"][0]
        return dict(snhu=c, score=sc, rank=1, verdict="needs description",
                    conf="", reason="No catalog description available; title-only suggestion.")
    sending = {"code": it["cat_code"] or it["code"], "title": it["cat_title"] or clean_title(it["title"]),
               "description": it["desc"]}
    c1, s1 = it["cands"][0]
    v1 = cached("strict", sending, [c1["code"]], lambda: verify(sending, c1))
    best = dict(snhu=c1, score=s1, rank=1, verdict=v1.get("verdict"), conf=v1.get("confidence"), reason=v1.get("reason"))
    v3 = cached("top3", sending, [c["code"] for c, _ in it["cands"]], lambda: verify_top3(sending, it["cands"]))
    if v3["best"] in (2, 3):
        cp, sp = it["cands"][v3["best"] - 1]
        vs = cached("strict", sending, [cp["code"]], lambda: verify(sending, cp))
        if RANK.get(vs.get("verdict"), -1) > RANK.get(best["verdict"], -1):
            best = dict(snhu=cp, score=sp, rank=v3["best"], verdict=vs.get("verdict"),
                        conf=vs.get("confidence"), reason=vs.get("reason"))
    return best

def judge_all(todo, workers=6):
    """Judge each item once (repeated transcript lines share one LLM call) and store it["res"]."""
    key = lambda it: (it["inst"], it["cat_code"] or it["code"], it["title"], it["desc"])
    uniq = {}
    for it in todo:
        uniq.setdefault(key(it), it)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = dict(zip(uniq.keys(), ex.map(judge, uniq.values())))
    save_verdict_cache()
    for it in todo:
        it["res"] = results[key(it)]
    return len(uniq)

def add_flags(items):
    seen_snhu = defaultdict(list)
    for it in items:
        r = it.get("res")
        if r and r["verdict"] in ("equivalent", "partial"):
            seen_snhu[(it["doc"], r["snhu"]["code"])].append(it)
    lines = Counter((it["doc"], it["code"], it["title"], it["term"]) for it in items)
    for it in items:
        r = it.get("res")
        flags = []
        if lines[(it["doc"], it["code"], it["title"], it["term"])] > 1:
            flags.append("Duplicate transcript line (same course and term), likely a parsing repeat; count once")
        if r:
            lf = level_flag(it["inst"], it["cat_code"] or it["code"], r["snhu"]["code"])
            if lf and r["verdict"] in ("equivalent", "partial"):
                flags.append(lf)
            dup = seen_snhu.get((it["doc"], r["snhu"]["code"]), [])
            n_courses = len({(d["cat_code"] or d["code"]) for d in dup})
            n_terms = len({d["term"] for d in dup})
            if n_courses > 1:
                flags.append(f"CHECK: {n_courses} different courses map to {r['snhu']['code']}")
            elif n_terms > 1:
                flags.append("Repeated attempt of the same course")
        it["flags"] = "; ".join(flags)

def write_csv(items, path):
    hdr = ["Document ID", "Institution", "Program", "Transcript Code", "Transcript Title", "Term", "Grade", "Credits",
           "Handling", "Catalog Code Used", "Catalog Title", "Lookup", "SNHU Match", "SNHU Title", "Candidate Rank",
           "Cosine", "Band", "Verdict", "Confidence", "Reason", "Flags"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(hdr)
        for it in items:
            r = it.get("res")
            w.writerow([it["doc"], it["inst"], it["program"], it["code"], it["title"], it["term"], it["grade"], it["units"],
                        it["handling"] or "Evaluate", it["cat_code"], it["cat_title"], it["mode"],
                        r["snhu"]["code"] if r else "", r["snhu"]["title"] if r else "", r["rank"] if r else "",
                        round(r["score"], 3) if r else "", band(r["score"]) if r else "",
                        r["verdict"] if r else "", r["conf"] if r else "", r["reason"] if r else "", it["flags"]])

def write_md(items, path):
    md = ["# Transcript evaluation drafts (AI-assisted, not final)", "",
          "Each course was matched to SNHU undergraduate courses by embedding similarity, then checked by GPT-4o-mini "
          "with a strict single-pair prompt. Verdicts are drafts for an evaluator to confirm. Grade and credit-minimum "
          "policies are not applied.", ""]
    by_doc = defaultdict(list)
    for it in items:
        by_doc[(it["doc"], it["inst"], it["program"])].append(it)
    for (doc, inst, prog), its in by_doc.items():
        counts = defaultdict(int)
        for it in its:
            counts[it["res"]["verdict"] if it.get("res") else "set aside"] += 1
        md += [f"## {inst} — document {doc}", f"Program: {prog if prog and prog != 'null' else '—'}  ",
               "Summary: " + ", ".join(f"{k} {v}" for k, v in counts.items()), "",
               "| Transcript course | Term | Grade | Result | SNHU course | Score | Why | Flags |", "|---|---|---|---|---|---|---|---|"]
        for it in its:
            r = it.get("res")
            sent = f"{it['code']} {it['title']}" + (f" → {it['cat_code']}" if it["cat_code"] and it["cat_code"].replace('-', ' ') != it["code"] else "")
            if r:
                md.append(f"| {sent} | {it['term']} | {it['grade']} | **{r['verdict']}** | {r['snhu']['code']} {r['snhu']['title']} | "
                          f"{r['score']:.2f} | {str(r['reason'] or '').replace('|', '/')} | {it['flags']} |")
            else:
                md.append(f"| {sent} | {it['term']} | {it['grade']} | set aside | — | — | {it['handling']} | |")
        md.append("")
    open(path, "w", encoding="utf-8").write("\n".join(md))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts", required=True)
    ap.add_argument("--coverage", required=True)
    ap.add_argument("--snhu", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("catalogs", nargs="*")
    a = ap.parse_args()

    catalogs = {}
    for arg in a.catalogs:
        name, path = arg.split("=", 1)
        catalogs[name.strip().lower()] = {r["code"].upper(): r for r in map(json.loads, open(path, encoding="utf-8")) if r}
    cov = {}
    for r in csv.DictReader(open(a.coverage, encoding="utf-8-sig")):
        cov[(r["Institution"], r["Transcript Code"], r["Transcript Title"], r["Term"])] = r

    def lookup(inst, c):
        cv = cov.get((inst, c["course_code"], c["course_title"], c.get("term")))
        return (cv["Status"], cv["Catalog Code"]) if cv else ("", "")

    cache = load_cache()
    snhu, S = load_snhu(a.snhu, cache)
    items = build_items(csv.DictReader(open(a.transcripts, encoding="utf-8-sig")), catalogs, lookup)
    todo = [it for it in items if not it["handling"]]
    add_candidates(todo, snhu, S, cache)
    n_uniq = judge_all(todo, a.workers)
    add_flags(items)
    write_csv(items, a.out_csv)
    write_md(items, a.out_md)

    total = defaultdict(int)
    for it in items:
        total[it["res"]["verdict"] if it.get("res") else "set aside: " + it["handling"].split(" —")[0]] += 1
    print("Transcript lines:", len(items), "| matched:", len(todo), "| LLM-judged unique:", n_uniq)
    for k, v in sorted(total.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    print("Wrote", a.out_csv, "and", a.out_md)

if __name__ == "__main__":
    main()
