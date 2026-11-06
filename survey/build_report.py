"""
Turn catalog_survey.csv into Catalog_Scrapeability_Survey.xlsx: a Summary sheet (live COUNTIFS
formulas), the full school list, and a Ready sheet of schools whose catalogs can be scraped now.

Usage: python build_report.py catalog_survey.csv Catalog_Scrapeability_Survey.xlsx
"""
import sys, csv
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

INK, GOLD, PARCH = "00244E", "FDB913", "F4F0EC"
FONT = "Arial"
BUCKETS = [
    ("ready", "Scraper exists (CourseLeaf, SmartCatalogIQ, Coursedog); allowed and readable"),
    ("needs_adapter", "Allowed and readable; needs a new scraper for its platform or custom site"),
    ("pdf_only", "Catalog published as a PDF; readable per edition"),
    ("slow", "Allowed, but robots.txt asks for a crawl delay over 15 s (Acalog asks 120 s)"),
    ("blocked", "robots.txt disallows, robots.txt refused, or a bot check / access denied"),
    ("not_found", "No catalog found at common addresses or linked from the homepage"),
    ("unreachable", "Website did not respond"),
]
SECTORS = {"1": "Public 4-year", "2": "Private nonprofit 4-year", "3": "Private for-profit 4-year",
           "4": "Public 2-year", "5": "Private nonprofit 2-year", "6": "Private for-profit 2-year",
           "7": "Public <2-year", "8": "Private nonprofit <2-year", "9": "Private for-profit <2-year", "0": "Administrative unit"}

def header(ws, row, values):
    for c, v in enumerate(values, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=INK)
        cell.alignment = Alignment(vertical="center", wrap_text=True)

def body_font(ws):
    for row in ws.iter_rows():
        for cell in row:
            if cell.font is None or not cell.font.bold:
                cell.font = Font(name=FONT, size=10, color="1D1C18")

