# USAspending and SAM.gov entity API probe, 2026-09-07

Live requests made before building the second and third adapters (design §5 sources 3 and 4,
§9 step 8), recorded so the clients are built on observed behavior. USAspending needs no key.
The three SAM.gov requests used a personal API key with no role on an entity registration and
were made outside the local quota accounting, so they do not appear in `api_requests`.
Re-verify before the first release.

## USAspending (no key, no quota)

| # | Request | Status | Notes |
|---|---|---|---|
| 1 | `POST api.usaspending.gov/api/v2/download/awards/` with `filters` = NAICS 541512, award types A–D, action dates FY2025, `file_format: csv` | 200 in 1.4 s | Body: `status_url`, `file_name`, `file_url` (absolute, on `files.usaspending.gov`), and a `download_request` echo with `limit: 500000` and the expanded filter |
| 2 | `GET .../download/status?file_name=…`, every 15 s | 200 | `status` went `ready` → `running` (with `seconds_elapsed`) → `finished` after 312 s; final body carries `total_rows` 17,779, `total_columns` 404, `total_size` 3212 KB, `file_url` |
| 3 | `POST .../search/spending_by_award/`, same filters, 16 contract fields, `sort: "Award Amount"`, `limit: 5` | 200 | `results`, `page_metadata` (`page`, `hasNext`, `last_record_unique_id`, `last_record_sort_value`), and a `messages` note that searches are limited to dates from 2007-10-01 |
| 4 | `GET .../awards/<generated_unique_award_id>/` | 200 | `piid`, `type`, `total_obligation`, `base_and_all_options`, `date_signed`, `parent_award`, `awarding_agency` (toptier and subtier with codes, `office_agency_name` without a code), `recipient.recipient_uei`, `period_of_performance`, `latest_transaction_contract_data` (`solicitation_identifier`, `extent_competed`, `naics`, `product_or_service_code`, `type_of_set_aside`, …), `executive_details.officers` |
| 5 | `GET files.usaspending.gov/generated_downloads/<file_name>` | 200, no redirect | 3.2 MB zip, `binary/octet-stream`, `ETag` and `Last-Modified` present; 2.4 s |

The zip holds two CSVs: `Contracts_PrimeAwardSummaries_<stamp>_1.csv` (22 MB, 286 columns,
10,405 rows, one per award) and `Contracts_Subawards_<stamp>_1.csv` (ignored). `total_rows`
in the status body counts both files. The award file is UTF-8 with a BOM and CRLF line ends.

### Findings that shape the adapter

1. **The award-level CSV has everything the graph needs, keyed.** `contract_award_unique_key`
   (unique, the same id the award page uses), `award_id_piid`, `parent_award_id_piid`,
   `awarding_agency_code`/`name`, `awarding_sub_agency_code`/`name`, **`awarding_office_code`**
   (the AAC, the last segment of a SAM.gov `fullParentPathCode`), `awarding_office_name`,
   **`recipient_uei`**, `recipient_name`, `cage_code`, `recipient_parent_uei`,
   **`solicitation_identifier`**, `naics_code`, `product_or_service_code`, `award_type_code`,
   `type_of_set_aside_code`, `extent_competed_code`, `award_base_action_date`,
   `award_latest_action_date`, `period_of_performance_start_date`,
   `period_of_performance_current_end_date`, `current_total_value_of_award`,
   `potential_total_value_of_award`, `total_obligated_amount`, `usaspending_permalink`,
   `last_modified_date`. The search API exposes no office code and the award page exposes
   only the office name, so the download is the only path that resolves awards to offices.
2. **Fill rates in the FY2025 slice:** no null UEIs (the DUNS era is before 2022), one null
   CAGE, `solicitation_identifier` filled on 69% of rows (7,196), and two of those match a
   solicitation number already in the store. Award types: C 9,082, A 527, B 444, D 352.
   68% of rows are Department of Defense.
3. **Office overlap is real but partial.** 721 distinct awarding offices; 66 of them (1,400
   rows) are already office entities in a store fed only by SAM.gov notices in the same
   NAICS. The rest must resolve to nothing and be recorded as unresolved aliases.
4. **USAspending toptier codes are not SAM.gov CGAC codes for DoD.** Army, Navy, and Air Force
   awards carry `awarding_agency_code` `097` (Department of Defense) with the component as
   the sub-agency, whereas SAM.gov paths start with the component's own CGAC (`021.2100` for
   Army). The triple is therefore not a shared key; only the office code is. Never create
   agency or office entities from USAspending.
5. **Preparation is slow, download is fast.** Five minutes to build a 10,000-row slice.
   The poll loop must tolerate that, and a `failed` status must abort with the message.
6. **The file carries data the graph must not keep.** `recipient_phone_number`,
   `recipient_fax_number`, and `highly_compensated_officer_{1..5}_name`/`_amount` are vendor
   employees, not officials in a public capacity (principle 8). The adapter drops them
   before storing `raw_json`. The same vendor can register more than one UEI (two in this
   slice for the top recipient); entities are keyed by UEI and share the name as an alias.
7. **No contracting officer anywhere.** Neither the CSV nor the award page names one, so the
   officials panel comes from SAM.gov notice points of contact.
8. **No rate-limit headers.** Politeness only: one download request per run, status polls
   spaced by the configured delay, and the API's own `User-Agent` convention honored.

## SAM.gov entity registrations (keyed; 3 requests spent)

