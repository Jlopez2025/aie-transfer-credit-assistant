# Catalog scrapeability survey

Which sending schools' online catalogs can be scraped ahead of time to build OKF files.

- Universe: every active, degree-granting institution in IPEDS HD2023 (4,096 schools on 3,483
  websites; branch campuses sharing a website checked once), marked institutionally accredited or
  not from DAPIP. Source file: `../ipeds_raw/HD2023.zip` (NCES IPEDS Directory, nces.ed.gov).
- Per site: robots.txt plus 1-3 pages, honest user agent. Looks for `catalog.`, `catalogs.`,
  `bulletin.`, `ecatalog.` subdomains, a catalog link on the homepage, and common `/catalog` paths;
  follows one link from a catalog landing page to a platform or PDF.
- Run: `python survey.py HD2023.csv DAPIP_Institutionally_Accredited.csv catalog_survey.csv`
  (resumable), then `python build_report.py catalog_survey.csv ../Catalog_Scrapeability_Survey.xlsx`.

Results (2026-11-05): ready 610, needs_adapter 640, pdf_only 877, slow 649, blocked 548,
not_found 639, unreachable 133. Bucket meanings are on the workbook's Summary sheet.

Caveats: platform detection is by fingerprints in addresses and page HTML; Coursedog counts as
ready on the strength of one working scraper (Bristol); "allowed by robots.txt" is not the same as
permission under a site's terms of use; "not_found" includes catalogs reachable only through
JavaScript menus or unusual addresses.
