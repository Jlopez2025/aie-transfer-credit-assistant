"""
Build an external-institution OKF markdown file from a catalog JSONL.

JSONL fields per line: institution, code, subject, number, title, credits, description,
level, catalog_year, source_url

Usage:
  python build_okf.py --jsonl courses.jsonl --dapip DAPIP_Institutionally_Accredited.csv \
      --name "University of New Hampshire" --state NH --source "CourseLeaf" \
      --catalog-url https://catalog.unh.edu --out University_of_New_Hampshire.md
"""
import argparse, csv, json, re, datetime
from collections import Counter

csv.field_size_limit(10_000_000)

def load_dapip(path, name, state):
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["Institution Name"].strip().lower() == name.lower() and r["State"] == state:
                return r
    return None

def numkey(c):
    m = re.match(r"(\d+)", c.get("number", ""))
    return (c.get("subject", ""), int(m.group(1)) if m else 0, c.get("number", ""), c.get("level", ""))

def q(s):
    return '"' + str(s or "").replace('"', "'") + '"'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--dapip", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--source", required=True, help="catalog platform, e.g. CourseLeaf")
    ap.add_argument("--catalog-url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--note", default="", help="optional frontmatter note")
    args = ap.parse_args()

    courses = [json.loads(l) for l in open(args.jsonl, encoding="utf-8") if l.strip()]
    courses.sort(key=numkey)
    meta = load_dapip(args.dapip, args.name, args.state) or {}
    years = Counter(c.get("catalog_year", "") for c in courses if c.get("catalog_year"))
    catalog_year = years.most_common(1)[0][0] if years else ""
    levels = Counter(c.get("level", "undergraduate") for c in courses)
    today = datetime.date.today().isoformat()

    fm = [
        "---",
        'type: "external-course-catalog"',
        f"institution: {q(args.name)}",
        f"state: {q(meta.get('State', args.state))}",
        f"city: {q(meta.get('City'))}",
        f"dapip_id: {q(meta.get('DAPIP ID'))}",
        f"ope_id: {q(meta.get('OPE ID'))}",
        f"ipeds_unitid: {q(meta.get('IPEDS UnitID(s)'))}",
        f"accreditor: {q(meta.get('Institutional Accreditor(s)'))}",
        f"accreditation_status: {q(meta.get('Accreditation Status'))}",
        f"catalog_source: {q(args.source)}",
        f"catalog_url: {q(args.catalog_url)}",
        f"catalog_year: {q(catalog_year)}",
        f"retrieved: {q(today)}",
        f"course_count: {len(courses)}",
        f"subject_count: {len(set(c.get('subject') for c in courses))}",
        "levels: {" + ", ".join(f"{k}: {v}" for k, v in sorted(levels.items())) + "}",
    ]
    if args.note:
        fm.append(f"note: {q(args.note)}")
    fm += ['owner: "generated from the institution catalog; do not hand-edit"', "---", "",
           f"# {args.name} courses", ""]

    out = fm
    for c in courses:
        level = c.get("level") or "undergraduate"
        out.append(f"## {c['code']} — {c.get('title', '').strip()} `[{level}]`")
        out.append("")
        out.append(c.get("description") or "_No description on record._")
        out.append("")
        if c.get("prerequisites"):
            pre = c["prerequisites"]
            out.append(pre if re.match(r"(Pre|Co)", pre) else f"Prerequisites: {pre}")
            out.append("")
        if c.get("outcomes"):
            out.append(f"Learning outcomes: {c['outcomes']}")
            out.append("")
        if c.get("credits"):
            out.append(f"Credits: {c['credits']}")
            out.append("")
        src = c.get("source_url") or args.catalog_url
        out.append(f"> source: {src} (retrieved {today})")
        out.append("")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    with_desc = sum(1 for c in courses if c.get("description"))
    print(f"{args.name}: {len(courses)} courses ({with_desc} with descriptions), catalog {catalog_year} -> {args.out}")

if __name__ == "__main__":
    main()
