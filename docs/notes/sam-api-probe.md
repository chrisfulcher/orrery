# SAM.gov API probe, 2026-09-06

Three live requests with a personal API key (no role on an entity registration), NAICS
541512, posted 2026-08-30 to 2026-09-06. Recorded here so the client and the quota model are
built on observed behavior rather than documentation. Re-verify before the first release.

## Requests and results

| # | Request | Key | Status | Notes |
|---|---|---|---|---|
| 1 | `GET api.sam.gov/opportunities/v2/search?postedFrom=MM/dd/yyyy&postedTo=MM/dd/yyyy&ncode=541512&limit=25` | yes | 200 | 29 total records, 25 returned, 47 KB JSON |
| 2 | `GET api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=<id>` | yes | 200 | `application/hal+json`, body `{"description": "<html>"}` |
| 3 | `GET sam.gov/api/prod/opps/v3/opportunities/resources/files/<id>/download` | **no** | 200 | 951 KB PDF served by S3 |

The attachment downloaded without the key on the first try, so the planned fourth request
(same URL with the key) was not needed. Two of the five budgeted requests were unspent.

## Findings that change the design

1. **Attachment downloads do not need the key and are not API requests.** `resourceLinks`
   point at `sam.gov/api/prod/opps/v3/opportunities/resources/files/<file id>/download`,
   served straight from S3 (`Server: AmazonS3`, `ETag`, `Last-Modified`, `Accept-Ranges`).
   Attachment fetching is therefore bounded by politeness, not by the API quota.
2. **Every notice description costs one keyed request.** The search response's
   `description` field is a URL for all 25 notices, never text. The description endpoint
   returns HAL JSON whose `description` value is an HTML fragment with entities encoded.
3. **No quota or rate-limit headers exist.** Neither endpoint returns anything resembling
   `X-RateLimit-*`; the only per-request identifier is `activityid`. Quota accounting must
   be local, counting our own keyed requests per day.

Budget consequence: a notice costs its share of one search page plus one description
request; its attachments cost nothing. At ~10 requests/day (this key) that is one search
page and a handful of descriptions; at ~1,000/day, several hundred descriptions.

## Field observations (v2 search)

- Top level: `totalRecords`, `limit`, `offset`, `opportunitiesData`, `links` (self).
- Per notice: `noticeId` (32 hex chars), `title`, `solicitationNumber`, `type` and
  `baseType` (seen: Solicitation, Combined Synopsis/Solicitation, Sources Sought, Special
  Notice, Justification), `postedDate` (`YYYY-MM-DD`), `responseDeadLine` (ISO-8601 with a
  UTC offset, e.g. `2026-09-14T08:00:00-04:00`, or null), `archiveType` (`auto15`,
  `auto30`, `autocustom`), `archiveDate`, `active` (the string `"Yes"`), `naicsCode` plus
  `naicsCodes` (list), `classificationCode` (PSC), `typeOfSetAside` (seen: `SBA`, `NONE`,
  empty string, null) and `typeOfSetAsideDescription`, `organizationType` (OFFICE,
  DEPARTMENT, MAJOR COMMAND), `uiLink`, `additionalInfoLink` (always null here).
- `fullParentPathCode` / `fullParentPathName`: dot-separated, 1 to 7 segments in this sample.
  The deprecated `department` / `subTier` / `office` fields are absent.
- `pointOfContact`: list of `{type: primary|secondary, fullName, email, phone, fax}`.
  Scrubbed to placeholders in the fixture.
- `placeOfPerformance`: nested `{city: {code, name}, state: {code, name}, country: {code, name}}`,
  sometimes partial. `officeAddress`: `{zipcode, city, countryCode, state}`.
- `award`: present on Justification and some Sources Sought rows; `{date, number}` or an
  `awardee` object that was empty in every case seen.
- `resourceLinks`: 0 to 17 per notice (22 of 25 had at least one; 88 links in total).
  URLs are opaque; the filename arrives in the download's `Content-Disposition`
  (`attachment; filename=Name+With+Plus+Signs.pdf`) and `Content-Type` is always
  `application/octet-stream`, so file type must be sniffed from content.
- `links[0].href` gives a single-notice lookup: `.../v2/search?noticeid=<id>&limit=1`.

## Fixtures captured

- `tests/fixtures/sam_search_v2.json`: five notices chosen for coverage (an award with one
  attachment, a small-business set-aside with eleven, a `NONE` set-aside with seven, a
  one-segment agency path, and a notice with no attachments). Point-of-contact names,
  emails, phones, and faxes replaced with placeholders; everything else verbatim.
- `tests/fixtures/sam_noticedesc_v1.json`: the description endpoint's shape with a synthetic
  body.
- The downloaded PDF is not committed; extraction tests generate their own.