def main():
    src, out = sys.argv[1], sys.argv[2]
    rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    for r in rows:
        r["Sector"] = SECTORS.get(r["Sector"], r["Sector"])
    cols = list(rows[0].keys())
    wb = Workbook()

    # ---- All schools ----
    ws = wb.active
    ws.title = "All Schools"
    header(ws, 1, cols)
    for i, r in enumerate(rows, 2):
        for c, k in enumerate(cols, 1):
            v = r[k]
            ws.cell(row=i, column=c, value=float(v) if k == "Crawl delay (s)" and v not in ("",) and v.replace(".", "", 1).isdigit() else v)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(rows) + 1}"
    widths = {"Institution": 42, "Website": 30, "Catalog URL": 55, "Note": 55, "Sector": 24, "Platform": 18, "Bucket": 14}
    for c, k in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(c)].width = widths.get(k, 12)
    n = len(rows) + 1
    bcol = get_column_letter(cols.index("Bucket") + 1)
    pcol = get_column_letter(cols.index("Platform") + 1)
    acol = get_column_letter(cols.index("Institutionally accredited (DAPIP)") + 1)
    scol = get_column_letter(cols.index("Sector") + 1)

    # ---- Ready ----
    wr = wb.create_sheet("Ready")
    keep = ["UNITID", "Institution", "State", "Sector", "Institutionally accredited (DAPIP)", "Platform", "Catalog URL", "Note"]
    header(wr, 1, keep)
    ready = [r for r in rows if r["Bucket"] == "ready"]
    for i, r in enumerate(sorted(ready, key=lambda r: (r["Platform"], r["State"], r["Institution"])), 2):
        for c, k in enumerate(keep, 1):
            wr.cell(row=i, column=c, value=r[k])
    wr.freeze_panes = "C2"
    wr.auto_filter.ref = f"A1:{get_column_letter(len(keep))}{len(ready) + 1}"
    for c, k in enumerate(keep, 1):
        wr.column_dimensions[get_column_letter(c)].width = widths.get(k, 12)

    # ---- Summary (formulas over All Schools) ----
    s = wb.create_sheet("Summary", 0)
    s["A1"] = "Catalog scrapeability survey"
    s["A1"].font = Font(name=FONT, size=16, bold=True, color=INK)
    s["A2"] = ("Every active, degree-granting institution in IPEDS HD2023 (source: NCES IPEDS Directory file HD2023, "
               "nces.ed.gov). Branch campuses sharing a website were checked once. Checks: robots.txt plus 1-3 pages per site.")
    s["A2"].alignment = Alignment(wrap_text=True)
    s.merge_cells("A2:E2")
    s.row_dimensions[2].height = 45
    header(s, 4, ["Bucket", "Meaning", "Schools", "Share", "Accredited (DAPIP)"])
    for i, (b, meaning) in enumerate(BUCKETS, 5):
        s.cell(row=i, column=1, value=b)
        s.cell(row=i, column=2, value=meaning).alignment = Alignment(wrap_text=True)
        s.cell(row=i, column=3, value=f"=COUNTIFS('All Schools'!${bcol}$2:${bcol}${n},A{i})")
        s.cell(row=i, column=4, value=f"=IFERROR(C{i}/C{5 + len(BUCKETS)},0)").number_format = "0.0%"
        s.cell(row=i, column=5, value=f"=COUNTIFS('All Schools'!${bcol}$2:${bcol}${n},A{i},'All Schools'!${acol}$2:${acol}${n},\"yes\")")
    t = 5 + len(BUCKETS)
    s.cell(row=t, column=1, value="Total").font = Font(name=FONT, bold=True)
    s.cell(row=t, column=3, value=f"=SUM(C5:C{t - 1})").font = Font(name=FONT, bold=True)
    s.cell(row=t, column=4, value=f"=IFERROR(C{t}/C{t},0)").number_format = "0.0%"
    s.cell(row=t, column=5, value=f"=SUM(E5:E{t - 1})").font = Font(name=FONT, bold=True)
    for c in range(1, 6):
        s.cell(row=t, column=c).fill = PatternFill("solid", fgColor=GOLD)

    p0 = t + 2
    header(s, p0, ["Catalog platform", "Schools", "Of which ready", "Of which slow", "Of which blocked"])
    plats = sorted({r["Platform"] for r in rows if r["Platform"]}, key=lambda p: -sum(r["Platform"] == p for r in rows))
    for j, pl in enumerate(plats, p0 + 1):
        s.cell(row=j, column=1, value=pl)
        s.cell(row=j, column=2, value=f"=COUNTIFS('All Schools'!${pcol}$2:${pcol}${n},A{j})")
        for c, b in ((3, "ready"), (4, "slow"), (5, "blocked")):
            s.cell(row=j, column=c, value=f"=COUNTIFS('All Schools'!${pcol}$2:${pcol}${n},A{j},'All Schools'!${bcol}$2:${bcol}${n},\"{b}\")")

    q0 = p0 + len(plats) + 2
    header(s, q0, ["Sector", "Schools", "Ready", "Needs scraper", "Blocked or slow"])
    for j, sec in enumerate(sorted({r["Sector"] for r in rows}), q0 + 1):
        s.cell(row=j, column=1, value=sec)
        s.cell(row=j, column=2, value=f"=COUNTIFS('All Schools'!${scol}$2:${scol}${n},A{j})")
        s.cell(row=j, column=3, value=f"=COUNTIFS('All Schools'!${scol}$2:${scol}${n},A{j},'All Schools'!${bcol}$2:${bcol}${n},\"ready\")")
        s.cell(row=j, column=4, value=f"=COUNTIFS('All Schools'!${scol}$2:${scol}${n},A{j},'All Schools'!${bcol}$2:${bcol}${n},\"needs_adapter\")")
        s.cell(row=j, column=5, value=f"=COUNTIFS('All Schools'!${scol}$2:${scol}${n},A{j},'All Schools'!${bcol}$2:${bcol}${n},\"blocked\")"
                                       f"+COUNTIFS('All Schools'!${scol}$2:${scol}${n},A{j},'All Schools'!${bcol}$2:${bcol}${n},\"slow\")")
    for col, w in zip("ABCDE", (26, 70, 12, 14, 18)):
        s.column_dimensions[col].width = w

    for sh in (s, ws, wr):
        body_font(sh)
    s["A1"].font = Font(name=FONT, size=16, bold=True, color=INK)
    wb.save(out)
    print(f"wrote {out}: {len(rows)} schools, {len(ready)} ready")

if __name__ == "__main__":
    main()
