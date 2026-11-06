"""
Embedding-based transfer-credit matcher (Azure OpenAI).
Stage 1: text-embedding-3-large semantic match (BHCC course -> SNHU catalog), with
         level filtering and confidence bands.
Stage 2: GPT-4o-mini verifier confirms equivalence on top candidates (sampled).

Usage:
  python embed_and_match.py --snhu <okf_courses_dir> --bhcc <bhcc.jsonl> --out <dir> [--verify N]
"""
import sys, os, re, json, glob, math, hashlib, argparse, pickle, time
import numpy as np
import azure_client as az

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "emb_cache.pkl")

# ---------------- parsing ----------------
MARKERS = ("**", ">", "-", "unlocks:", "no prerequisites", "all of", "any ", "source")
def parse_snhu(path):
    out = []
    lines = open(path, encoding="utf-8").read().split("\n")
    i = 0
    while i < len(lines):
        m = re.match(r"^##\s+([A-Z0-9]+-[A-Z0-9]+)\s+—\s+(.*?)\s*(?:`\[([^\]]+)\]`)?\s*$", lines[i])
        if m:
            code, title, level = m.group(1), m.group(2).strip(), (m.group(3) or "").strip().lower()
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            desc = []
            while j < len(lines) and lines[j].strip():
                desc.append(lines[j].strip()); j += 1
            d = " ".join(desc)
            if any(d.lower().startswith(mk) for mk in MARKERS):
                d = ""
            out.append({"code": code, "title": title, "level": level or "undergraduate", "desc": d})
            i = j
        else:
            i += 1
    return out

# ---------------- embedding w/ disk cache ----------------
def load_cache():
    if os.path.exists(CACHE):
        try:
            return pickle.load(open(CACHE, "rb"))
        except Exception:
            return {}
    return {}

def save_cache(c):
    pickle.dump(c, open(CACHE, "wb"))

def embed_texts(texts, cache, batch=256):
    keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
    todo = [(k, t) for k, t in zip(keys, texts) if k not in cache]
    # unique
    seen = {}
    uniq = []
    for k, t in todo:
        if k not in seen:
            seen[k] = True; uniq.append((k, t))
    for s in range(0, len(uniq), batch):
        chunk = uniq[s:s+batch]
        vecs = az.embed([t for _, t in chunk])
        for (k, _), v in zip(chunk, vecs):
            cache[k] = np.asarray(v, dtype=np.float32)
        print(f"  embedded {min(s+batch,len(uniq))}/{len(uniq)} new texts", flush=True)
    if uniq:
        save_cache(cache)
    return np.vstack([cache[k] for k in keys])

