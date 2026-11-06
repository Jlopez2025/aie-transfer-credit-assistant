# aie-transfer-credit-assistant
 course-matching tool to cut the time an admissions officer needs to qualify an outside course for transfer credit.

## What's here

- `app/`: Streamlit prototype. Upload a parsed transcript and get a draft evaluation table with
  source links and approve/override. Live web search for missing descriptions is off for now
  ("Live Agent Search Coming Soon"). See `app/README.md`.
- `matcher/`: builds OKF catalog files and matches transcript courses to SNHU courses with Azure
  OpenAI embeddings plus a GPT verdict. See `matcher/README.md`.
- `survey/`: which degree-granting schools' online catalogs can be scraped ahead of time;
  results in `Catalog_Scrapeability_Survey.xlsx`. See `survey/README.md`.
- `EXTERNAL_OKF_FORMAT.md`: the sending-school catalog file format.

## Not in the repository

- Settings and keys (`.env`); copy `app/.env.example` or `matcher/.env.example`.
- Transcripts and anything built from them (student records).
- Sending-school catalog files (`<School>.md` / `.jsonl`) and the BHCC match outputs, which are
  built from school websites and CollegeTransfer.net. Rebuild with `matcher/build_okf.py`.
- SNHU course OKF files (set `SNHU_OKF_DIR`), the DAPIP list, IPEDS HD2023
  (nces.ed.gov), and the embedding / verdict caches.