| # | Request | Status | Notes |
|---|---|---|---|
| 1 | `GET api.sam.gov/data-services/v1/extracts?fileType=ENTITY&sensitivity=PUBLIC&frequency=MONTHLY&charset=UTF-8&api_key=…` | 302 in 0.5 s | `Location` is a presigned S3 URL on `falextracts.s3.amazonaws.com` (`X-Amz-Expires=3600`, no API key in it) for `Entity Registration/Public V2/SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.ZIP`. Following it: 200, `binary/octet-stream`, 147 MB, `ETag` and `Last-Modified` present, no `Content-Disposition`. 4.7 s to download. |
| 2 | `GET api.sam.gov/entity-information/v3/entities?ueiSAM=<uei>&includeSections=entityRegistration,coreData,assertions,pointsOfContact&api_key=…` | 200 in 0.3 s | `totalRecords`, `entityData[]`, `links.selfLink` (with `REPLACE_WITH_API_KEY`, `page=0`, `size=10`) |
| 3 | `GET .../v3/entities?primaryNaics=541512&registrationStatus=A&format=json&api_key=…` | 200 in 0.1 s | A plain-text sentence: the extract "will be available for download with url `…/v3/download-entities?api_key=REPLACE_WITH_API_KEY&token=<10 chars>` in some time". Collecting it costs another keyed request per attempt, so nothing is built on this path. |

### The monthly public extract

- One member, `SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.dat`, 566 MB, 895,932 records
  (809,521 active, 86,106 expired), each a pipe-delimited line of 142 fields ending in
  `!end`, between a `BOF PUBLIC V2 00000000 20260906 0895932 0008331` header and the matching
  `EOF` trailer. A streaming pass over the whole file takes about 4 s.
- 305 records (0.03%) split into more than 142 fields because a free-text field (the DBA
  name, an address line, or a point-of-contact field) contains a pipe. The parser counts and
  skips them rather than guessing which field overflowed.
- Field positions verified against the layout (1-based): 1 UEI, 4 CAGE, 5 DoDAAC, 6 extract
  code (`A` active, `E` expired), 7 purpose of registration, 8 initial registration date,
  9 expiration date, 10 last update, 11 activation date, 12 legal business name, 13 DBA name,
  16–23 physical address (line 1, line 2, city, state, ZIP, ZIP+4, country, congressional
  district), 25 entity start date, 27 URL, 28 entity structure, 29–30 state and country of
  incorporation, 31–32 business type counter and `~`-separated string, 33 primary NAICS,
  34–35 NAICS counter and string, 36–37 PSC counter and string, 40–46 mailing address,
  47–112 six point-of-contact groups (never stored), 113–114 NAICS exceptions, 115 debt
  subject to offset, 116 exclusion status flag, 117–118 SBA business types, 119 no-public-
  display flag, 120–121 disaster response, 122–141 flex fields, 142 `!end`.
- List entries carry trailing flags or whitespace: NAICS entries look like `541512Y`,
  `541512N`, or `541512 ` (the letter is the small-business flag for that code), so a parser
  takes the first six characters and the flag separately. Counters read `0000` with an empty
  string when a list is empty.
- The slice: 11,132 registrants list 541512 as their primary NAICS and 23,654 list it at all.

### The Entity Management API v3

- `entityRegistration`: `ueiSAM`, `cageCode`, `dodaac`, `legalBusinessName`, `dbaName`,
  `purposeOfRegistrationCode`/`Desc`, `registrationStatus` (`Active`), `registrationDate`,
  `lastUpdateDate`, `registrationExpirationDate`, `activationDate`, `ueiStatus`,
  `publicDisplayFlag`, `exclusionStatusFlag`.
- `coreData`: `generalInformation` (`entityStructureCode`/`Desc`, `profitStructureCode`,
  `organizationStructureCode`, state and country of incorporation), `physicalAddress`,
  `mailingAddress`, `businessTypes.businessTypeList[]` (`businessTypeCode`/`Desc`) and
  `sbaBusinessTypeList[]` (a single all-null entry when there are none),
  `entityInformation`, `financialInformation`, `congressionalDistrict`.
- `assertions.goodsAndServices`: `primaryNaics`, `naicsList[]` (`naicsCode`,
  `naicsDescription`, `sbaSmallBusiness`, `naicsException`), `pscList[]` (a single all-null
  entry when empty).
- `pointsOfContact`: six groups, each with names, title, and a postal address, no email or
  phone at the public level. Never stored.
- No rate-limit headers on any response; accounting stays local.

## Fixtures captured

- `tests/fixtures/usaspending_download_awards.json`: request 1's body, verbatim.
- `tests/fixtures/usaspending_download_status.json`: the final status body, verbatim.
- `tests/fixtures/usaspending_awards_sample.csv`: the verbatim 286-column header and eight
  rows, all awards to large public contractors, chosen to cover DoD and civilian offices,
  base awards and orders, and filled and empty solicitation identifiers. Recipient phone and
  fax and the compensated-officer columns are blanked.
- `tests/fixtures/sam_entity_v3.json`: request 2's body, verbatim except that every
  point-of-contact name and title is a placeholder.
- `tests/fixtures/sam_entity_extract_sample.txt`: the `BOF` line, three records (two large
  active contractors and one expired public university), and the `EOF` line, with fields
  47–112 blanked.
- The downloaded zips stay under the data directory and are never committed.