def normalize(M):
    n = np.linalg.norm(M, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return M / n

def band(score):
    if score >= 0.62: return "High"
    if score >= 0.50: return "Medium"
    if score >= 0.40: return "Low"
    return "No match"

# ---------------- LLM verifier ----------------
VERIFY_SYS = (
    "You are a university transfer-credit evaluator. Given an incoming (sending) course and a "
    "candidate SNHU course, decide if the sending course is equivalent enough to award transfer "
    "credit for the SNHU course. Consider topic, depth, and level. Respond ONLY as JSON: "
    '{"verdict":"equivalent|partial|not_equivalent","confidence":0-1,"reason":"<=25 words"}.'
)
def verify(sending, snhu):
    user = (f"SENDING: {sending['code']} {sending['title']}\n{sending['description'][:900]}\n\n"
            f"SNHU CANDIDATE: {snhu['code']} {snhu['title']}\n{snhu['desc'][:900]}")
    try:
        txt = az.chat([{"role": "system", "content": VERIFY_SYS},
                       {"role": "user", "content": user}], json_mode=True, max_tokens=120)
        return json.loads(txt)
    except Exception as e:
        return {"verdict": "error", "confidence": 0, "reason": str(e)[:60]}

VERIFY3_SYS = (
    "You are a university transfer-credit evaluator. Given an incoming (sending) course and up to "
    "three candidate SNHU courses, pick the ONE candidate that is the best transfer equivalent, "
    "considering topic, depth, and level. If none is equivalent or partially equivalent, pick 0. "
    'Respond ONLY as JSON: {"best":0-3,"verdict":"equivalent|partial|not_equivalent",'
    '"confidence":0-1,"reason":"<=25 words"}.'
)
def verify_top3(sending, cands):
    parts = [f"SENDING: {sending['code']} {sending['title']}\n{sending['description'][:900]}\n"]
    for n, (c, _) in enumerate(cands, 1):
        parts.append(f"CANDIDATE {n}: {c['code']} {c['title']}\n{c['desc'][:700]}\n")
    try:
        txt = az.chat([{"role": "system", "content": VERIFY3_SYS},
                       {"role": "user", "content": "\n".join(parts)}], json_mode=True, max_tokens=150)
        v = json.loads(txt)
        best = int(v.get("best", 0) or 0)
        if best < 0 or best > len(cands):
            best = 0
        v["best"] = best
        return v
    except Exception as e:
        return {"best": 0, "verdict": "error", "confidence": 0, "reason": str(e)[:60]}

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snhu", required=True)
    ap.add_argument("--bhcc", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--verify", type=int, default=0, help="verify top-1 for N courses (>=total verifies all)")
    ap.add_argument("--workers", type=int, default=6, help="concurrent LLM verifier requests")
    ap.add_argument("--top3", action="store_true", help="verifier judges all top-3 candidates and picks the best")
    ap.add_argument("--same-level", action="store_true", help="restrict candidates to undergraduate")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    snhu = []
    for p in glob.glob(os.path.join(args.snhu, "*.md")):
        snhu.extend(parse_snhu(p))
    snhu = [c for c in snhu if c["desc"]]
    if args.same_level:
        snhu = [c for c in snhu if c["level"].startswith("under")]
    print(f"SNHU candidates: {len(snhu)}")

    bhcc = [json.loads(l) for l in open(args.bhcc, encoding="utf-8")]
    print(f"BHCC courses: {len(bhcc)}")

    cache = load_cache()
    print("Embedding SNHU catalog...")
    S = normalize(embed_texts([f"{c['title']}. {c['desc']}" for c in snhu], cache))
    print("Embedding BHCC catalog...")
    B = normalize(embed_texts([f"{c['title']}. {c['description']}" for c in bhcc], cache))

    print("Scoring (cosine)...")
    sims = B @ S.T  # [nB, nS]
    topk = 3
    out_csv = os.path.join(args.out, "BHCC_to_SNHU_matches_embeddings.csv")
    import csv
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        hdr = ["BHCC Code", "BHCC Title"]
        for i in range(1, topk+1):
            hdr += [f"Match{i} SNHU", f"Match{i} Title", f"Score{i}", f"Band{i}"]
        w.writerow(hdr)
        results = []
        for bi, b in enumerate(bhcc):
            idx = np.argsort(-sims[bi])[:topk]
            row = [b["code"], b["title"]]
            cand = []
            for j in idx:
                sc = float(sims[bi, j])
                row += [snhu[j]["code"], snhu[j]["title"], round(sc, 3), band(sc)]
                cand.append((snhu[j], sc))
            w.writerow(row)
            results.append((b, cand))
    print("Wrote", out_csv)

    # band distribution
    from collections import Counter
    dist = Counter(band(c[0][1]) for _, c in results)
    print("Top-1 band distribution:", dict(dist))

    # ---- LLM verification (concurrent) ----
    if args.verify > 0:
        import random
        from concurrent.futures import ThreadPoolExecutor, as_completed
        full = args.verify >= len(results)
        suffix = "_top3" if args.top3 else ""
        if full:
            targets = list(enumerate(results))
            vout = os.path.join(args.out, f"BHCC_to_SNHU_verified_ALL{suffix}.csv")
        else:
            random.seed(7)
            idxs = random.sample(range(len(results)), args.verify)
            targets = [(i, results[i]) for i in sorted(idxs)]
            vout = os.path.join(args.out, f"BHCC_to_SNHU_verified_sample{suffix}.csv")

        print(f"\nVerifying {len(targets)} courses with GPT-4o-mini ({args.workers} workers)...", flush=True)

        def work(item):
            i, (b, cand) = item
            sending = {"code": b["code"], "title": b["title"], "description": b["description"]}
            if args.top3:
                v = verify_top3(sending, cand)
                rank = v["best"]
                snhu_c, sc = cand[rank - 1] if rank else cand[0]
            else:
                snhu_c, sc = cand[0]
                v = verify(sending, snhu_c)
                rank = 1
            return i, b, snhu_c, sc, v, rank, cand[0][0]["code"]

        out_rows = {}
        done = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(work, it) for it in targets]
            for fut in as_completed(futs):
                i, b, snhu_c, sc, v, rank, top1 = fut.result()
                row = [b["code"], b["title"], snhu_c["code"] if rank else "", snhu_c["title"] if rank else "",
                       rank, round(sc, 3) if rank else "", band(sc) if rank else "",
                       v.get("verdict"), v.get("confidence"), v.get("reason")]
                if args.top3:
                    row.append(top1)
                out_rows[i] = row
                done += 1
                if done % 50 == 0 or done == len(targets):
                    print(f"  verified {done}/{len(targets)}  ({time.time()-t0:.0f}s)", flush=True)

        with open(vout, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            hdr = ["BHCC Code", "BHCC Title", "Chosen SNHU", "SNHU Title", "Candidate Rank", "CosScore",
                   "Band", "LLM Verdict", "LLM Confidence", "LLM Reason"]
            if args.top3:
                hdr.append("Embedding Top-1 SNHU")
            w.writerow(hdr)
            for i in sorted(out_rows):
                w.writerow(out_rows[i])
        print("Wrote", vout)

        verdicts = Counter(out_rows[i][7] for i in out_rows)
        print("Verdict distribution:", dict(verdicts))

if __name__ == "__main__":
    main()
