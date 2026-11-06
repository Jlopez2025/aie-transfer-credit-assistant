# External Institution OKF — Format Spec

Mirrors the SNHU OKF course format so external (sending-institution) catalogs drop
into the same RAG pipeline. Matching is directional: **external course description →
nearest SNHU course description** (the SNHU `okf_courses` set is the match target).

## File unit
- **One file per institution**: `external_okf/<Institution_Slug>.md`
- Courses are `##` headings inside, same as SNHU subject files use `## ACC-201 — …`.
- (If a school is huge, it can be split per-subject later; one-file-per-institution is the default.)

## Frontmatter
```yaml
---
type: "external-course-catalog"     # distinguishes from SNHU "course-prerequisites"
institution: "Bunker Hill Community College"
state: "MA"
city: "Boston"
dapip_id: "..."                      # keys back to the 9,640 accredited-institutions list
ope_id: "..."
ipeds_unitid: "..."
accreditor: "New England Commission of Higher Education"
accreditation_status: "Accredited"  # transfer-eligibility provenance
catalog_source: "Acalog | CourseLeaf | PDF | ..."
catalog_url: "https://..."
retrieved: "2026-11-04"
course_count: 412
owner: "generated; do not hand-edit metadata"
---
```

## Course entry (mirrors SNHU OKF)
```markdown
## ACCT-101 — Principles of Financial Accounting `[undergraduate]`

Introduces the complete accounting cycle for service and merchandising businesses:
journalizing, posting, adjusting and closing entries, and preparation of financial
statements under GAAP.

Credits: 3

> source: https://<catalog-url> (retrieved 2026-11-04)
```

- **Heading**: `## <COURSE CODE> — <Title> \`[level]\`` (level = `undergraduate` / `graduate`).
- **Description paragraph** directly under the heading — this is the **embedding/match target**, identical to how SNHU descriptions sit under their headings.
- `Credits:` line is optional but recommended (helps credit-equivalency rules).
- A `> source:` blockquote carries provenance per course (parallels SNHU's `> source text:`).

## Why this shape
The SNHU side (`okf_courses/ACC.md`, etc.) already puts a clean description paragraph
directly under each `## CODE — Title` heading. By matching that exact shape, one chunker
splits both corpora identically: each `##` block becomes one embeddable record whose
vector is built from the description paragraph. Transfer matching is then a
nearest-neighbor lookup from an external block into the SNHU block space, returning
ranked SNHU equivalents + a similarity score for a human evaluator to confirm.

## What is NOT in here (and where it comes from)
- **DAPIP** supplies only the frontmatter (institution identity + accreditation).
- **Course titles + descriptions** come from each institution's own catalog
  (scrape / dataset / transcript feed) — that is the ingestion step still to be wired up.
