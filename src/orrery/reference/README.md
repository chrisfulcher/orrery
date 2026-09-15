# Reference code lists

Two code lists ship with orrery so that a notice's NAICS and PSC codes read as words on a
machine that has never been online. They live inside the package rather than under `data/`
so that they travel in the wheel and are not swallowed by the store's gitignore rule, and
`orrery db migrate` loads them into `naics_codes` and `psc_codes`.

Both are U.S. Government works, public domain. Neither file is edited by hand: each is a
straight `code,title` projection of a published spreadsheet, so a refresh is a re-run of the
snippet below and a diff.

## `naics_2022.csv` (2,125 rows)

- Source: U.S. Census Bureau, 2022 NAICS, 2- to 6-digit codes.
- URL: <https://www.census.gov/naics/2022NAICS/2-6%20digit_2022_Codes.xlsx> (82 KB, one sheet
  `tbl_2022_title_description_coun`), linked from <https://www.census.gov/naics/>.
- Retrieved: 2026-09-14.
- License: U.S. Government work, public domain.
- Contents: every code at levels 2 through 6, in the publisher's order. The three sector
  ranges are kept as the Census writes them (`31-33`, `44-45`, `48-49`), so a code in this
  file is always the code a source would print. Census marks some titles in some vintages
  with a trailing `T`; the 2022 file carries none, and the snippet strips one if a later
  vintage does.
- `sources` row: `census_naics`, adapter version `2022`.

## `psc_2025_04.csv` (2,540 rows)

- Source: GSA, Product and Service Codes Manual, April 2025 edition.
- URL: <https://www.acquisition.gov/sites/default/files/manual/PSC%20April%202025.xlsx>
  (452 KB, sheet `PSC for 042025`), linked from <https://www.acquisition.gov/psc-manual>.
- Retrieved: 2026-09-14.
- License: U.S. Government work, public domain.
- Contents: every active code the manual carries, at all three of its lengths: 25
  one-character groups, 171 two-character categories and 2,344 four-character product and
  service codes. The short codes are headings in the manual's layout but codes on a notice,
  and the government puts one on about a fifth of what orrery ingests, so a list of the
  four-character codes alone leaves those notices reading as a bare `99`. Retired codes sit
  alongside active ones and are marked with an end date, so the 3,568 rows carrying one are
  dropped (25 of them have no name either) and the 2,540 without one are kept; the header row
  is dropped by the same length test that picks the three code lengths out. The title is the
  manual's `PRODUCT AND SERVICE CODE NAME` column, which GSA publishes in upper case; the
  mixed-case `FULL NAME (DESCRIPTION)` column is empty for 877 of the active codes, so taking
  it would leave the list in two casings.
- `sources` row: `gsa_psc_manual`, adapter version `2025-04`.

## Refreshing

Download both spreadsheets into a scratch directory, then run this from the repository root
with the directory as the one argument. openpyxl is already a dependency. No conversion
script ships; this snippet is the record of how the files were made.

```python
import csv
import sys
from pathlib import Path

import openpyxl

src = Path(sys.argv[1])  # where the two .xlsx files were downloaded
out = Path("src/orrery/reference")

# NAICS: the one sheet, column B the code (an int, or "31-33" for a sector range), column C
# the title. Census marks some titles with a trailing "T"; the 2022 file has none, and the
# removesuffix below is a no-op guard for a vintage that does.
book = openpyxl.load_workbook(src / "2-6 digit_2022_Codes.xlsx", read_only=True, data_only=True)
naics = []
for code, title in ((r[1], r[2]) for r in book.worksheets[0].iter_rows(values_only=True)):
    if code is None or title is None or str(code).strip() == "2022 NAICS US   Code":
        continue
    naics.append((str(code).strip(), str(title).strip().removesuffix("T").strip()))
book.close()

# PSC: sheet "PSC for 042025". Column A the code (a float when it is all digits), column B the
# name, column D the end date -- set on a retired code, empty on an active one. Every active
# code is kept at all three lengths the manual uses: the four-character product and service
# codes and the one- and two-character group and category codes above them, which notices cite
# as well. The length test also drops the header row, whose "PSC CODE" is none of the three.
book = openpyxl.load_workbook(src / "PSC April 2025.xlsx", read_only=True, data_only=True)
psc = []
for code, name, _start, end in (r[:4] for r in book["PSC for 042025"].iter_rows(values_only=True)):
    if code is None or name is None or end is not None:
        continue
    code = str(int(code)) if isinstance(code, float) else str(code).strip()
    if len(code) in (1, 2, 4):
        psc.append((code, str(name).strip()))
book.close()

for name, rows in (("naics_2022.csv", naics), ("psc_2025_04.csv", psc)):
    with (out / name).open("w", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows([("code", "title"), *rows])
    print(name, len(rows))
```

A new vintage is a new file name, a new `sources` row and a new migration, not an edit in
place: `naics_codes` and `psc_codes` hold one vintage each today, and mapping a code across
vintages (2017 to 2022 moved wired telecom from 517311 to 517111, across the four-digit
boundary) is a separate piece of work.
