# Transfer-Credit Matcher (Azure OpenAI)

Two-stage matcher: incoming (sending-school) course → SNHU catalog equivalent.

- **Stage 1 — Embeddings** (`text-embedding-3-large`): semantic cosine match, level
  filtering (undergrad↔undergrad), and confidence bands (High / Medium / Low / No match).
- **Stage 2 — LLM verifier** (`GPT-4o-mini`): confirms equivalence on top candidates and
  returns `{verdict, confidence, reason}`. Catches high-similarity-but-not-equivalent cases
  (e.g. *applied music* vs *music theory*) that embeddings alone pass.

## Setup
1. Requires Python 3.12 with `numpy`. (On the SNHU network / VPN — the Azure resource has
   public access disabled and is reachable only via its private endpoint.)
2. `copy .env.example .env` and paste your Azure key into `.env`.

## Run
```
python embed_and_match.py ^
  --snhu  "<path>\okf_courses" ^
  --bhcc  "<path>\<institution>.jsonl" ^
  --out   "<output dir>" ^
  --same-level ^
  --verify 40
```
- `--same-level` restricts SNHU candidates to undergraduate (recommended for community-college transfer).
- `--verify N` runs the LLM verifier on N sampled courses (`--verify 100000` = verify all).
- `--top3` has the verifier see all three embedding candidates in one call and pick the best
  (or none). Recommended: it rescues courses whose right SNHU match was candidate 2 or 3.
- `--workers N` sets concurrent verifier calls (default 6; ~4 min for 928 courses).
- Embeddings are cached in `emb_cache.pkl`, so re-runs and added schools only embed new text (no re-billing).

## Inputs
- **SNHU side**: the `okf_courses/*.md` files (parsed: `## CODE — Title [level]` + description paragraph).
- **Sending side**: an institution JSONL (one course per line: `code, title, description, credits, ...`),
  produced by the catalog extractor.

## Outputs
- `BHCC_to_SNHU_matches_embeddings.csv` — every course, top-3 SNHU candidates + cosine + band.
- `BHCC_to_SNHU_verified_sample.csv` — sampled courses with the LLM verdict.

## Scaling to more schools
Run the same catalog extractor for each feeder (many use Ellucian / Banner / Workday guest
APIs), produce its `.jsonl`, then point `--bhcc` at it. The SNHU embeddings stay cached;
only the new school's courses get embedded.

## Security
The real `.env` holds a live key — keep it out of version control. Rotate the key if it has
been shared in plaintext, and prefer Azure Key Vault for production.

## Recommended full run (three steps)
1. `embed_and_match.py ... --same-level --verify 100000` → strict verdict on candidate 1.
2. `embed_and_match.py ... --same-level --verify 100000 --top3` → picks the best of the top 3.
3. `finalize_verdicts.py <okf_courses> <school.jsonl> <step1.csv> <step2.csv> <FINAL.csv>`
   re-judges step-2 picks of candidate 2/3 with the strict prompt and keeps the better verdict.

Why step 3: when the model is asked to *choose* among candidates it grades its pick more
leniently (on BHCC, "equivalent" jumped 161 → 492 and most "partial" verdicts disappeared).
Re-judging with the single-pair prompt keeps every final verdict on the same strict scale.
The step-2 CSV is diagnostic only — use the FINAL file.

## Transcript evaluation (per student)
1. Build each sending school's catalog JSONL (and OKF file with `build_okf.py`). If descriptions
   open with requisite text ("Prerequisite: CJ 205 This course ..."), run `split_requisites.py <school.jsonl>`
   first so embeddings see only course content (the text moves to a `prerequisites` field).
2. `coverage.py <transcripts.csv> <coverage.csv> "<Institution Name>=<school.jsonl>" ...`
   maps each transcript line to a catalog course, catching misread codes (GBO→GEO),
   misparsed codes whose title fits another course in the same subject (ENG 101 "Comp II" → ENG 102),
   renumbered schools (New Haven MUSC 1112 = legacy MU 112), and "Lab <course>" lines.
   Title-only matches must contain every content word of the transcript title.
3. `transcript_match.py --transcripts ... --coverage ... --snhu <okf_courses> --out-csv ... --out-md ... "<Institution Name>=<school.jsonl>" ...`
   sets aside AP/exam credit, withdrawn and failed lines; matches the rest (strict verdict,
   top-3 rescue, strict re-judge); flags lower→upper-division matches, repeated attempts, and
   several different courses mapping to one SNHU course, and duplicate lines (same course and
   term, usually a parsing repeat). Schools with no catalog get title-only suggestions and no
   verdict. Grade minimums and credit caps are NOT applied.
   Verdicts are cached in `verdict_cache.json` next to the script, so re-runs give the same
   answer for the same course pair; delete the file to re-judge from scratch.

### University of New Haven source
New Haven's own catalog (Acalog) is behind a bot challenge, so `University_of_New_Haven.jsonl`
comes from CollegeTransfer.Net course profiles: 141 courses found by about 25 paced searches for
one transcript, not a full catalog. CollegeTransfer's robots.txt disallows crawling its course
pages and reserves AI-training use, so don't bulk-harvest it; for full coverage ask New Haven's
registrar for a catalog export. Its profiles use pre-2018 numbers (MU 112) and are undated.
