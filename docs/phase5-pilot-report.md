# Phase 5 pilot report

**Status: live pilot BLOCKED. No live collection was performed. Every number below is from
offline, synthetic test runs.**

Date: 2026-11-16

## Live pilot

| Item | Result |
| --- | --- |
| Attempted companies (live) | 0 |
| Successfully processed (live) | 0 |
| Blocked / failed (live) | 0 / 0 |
| Jobs extracted / stored / exported (live) | 0 / 0 / 0 |

### Why it was not run

1. **No authorized source list.** `collector run` needs a sources CSV
   (`platform,company_name,identifier,company_website`). The only company list in the
   repository, `collector/companies/seed_companies.csv`, contains a header and no rows. Its
   columns (`company_name,website,careers_url`) are also not a sources file, so
   `collector run --sources collector/companies/seed_companies.csv` stops with
   `sources file lacks column(s): platform, identifier` before any request.
2. **No recorded permission.** No company or platform has been confirmed as permitting this
   collection. Public availability of an endpoint is not treated as permission, and no company
   URLs were invented.
3. **The execution environment refused live crawl commands.** An attempt to run `collector run`,
   even against the empty seed file, was denied by the session's permission policy.

### What is needed to run it

- A sources CSV listing 5 to 10 companies whose collection is explicitly permitted, with the
  platform (`greenhouse`, `lever`, `ashby` or `jsonld`) and board identifier or page URL for
  each.
- Confirmation of the permitted collection method for each source (for example the platform's
  documented public job-board API).
- Explicit authorization to make live network requests from the machine that runs the pilot.

The pilot is then two commands:

```bash
collector run --sources pilot_sources.csv --db data/pilot.db
collector export --db data/pilot.db --output output/pilot-jobs.xlsx
```

All existing protections stay on: robots.txt, the request delay and concurrency limits,
HTTPS only, exact-host redirects, connection-level public-IP checks, TLS verification, no
proxies, and blocking on HTTP 403/429 and challenge pages. Before the first live run, the
pre-live checks from Phase 4 Step 3 still apply: one real-network smoke test against an approved
host, and a real TLS handshake.

## Offline end-to-end results (synthetic)

These come from `test_offline_crawl_to_sqlite_to_excel_end_to_end` in
`tests/test_excel_export.py`. The scenario uses the real Scrapy engine, adapters, pipeline,
SQLite store and exporter. The responses come from the synthetic fixtures in `tests/fixtures/`
through a scripted download handler, with sockets blocked.

Collection run (reference date 2026-10-20):

| Item | Count |
| --- | --- |
| Sources attempted | 7 (Greenhouse, Lever, Ashby, 2 JSON-LD mirror pages, 1 blocked JSON-LD page, 1 malformed Greenhouse response) |
| Sources processed successfully | 5 |
| Sources blocked | 1 (HTTP 403) |
| Sources failed | 2 (the blocked one plus 1 unusable response) |
| Records extracted | 7 |
| Same-run duplicates | 2 (the mirror page repeats the same two postings) |
| Unique records | 5 |
| Rejected jobs | 0 job-level rejections; 1 response-level extraction failure (the malformed body) |
| Stored (new) | 5 |
| Eligible / unknown date / old / future | 3 / 2 / 0 / 0 |
| Run status | FAILED (a source was blocked or failed); the healthy jobs stay stored |

The test runs the same crawl a second time: 0 new, 5 seen, 0 updated, and still 5 job rows.

Export two weeks later (reference 2026-11-04):

| Item | Count |
| --- | --- |
| Rows written | 3 |
| Eligible | 1 |
| Unknown date (labelled for review) | 2 |
| Excluded as old | 2 (the JSON-LD postings dated 2026-10-01 and 2026-10-03) |
| Excluded as closed | 0 |

## Export performance (synthetic, measured on Windows, Python 3.13)

Peak working set of a fresh process exporting a prebuilt database:

| Stored jobs | Rows exported | Time | Peak working set |
| --- | --- | --- | --- |
| 20,000 | 16,000 | about 12-15 s | 97 MB |
| 100,000 | 80,000 | about 62 s | 247 MB |
| 100,000, no hyperlinks (diagnostic only) | 80,000 | about 55 s | 62 MB |

Rows are streamed in batches of 1,000 from SQLite into a write-only workbook. The remaining
growth comes from openpyxl keeping hyperlinks until it saves, about 2 KB per clickable
application URL. These figures do not establish the memory needed on a 2 vCPU / 4 GB server;
they were measured on a development machine.
