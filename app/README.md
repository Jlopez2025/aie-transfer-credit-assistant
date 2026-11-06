# Transfer credit evaluator (prototype)

One screen: upload a parsed transcript, review AI-drafted SNHU matches with their sources,
find missing course descriptions on the sending school's website, and approve or override each line.
Styled to the SNHU Brand Kit (`.streamlit/config.toml`, logos in `static/`; the logos are SVG
redraws, so replace them with the official files when available).

## Run
1. VPN on (Azure OpenAI is a private endpoint).
2. `app/.env` holds the Azure settings and the search settings (never commit it; `.gitignore` covers it;
   copy `.env.example` to start):
   - `SNHU_OKF_DIR`: folder of SNHU course OKF files (one `.md` per course).
   - `LIVE_SEARCH=false`: the "Find description" web search is off and the app shows
     "Live Agent Search Coming Soon"; `true` turns it back on.
   - `SEARCH_PROVIDERS=azure`: **only the Azure OpenAI research agent runs** (`azure_research.py`). It
     uses the existing Azure OpenAI chat deployment (`AZURE_OPENAI_RESEARCH_DEPLOYMENT` to pick a
     stronger one) with function calling over the app's own tools. It does not use Azure's Bing-based
     web search, whose terms bar building a database from results or fetching the result links.
   - `TAVILY_API_KEY`: the agent's search tool (and the one-time official-website lookup).
   - `ARCHIVE_FIRST=false`: the standalone archive lookup before searching is off; the agent reads
     archived copies itself when a page is dead or blocked.
   - Also wired in but off: `tavily`, `claude` (DATUM-style Claude research; needs `ANTHROPIC_API_KEY`
     with API credit, plus `ANTHROPIC_API_URL` / `ANTHROPIC_WORKSPACE_ID` as needed) and `brave`
     (its terms bar storing results). Add a name to `SEARCH_PROVIDERS` to turn one on.
   - SNHU's Azure OpenAI deployment filters personal information from responses, and a filtered
     response comes back empty. The app removes phone numbers and emails from page text, and asks
     for a catalog start year as a number, because "2022-2023" in a response is read as a phone number.
3. From this folder:
   ```
   python -m streamlit run app.py
   ```
   `.streamlit/config.toml` keeps it on 127.0.0.1. First load takes ~15 s.

Input is the parsed-transcript CSV (`document_id, institution_name, program_of_study, courses_json`),
same as `sample_transcript_parsed.csv`. With no upload, the five sample transcripts are shown.

## Pieces
- `engine.py`: runs the matcher (`../matcher`) using the sending catalogs in `../*.jsonl`, choosing the
  catalog year closest to the student's terms, plus any descriptions found by search. Learns each
  catalog's page-address pattern from its saved source links (Bristol: `/courses/{S}{N}`).
- `finder.py`: Find description.
  1. Looks up the school's official .edu domain once (`data/domains.json`, editable).
  2. Archives first: the course's own catalog address (from the pattern) in the Internet Archive and
     Common Crawl, closest to the term taken. Catches courses dropped from the live catalog.
  3. Then search engines in order. Claude researches the school's domain itself (several searches,
     reads pages and PDFs); Tavily runs fixed queries. A page that can't be read live (dead link,
     long crawl delay, bot check) is read from its archived copy instead.
  4. Excerpts the page around the course (code, a code the researcher reported, or title words when
     the transcript code is misread) and has GPT-4o-mini copy the description verbatim.
  5. Accepts it only if: it is on the school's domain; it appears on the page; it is real prose, not a
     course listing; it is about this course (title-to-description similarity >= 0.40; real
     descriptions scored 0.52-0.59, unrelated page text 0.05-0.25); and the page's title agrees with
     the transcript (a matching number alone is not enough). Claude's own text is never used as a
     description, only the pages it fetched.
  6. If several pages agree on the course but none has a description, the app shows the identified
     course ("ECON 1134 Intro to Microeconomics, 8 pages agree") so the evaluator can request it.
- `claude_search.py`: the Claude researcher, modeled on DATUM's internet research tool: one request with
  Anthropic's server-side `web_search_20260318` + `web_fetch_20260318` (`allowed_callers: ["direct"]`,
  hard `max_uses` caps of 4 and 4, 20k tokens per fetch, both tools limited to the school's domain,
  low effort, `pause_turn` resumed). Cost: $10 per 1,000 searches plus tokens; fetch is tokens only.
- `archive.py`: exact-address lookups in the Internet Archive (Wayback Machine) and Common Crawl.
  Both allow research use and keeping what you retrieve. Common Crawl's index is often overloaded
  (504/503 during testing), so it is skipped after two failures in a session.
- `fetcher.py`: gets page text for results that come back without it. Honors robots.txt and crawl
  delays (more than 15 s: not fetched), identifies itself, pauses between requests, reads PDFs and
  app-style catalogs (embedded JSON), and never works around a bot check.
- `store.py`: `data/found_descriptions.json` (with a per-course search log), `data/decisions.json`.
- `eval_find.py`: runs Find description on every line without a description and prints the outcome.

Searches send only the school name, course code and title. Never student details.

## Test (sample transcripts, 9 lines / 7 courses without descriptions)
Latest run, Azure agent only (GPT-4o-mini; 5-17k tokens and 2-4 Tavily searches per course):
**2 courses found correctly, no wrong descriptions accepted, 1 more identified.**

| Course | Outcome |
|---|---|
| Bristol AST 192 Intro to Astrobiology | **Found**: AST 192 Life in the Universe - Introduction to Astrobiology, 4 credits, full description from Bristol's 2019-2020 catalog PDF (the edition for the student's term) |
| New Haven ECON 1130 Intro to Microeconomics | **Found**: ECON 1134 Introduction to Microeconomics (code misread), full description, from the 2022-2023 catalog (the student took it in 2019; the description matches the legacy EC 134 entry) |
| New Haven UNIV 1141 "Acid Research Project" | Identified as UNIV 1141 Introduction to Academic Research and Project Management ("Acad" misread); title check too far apart to accept automatically |
| New Haven PHYS 1111 Lab-Phys 1100 | Not found. The agent saw the lab listed (0 credits) with no description |
| New Haven MUSC 1110 College Algebra | Not found (really MATH 1110) |
| New Haven SDEV 1000 Web Dev Placement | Not found (likely a placement record, not a course) |
| Bristol HOM 172 Coping with Life & Death | Not found |

Not applied yet: grade minimums, credit caps, repeat rules.
