"""
Combine the strict top-1 run with the top-3 run so every final verdict comes from the
same strict single-pair prompt:
  - keep the top-1 verdict (candidate 1)
  - if the top-3 run picked candidate 2 or 3, re-judge that pair with the strict prompt
  - final = the better of the two strict verdicts
"""
import sys, os, csv, glob, json
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from embed_and_match import parse_snhu, verify

snhu_dir, bhcc_jsonl, run1_csv, top3_csv, out_csv = sys.argv[1:6]

snhu = {}
for p in glob.glob(os.path.join(snhu_dir, "*.md")):
    for c in parse_snhu(p):
        if c["desc"]:
            snhu[c["code"]] = c
bhcc = {}
for line in open(bhcc_jsonl, encoding="utf-8"):
    r = json.loads(line)
    bhcc.setdefault(r["code"], r)

r1 = list(csv.DictReader(open(run1_csv, encoding="utf-8-sig")))
r3 = list(csv.DictReader(open(top3_csv, encoding="utf-8-sig")))
assert len(r1) == len(r3) and all(a["BHCC Code"] == b["BHCC Code"] for a, b in zip(r1, r3))

RANK = {"equivalent": 2, "partial": 1, "not_equivalent": 0}
todo = [i for i, b in enumerate(r3) if b["Candidate Rank"] in ("2", "3")]
print(f"Re-judging {len(todo)} rank-2/3 picks with the strict prompt...", flush=True)

def rejudge(i):
    b = r3[i]
    s = bhcc[b["BHCC Code"]]
    return i, verify({"code": s["code"], "title": s["title"], "description": s["description"]},
                     snhu[b["Chosen SNHU"]])

strict = dict(ThreadPoolExecutor(max_workers=6).map(rejudge, todo))

final_counts = Counter()
rescued = 0
with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["BHCC Code", "BHCC Title", "Final SNHU", "SNHU Title", "Candidate Rank", "CosScore",
                "Band", "Final Verdict", "Confidence", "Reason", "Source"])
    for i, (a, b) in enumerate(zip(r1, r3)):
        best = [a["Top SNHU"], a["SNHU Title"], 1, a["CosScore"], a["Band"],
                a["LLM Verdict"], a["LLM Confidence"], a["LLM Reason"], "top-1"]
        if i in strict:
            v = strict[i]
            if RANK.get(v.get("verdict"), -1) > RANK.get(a["LLM Verdict"], -1):
                best = [b["Chosen SNHU"], b["SNHU Title"], int(b["Candidate Rank"]), b["CosScore"],
                        b["Band"], v.get("verdict"), v.get("confidence"), v.get("reason"),
                        f"rescued from candidate {b['Candidate Rank']}"]
                rescued += 1
        final_counts[best[5]] += 1
        w.writerow([a["BHCC Code"], a["BHCC Title"]] + best)

print("Rescued (better strict verdict on candidate 2/3):", rescued)
print("Final verdict distribution:", dict(final_counts))
print("Wrote", out_csv)
