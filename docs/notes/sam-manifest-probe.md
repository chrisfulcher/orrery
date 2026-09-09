# SAM.gov attachment manifest probe, 2026-09-09

The attachment list SAM.gov's own web interface reads, checked live so that discovery is built
on observed behaviour rather than on documentation, because there is none. Re-verify before the
first release, and whenever the manifest stage starts failing.

## The endpoint

```
GET https://sam.gov/api/prod/opps/v3/opportunities/{notice_id}/resources
Accept: application/hal+json
```

No API key. No quota. No `api_requests` row. It is the same host and the same `opps/v3` path
family that every attachment download already uses.

## Requests and results

| # | Request | Key | Status | Notes |
|---|---|---|---|---|
| 1 | `.../{id}/resources` with `Accept: */*` | no | 406 | `Acceptable representations: [application/hal+json]` |
| 2 | `.../{id}/resources`, notice with attachments | no | 200 | `_embedded.opportunityAttachmentList[].attachments[]` |
| 3 | `.../{id}/resources`, notice with none | no | 200 | **no `_embedded` key at all**, only `_links` |
| 4 | `.../{id}/resources`, unknown id | no | **400** | `{"errors":{"status":"BAD_REQUEST","details":"Record not found"}}` |
| 5 | `.../resources/files/{resourceId}/download` | no | 303 → 200 | 261,041-byte PDF, matching the declared `size` |

Request 5's URL is byte-for-byte the shape the keyed search already puts in
`attachments.url`, so both discovery paths produce identical rows. The 303 goes to a presigned
S3 URL valid for about nine seconds, so the redirect must be followed immediately;
`SamClient.download` already streams with `follow_redirects=True`.

## Fields on an attachment entry

`resourceId`, `name`, `mimeType`, `size`, `postedDate`, `accessStatus`, `exportControlled`,
`deletedFlag`, `attachmentId`, `attachmentOrder`, `fileExists`, `type`.

`exportControlled` and `deletedFlag` are string flags (`"0"` / `"1"`), not booleans.

## Findings that shape the design

1. **No attachments is a 200 with no `_embedded`, not an empty list.** "Checked and found
   nothing" therefore has to be recorded per notice, or those notices are asked about on every
   run forever. This is why `notices.manifest_status` exists rather than inferring the state
   from whether any `attachments` rows are present.
2. **An unknown notice is a 400, not a 404.** Here a 400 means the id is not usable rather than
   that the request was malformed, and either way it is terminal for that notice, so it is
   recorded as `unknown` and never retried.
3. **The declared `size` and `mimeType` arrive before the download.** An oversized file can be
   skipped without spending a request, and the filename and type are known in advance instead of
   being learned from a `Content-Disposition` header at fetch time.
4. **There is no published contract.** This is the interface the web UI calls, not the
   documented `api.sam.gov` API, and it can change without notice. The client pins `v3`, parses
   strictly, keeps the raw entry on the row, and stops the manifest stage on the first entry it
   cannot read rather than writing rows it does not understand. If the endpoint goes away,
   behaviour degrades to what it was before: attachments still arrive through `resourceLinks`
   on the keyed path.

## Yield, measured the same day

Sampled against notices backfilled from the bulk extract in one NAICS slice:

| measure | value |
|---|---|
| notices sampled | 12 |
| with at least one public attachment | 10 |
| public attachment files | 67 |
| mean files per notice | 5.6 |
| mean bytes per notice | ~2.4 MB |

Across a wider 25-notice sample, all 206 files were `public`, and the types were 145 `.pdf`,
45 `.docx`, 11 `.xlsx` and 5 unstated — so roughly 30% of attachment text is currently
unreachable, and a `.docx` reader alone would close about three quarters of that gap (#11).
