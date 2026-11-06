"""
Check which transcript courses exist in each school's extracted catalog.

For misses, suggest the closest catalog course: same course number first (catches
misread subject prefixes like GBO->GEO), then best title similarity overall.

Usage:
  python coverage.py <transcripts.csv> <out.csv> name=path/to/courses.jsonl [name=...]

The app imports map_course() and load_catalog() from here.
"""
import sys, csv, json, re
from difflib import SequenceMatcher

csv.field_size_limit(10_000_000)

def load_catalog(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]

def norm_code(code):
    return re.sub(r"\s+", "-", (code or "").strip().upper())

def norm_title(t):
    t = re.sub(r"^T\s+", "", (t or "").strip())          # USF "T " = transfer-course marker
    t = re.sub(r"\(\d+\)\s*$", "", t)                      # trailing "(3)" credit marker
    t = t.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()

def sim(a, b):
    return SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()

STOP = {"to", "of", "the", "and", "in", "for", "a", "an", "w", "with", "i", "ii", "iii"}

def words(t):
    return [w for w in norm_title(t).split() if w not in STOP]

def word_in(w, others):
    """Exact word, or an abbreviation of at least 3 letters ("perf" ~ "performance", "bib" ~ "biblical")."""
    return any(w == o or (min(len(w), len(o)) >= 3 and (o.startswith(w) or w.startswith(o))) for o in others)

def token_fit(a, b):
    """Word overlap as a share of the longer title, so extra words on either side lower the fit."""
    ta, tb = words(a), words(b)
    if len(ta) < 2 or len(tb) < 2:
        return 0
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    hit = sum(word_in(s, long_) for s in short)
    return hit / len(long_) if hit >= 2 else 0

def covered(a, b):
    """Every content word of title a appears in title b (guards title-only matches: "Acid Research Project")."""
    return all(word_in(w, words(b)) for w in words(a))

def digits(n):
    m = re.match(r"(\d+)", n or "")
    return m.group(1) if m else ""

def index_catalog(cat):
    by_code = {}
    for c in cat:
        by_code.setdefault(c["code"].upper(), c)
    return by_code

def map_course(code_raw, title, cat, by_code=None):
    """Map one transcript line to a catalog course. Returns (status, match or None, title similarity or None)."""
    by_code = by_code if by_code is not None else index_catalog(cat)
    code = norm_code(code_raw)
    hit = by_code.get(code)
    if hit:
        score = sim(title, hit["title"])
        # A transcript code can be misparsed (e.g. "ENG 101 Comp II"). If another course in the
        # same subject fits the transcript title much better, prefer it and say so.
        subj = code.split("-")[0]
        alts = [c for c in cat if c["code"].upper().split("-")[0] == subj and c["code"].upper() != code]
        alt = max(alts, key=lambda c: sim(title, c["title"]), default=None)
        alt_score = sim(title, alt["title"]) if alt else 0
        contained = norm_title(title) and norm_title(title) in norm_title(hit["title"])  # e.g. "General Physics I" in "General Physics I - Calculus Based"
        if alt and not contained and alt_score >= 0.75 and alt_score - score >= 0.2:
            return f"suggested (title fits {alt['code']} better than {code})", alt, alt_score
        return ("exact code" if score >= 0.5 else "exact code, title differs"), hit, score

    num = code.split("-", 1)[1] if "-" in code else code
    same_num = [c for c in cat if c["number"].upper() == num]
    cands = sorted(same_num, key=lambda c: -sim(title, c["title"]))
    # Renumbered schools: a 4-digit transcript number often keeps the old 3-digit number as its
    # last three digits (New Haven MUSC 1112 = legacy MU 112).
    legacy = []
    if len(digits(num)) == 4:
        legacy = sorted((c for c in cat if len(digits(c["number"])) == 3 and digits(c["number"]) == digits(num)[1:]),
                        key=lambda c: -sim(title, c["title"]))
    lab = re.match(r"^lab\W+([a-z]+)\s*(\d+)", norm_title(title))  # "Lab MUSC 1125" = the lab for MUSC 1125
    lab_hit = None
    if lab:
        lec_num = lab.group(2)[-3:]
        lec = [c for c in cat if digits(c["number"]) == lec_num]
        for lc in lec:
            lab_hit = next((c for c in cat if c["subject"] == lc["subject"] and c is not lc
                            and norm_title(c["title"]) == norm_title(lc["title"]) + " lab"), None)
            if lab_hit:
                break
    if cands and sim(title, cands[0]["title"]) >= 0.5:
        return "suggested (same number)", cands[0], sim(title, cands[0]["title"])
    if lab_hit:
        return f"suggested (lab for {lab.group(1).upper()} {lab.group(2)})", lab_hit, None
    if legacy and sim(title, legacy[0]["title"]) >= 0.5:
        return "suggested (legacy number)", legacy[0], sim(title, legacy[0]["title"])
    best = max(cat, key=lambda c: sim(title, c["title"]))
    sc = sim(title, best["title"])
    tbest = max(cat, key=lambda c: token_fit(title, c["title"]))
    if sc >= 0.75 and covered(title, best["title"]):
        return "suggested (title only)", best, sc
    if token_fit(title, tbest["title"]) >= 0.75:
        return "suggested (title words)", tbest, sim(title, tbest["title"])
    return "not found", None, None

def main():
    transcripts_csv, out_csv = sys.argv[1], sys.argv[2]
    catalogs = {}
    for arg in sys.argv[3:]:
        name, path = arg.split("=", 1)
        catalogs[name.strip().lower()] = load_catalog(path)

    rows_out = []
    summary = {}
    with open(transcripts_csv, encoding="utf-8-sig", newline="") as f:
        for doc in csv.DictReader(f):
            inst = doc["institution_name"].strip()
            cat = catalogs.get(inst.lower())
            courses = json.loads(doc["courses_json"])
            s = summary.setdefault(inst, {"courses": 0, "exact": 0, "exact_title_ok": 0, "suggested": 0, "missing": 0,
                                          "has_catalog": cat is not None})
            by_code = index_catalog(cat) if cat else {}
            for tc in courses:
                s["courses"] += 1
                title = tc["course_title"]
                status, match, score = "no catalog", None, None
                if cat:
                    status, match, score = map_course(tc["course_code"], title, cat, by_code)
                    if status.startswith("exact"):
                        s["exact"] += 1
                        s["exact_title_ok"] += score >= 0.5
                    elif match:
                        s["suggested"] += 1
                    else:
                        s["missing"] += 1
                rows_out.append([inst, tc["course_code"], title, tc.get("term"), status,
                                 match["code"] if match else "", match["title"] if match else "",
                                 round(score, 2) if score is not None else "",
                                 "yes" if match and match.get("description") else ("" if not match else "no")])

    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Institution", "Transcript Code", "Transcript Title", "Term", "Status",
                    "Catalog Code", "Catalog Title", "Title Similarity", "Has Description"])
        w.writerows(rows_out)

    for inst, s in summary.items():
        if not s["has_catalog"]:
            print(f"{inst}: no catalog provided ({s['courses']} transcript courses)")
            continue
        print(f"{inst}: {s['courses']} transcript courses | exact code {s['exact']} "
              f"(title agrees {s['exact_title_ok']}) | suggested {s['suggested']} | not found {s['missing']}")
    print("Wrote", out_csv)

if __name__ == "__main__":
    main()
