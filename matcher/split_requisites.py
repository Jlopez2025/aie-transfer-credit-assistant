"""
Move leading "Prerequisite(s): ..." / "Corequisite(s): ..." text out of each description into a
`prerequisites` field, so embeddings and verdicts see only what the course covers.

A requisite clause ends at a period, a closing parenthesis, or a course number, when a capitalized
sentence follows ("Prerequisite: CJ 205 This course ...").

Usage: python split_requisites.py courses.jsonl   (rewrites the file in place)
"""
import sys, json, re

LEAD = r"^((?:Pre(?:re)?|Co-?re)quisites?\s*:.*?)"   # "Prequisites" typo included
CLAUSE = re.compile(LEAD + r"(?:(?<=[.)])|(?<=\d)|(?<=\d[A-Z]))\s+(?=[A-Z][a-z]|A\s)", re.S)
# fallback when no punctuation or course number ends the clause: "... department chair Topics selected ..."
LOOSE = re.compile(LEAD + r"(?<=[a-z])\s+(?=[A-Z][a-z])", re.S)

def split(desc):
    reqs, rest = [], (desc or "").strip()
    for _ in range(3):
        m = CLAUSE.match(rest) or LOOSE.match(rest)
        if not m:
            break
        reqs.append(m.group(1).strip())
        rest = rest[m.end():].strip()
    return " ".join(reqs), rest

path = sys.argv[1]
rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
n = 0
for r in rows:
    reqs, rest = split(r.get("description"))
    if reqs and rest:
        r["prerequisites"] = " ".join(filter(None, [r.get("prerequisites"), reqs]))
        r["description"] = rest
        n += 1
with open(path, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
left = sum(1 for r in rows if re.match(r"(Pre|Co)", r.get("description", "")))
print(f"split {n} of {len(rows)} descriptions; {left} still start with a requisite")
