# SAM.gov exclusions extract probe, 2026-09-14

The exclusions (debarment and suspension) list SAM.gov publishes as a daily public file,
checked live because the alternative in the issue was the keyed Exclusions API v4 and this
turns out to need no key at all. Re-verify before the first release, and whenever the adapter
starts failing on a column.

## The file

```
GET https://sam.gov/api/prod/fileextractservices/v1/api/download/
    Exclusions/Public%20V2/SAM_Exclusions_Public_Extract_V2_{YYJJJ}.ZIP?privacy=Public
```

No API key, no quota, no `api_requests` row. It is the same file service `orrery ingest bulk`
already reads the contract-opportunity extracts from, and it answers `303` to a presigned S3
URL, so the redirect must be followed.

`YYJJJ` is a two-digit year and a three-digit day of the year: `26257` is 2026-09-14. The
file for a given day appears during that UTC day, so early in the day the current file is
still yesterday's name.

A name with no file behind it (`26258`, checked the same day) answers **`204 No Content`**,
not `404`. 204 is a success status, so a client that only checks `response.is_success`
writes a zero-byte file and then fails on it, or worse caches it. `orrery.ingest.download`
therefore treats an empty body as a failed download whatever the status, which is what makes
the fall back to yesterday's name work.

## What came back

| Measure | Value |
|---|---|
| Zip | 11,840,108 bytes |
| Members | one, `SAM_Exclusions_Public_Extract_V2_26257.CSV` |
| CSV | 78,196,942 bytes, UTF-8 |
| Rows | 168,452 (excluding the header) |
| Columns | 31 |

Columns, in order: `Classification`, `Name`, `Prefix`, `First`, `Middle`, `Last`, `Suffix`,
`Address 1`, `Address 2`, `Address 3`, `Address 4`, `City`, `State / Province`, `Country`,
`Zip Code`, `Open Data Flag`, `Blank (Deprecated)`, `Unique Entity ID`, `Exclusion Program`,
`Excluding Agency`, `CT Code`, `Exclusion Type`, `Additional Comments`, `Active Date`,
`Termination Date`, `Record Status`, `Cross-Reference`, `SAM Number`, `CAGE`, `NPI`,
`Creation_Date`.

## What the values look like

| Column | Observed |
|---|---|
| `Classification` | `Individual` 133,266; `Special Entity Designation` 25,585; `Firm` 8,279; `Vessel` 1,322 |
| `Record Status` | `Active` for all 168,452 rows |
| `Exclusion Type` | `Prohibition/Restriction` 135,313; `Ineligible (Proceedings Completed)` 29,070; `Ineligible (Proceedings Pending)` 2,721; `Ineligible (Proceedings Complete)` 1,138; `Voluntary Exclusion` 210 |
| `Exclusion Program` | `Reciprocal` 159,307; `NonProcurement` 9,113; `Procurement` 32 |
| `Active Date` | ISO `YYYY-MM-DD` on 157,436 rows; blank on 11,016 |
| `Termination Date` | ISO on 9,307 rows; the literal `Indefinite` on 159,145 |
| `CT Code` | 30 distinct values, blank on 70,574 rows |
| Keys, non-individual rows | UEI only 33,758; UEI and CAGE 416; CAGE only 8; neither 1,004 |

Two things follow from this, and both are in the adapter:

**The file holds active records only.** Every row says `Active`, and an exclusion that has
ended is not in the file at all. So absence from a later complete file is the only way to
learn that one ended, which is why it is written as a `terminated` status fact at the file's
own cut rather than as a deletion, and why a run that did not finish the file terminates
nothing.

**Four rows in five are named individuals.** Those rows carry a person's name across six
columns and nothing orrery wants. They are counted and nothing else of them is read
(principle 8); the six person columns are blanked in every record that is stored, whatever
the row's classification; and only rows whose UEI or CAGE exactly matches a contractor the
store already holds are read at all, which is a few dozen rows of the 168,452 for a typical
store. Nothing in the file creates an entity or an alias.
