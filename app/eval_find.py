"""
Measure "Find description": run it on every transcript line that has no catalog description and
print what was found, so each result can be checked by hand.

Usage:  python eval_find.py [transcripts.csv]     (default: the sample transcripts)
"""
import sys, csv
import engine, store

path = sys.argv[1] if len(sys.argv) > 1 else engine.SAMPLE
docs = list(csv.DictReader(open(path, encoding="utf-8-sig")))
# start clean so earlier finds don't hide courses from the measurement (domains are kept)
store.save("found_descriptions.json", {})
snhu, S, cache = engine.load_snhu()
items, _ = engine.evaluate(docs, engine.discover_catalogs(), snhu, S, cache)
missing = [it for it in items if it["handling"].startswith("Not in catalog") or it["mode"] == "title only"]
uniq = {}
for it in missing:
    uniq.setdefault(store.course_key(it["inst"], it["code"], it["title"]), it)
print(f"{len(missing)} lines without a description ({len(uniq)} distinct courses)\n")

for it in uniq.values():
    print(f"=== {it['inst']} | {it['code']} {it['title']} ({it['term']})")
    rec, tried = engine.find_and_store(it["inst"], it["code"], it["title"], log=lambda m: print("   ", m), term=it["term"])
    for t in tried:
        print(f"    - {t['url']}\n      {t['outcome']}")
    if rec:
        print(f"  FOUND {rec['code']} {rec['title']} | year: {rec['catalog_year'] or '?'} | {rec['source_url']}")
        print(f"  {rec['description'][:400]}")
    else:
        print("  NOT FOUND")
    print()
