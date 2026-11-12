# Platform field mapping (Phase 2)

Research and fixture notes for the interfaces the collector will read in later phases. **No
adapter or spider exists yet**; this document describes what each fixture models and how its
fields are *proposed* to map onto the existing `JobRecord` ([collector/models.py](../collector/models.py)).
`JobRecord` is unchanged.

## Read this first

- **Every fixture is synthetic.** They were written for tests, modelled on the public
  documentation listed below. **None is a captured response.** No request was made to any
  company board or career site while preparing them; only documentation pages were read.
  Synthetic fixtures use invented ids and the made-up board name `example-corp-fixture` on the
  real platform hostnames (so URL shapes are realistic), `example.com` for everything else.
- [tests/fixtures/manifest.json](../tests/fixtures/manifest.json) is the authority for each
  fixture: scenario, provenance, expected counts and documented source fields. HTML fixtures
  also carry a visible `SYNTHETIC FIXTURE` comment. A fixture may only be relabelled
  `captured` with a `captured_at` date and a `source_url` (enforced by tests).
- Items marked **ASSUMPTION** are not confirmed by official documentation and must be validated
  against live responses, politely and within robots/terms, before Phase 3 relies on them.

## Sources

| Interface | Reference | Status |
| --- | --- | --- |
| Greenhouse Job Board API | <https://docs.greenhouse.io/job-board.html> | official, read |
| Lever Postings API | <https://github.com/lever/postings-api> | official README, read; `createdAt` unit **not** confirmed there |
| Ashby Job Postings API | <https://developers.ashbyhq.com/docs/public-job-posting-api> | official, read; `id` and compensation placement not reliably documented |
| JSON-LD `JobPosting` | <https://developers.google.com/search/docs/appearance/structured-data/job-posting> and <https://schema.org/JobPosting> | official, read |
| Lever `createdAt` as epoch ms | third-party scraper/wrapper descriptions (search results) | **unofficial, unverified** |

## Platform research summary

| Question | Greenhouse | Lever | Ashby | JSON-LD `JobPosting` |
| --- | --- | --- | --- | --- |
| Listing | `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs` → `{"jobs": [...], "meta": {"total"}}`; add `?content=true` for description, departments, offices | `GET https://api.lever.co/v0/postings/{site}?mode=json` → bare JSON array (EU: `api.eu.lever.co`) | `GET https://api.ashbyhq.com/posting-api/job-board/{board}` → `{"apiVersion", "jobs": [...]}` | HTML page(s) embedding `<script type="application/ld+json">` |
| Detail | `GET .../jobs/{id}` (adds `company_name`, `first_published`; optional `questions`, `pay_transparency`) | none separate: the list item is complete | none separate: the list item is complete | the same page, or the job's own page |
| Job id | `id` (post id), `internal_job_id` (ATS job, null for prospects) | `id` (UUID string) | `id` (**ASSUMPTION**: UUID; absent from the official field table) | `identifier` (`PropertyValue.value`), optional |
| Title | `title` | `text` | `title` | `title` |
| Company | `company_name` **detail only**; otherwise board token/config | not in response; site slug/config | not in response; board name/config | `hiringOrganization.name` (or `"confidential"`) |
| Location | `location.name` free text; `offices[].location` | `categories.location`, `categories.allLocations[]`, `country` (ISO alpha-2 or null) | `location`, `secondaryLocations[].location`, `address.postalAddress` | `jobLocation` (`Place`/`PostalAddress`, may be an array), `applicantLocationRequirements` |
| Remote/hybrid/on-site | **no field**; free text only (and possibly custom `metadata`) | `workplaceType`: `unspecified`, `on-site`, `remote`, `hybrid` | `workplaceType`: `OnSite`, `Remote`, `Hybrid`; `isRemote` is true for Remote **and** Hybrid | `jobLocationType: "TELECOMMUTE"` means 100 % remote; no hybrid/on-site signal |
| Description | `content`: HTML, **entity-encoded** | `description`/`descriptionPlain`, plus `descriptionBody*`, `lists[]`, `additional*` | `descriptionHtml`, `descriptionPlain` | `description`: HTML (sometimes escaped or plain) |
| Employment type | none (public API) | `categories.commitment` free text | `employmentType`: `FullTime`, `PartTime`, `Intern`, `Contract`, `Temporary` | `employmentType`: `FULL_TIME`, `PART_TIME`, `CONTRACTOR`, `TEMPORARY`, `INTERN`, `VOLUNTEER`, `PER_DIEM`, `OTHER`; may be an array |
| Posting date | `first_published` **detail only** (ISO 8601, `Z`); `updated_at` (offset, e.g. `-05:00`) is *last modified* | `createdAt` (**ASSUMPTION**: epoch ms; creation, not necessarily publication) | `publishedAt` (ISO 8601 with offset; "last publication") | `datePosted` (`Date` or `DateTime`, ISO 8601, offset optional) |
| Apply URL | `absolute_url` (job page; may be on a custom domain with `?gh_jid=`) | `applyUrl` (form) and `hostedUrl` (posting page) | `applyUrl` and `jobUrl` | **no dedicated property**; `url` is the posting page; `directApply` is a boolean hint |
| Pagination | none documented (`meta.total`) | offset: `skip`, `limit`; no cursor or total documented | none documented | n/a (page-level) |
| Needs JavaScript | no | no | no | no for the embedded data; the host page may itself be JS-rendered |
| Authorization | none for GET (Basic auth only for application POST) | none documented for GET (key only for application POST) | not mentioned; used unauthenticated | none |
| Rate limits | not documented | GET not documented (application POST: 2 req/s) | not documented | n/a |

## Mapping to `JobRecord`

`platform`, `collected_at` and `source_url` are always set by the collector (not by the source):
`platform` is the platform name, `collected_at` is the fetch time (timezone-aware), and
`source_url` is the URL that was requested (list/API/page URL). **ASSUMPTION:** `source_url` =
requested URL.

Common rules for every platform:

- **Missing optional values stay `None`.** Nothing is guessed, and a missing posted date is never
  replaced by `collected_at` or `updated_at`.
- **Required values missing → reject** via the existing `validate_job` (`title`, `company_name`,
  `apply_url`, `source_url`, `platform`, `collected_at`). Tests show incomplete fixtures are
  rejected rather than repaired.
- **URLs** are normalized by the existing `normalize_url` when building job identity: tracking
  parameters (`utm_*`, ...) are dropped, meaningful ones kept (Greenhouse `gh_jid` **must** stay,
  see [list_multiple_locations.json](../tests/fixtures/greenhouse/list_multiple_locations.json)).
  Relative links on generic pages are resolved against the page URL first.
- **Dates:** `parse_posted_date` returns the date *as written in the source*; an explicit offset
  is not converted to the business timezone (`2026-10-02T18:30:00-05:00` → 2 Oct).
- **Descriptions** → `description_snippet`: decode entities where noted, strip tags, collapse
  whitespace, truncate. Length is a Phase 3 decision (`JobRecord` has no limit; 500 characters is
  suggested).
- **Identity:** `external_id` is only set when the platform guarantees stability; Phase 1 scopes
  it by `platform + company_name`. Known limitation (`Infosys` vs `Infosys Limited`): prefer the
  board token/site slug as scope in a later phase.

### Greenhouse

| JobRecord | Source | Notes |
| --- | --- | --- |
| `title` | `title` | |
| `company_name` | detail `company_name`, else seed/config | not in the list response |
| `apply_url` | `absolute_url` | job page; the board hosts the form |
| `external_id` | `id` | post id; `internal_job_id` is the ATS job and can be null |
| `location` | `location.name` | free text, may hold several places (`"A; B"`); stored unsplit |
| `work_type` | none → `None` | no structured field; inferring from the text "Remote" is a heuristic needing approval |
| `employment_type` | none → `None` | not in the public API |
| `salary_text` | `pay_input_ranges` (single job with `pay_transparency=true`) | `None` otherwise; amounts are in cents |
| `posted_date` | `first_published` (detail only) | `posted_date_source="platform"`; **never** `updated_at` |
| `description_snippet` | `content` | HTML-entity-decode first, then strip tags |
| `company_website` | none → `None` | |

Limitations: list responses carry no posting date or company name, so a complete record needs one
extra request per job (or accepting `posted_date=None`, which the Phase 1 date filter keeps and
flags for review). Pagination is undocumented, so large boards need live validation. Dates mix `Z`
and numeric offsets.

Fixtures (all synthetic):
[list_complete](../tests/fixtures/greenhouse/list_complete.json),
[list_missing_optional_fields](../tests/fixtures/greenhouse/list_missing_optional_fields.json),
[list_multiple_locations](../tests/fixtures/greenhouse/list_multiple_locations.json),
[list_remote](../tests/fixtures/greenhouse/list_remote.json),
[list_html_description](../tests/fixtures/greenhouse/list_html_description.json),
[list_empty](../tests/fixtures/greenhouse/list_empty.json) (0 jobs),
[list_invalid_incomplete](../tests/fixtures/greenhouse/list_invalid_incomplete.json) (2 jobs, no title / no URL),
[detail_complete](../tests/fixtures/greenhouse/detail_complete.json),
[detail_missing_posted_date](../tests/fixtures/greenhouse/detail_missing_posted_date.json).
Source: <https://docs.greenhouse.io/job-board.html>.

### Lever

| JobRecord | Source | Notes |
| --- | --- | --- |
| `title` | `text` | |
| `company_name` | seed/config (site slug) | not in the response |
| `apply_url` | `applyUrl`, else `hostedUrl` | **ASSUMPTION:** the fallback is acceptable; both are real URLs from the response |
| `external_id` | `id` | UUID |
| `location` | `categories.location`; extra places from `categories.allLocations` joined with `"; "` | |
| `work_type` | `workplaceType`: `remote`→`remote`, `hybrid`→`hybrid`, `on-site`→`onsite`, `unspecified`→`None` | |
| `employment_type` | `categories.commitment` | free text |
| `salary_text` | `salaryDescriptionPlain`, else formatted `salaryRange` (`currency`, `interval`, `min`, `max`) | optional; the `interval` vocabulary is **ASSUMPTION** (`per-year-salary` used in fixtures) |
| `posted_date` | `createdAt` | **ASSUMPTION:** epoch milliseconds; convert the instant to a UTC date (a Phase 3 decision); `posted_date_source="platform"`; may precede the real publication date |
| `description_snippet` | `descriptionPlain`, else stripped `description` | |

Limitations: pagination is `skip`/`limit` only; the stop condition (empty or short page) and the
default page size are unconfirmed. `createdAt` is not in the official page as read; the
epoch-milliseconds claim comes from third-party sources.

Fixtures (all synthetic):
[postings_complete](../tests/fixtures/lever/postings_complete.json),
[postings_missing_optional_fields](../tests/fixtures/lever/postings_missing_optional_fields.json),
[postings_multiple_locations](../tests/fixtures/lever/postings_multiple_locations.json),
[postings_remote](../tests/fixtures/lever/postings_remote.json),
[postings_html_description](../tests/fixtures/lever/postings_html_description.json),
[postings_missing_posted_date](../tests/fixtures/lever/postings_missing_posted_date.json),
[postings_empty](../tests/fixtures/lever/postings_empty.json) (0),
[postings_invalid_incomplete](../tests/fixtures/lever/postings_invalid_incomplete.json) (2).
Source: <https://github.com/lever/postings-api>.

### Ashby

| JobRecord | Source | Notes |
| --- | --- | --- |
| `title` | `title` | |
| `company_name` | seed/config (board name) | not in the response |
| `apply_url` | `applyUrl`, else `jobUrl` | same fallback assumption as Lever |
| `external_id` | `id` | **ASSUMPTION:** present and stable; not in the official field table |
| `location` | `location`; `secondaryLocations[].location` appended with `"; "` | |
| `work_type` | `workplaceType`: `Remote`→`remote`, `Hybrid`→`hybrid`, `OnSite`→`onsite` | do **not** use `isRemote` alone: it is true for Hybrid too |
| `employment_type` | `employmentType` | stored as the raw enum value |
| `salary_text` | `compensation` summary (only with `includeCompensation=true`) | docs are inconsistent on where it sits; default `None` |
| `posted_date` | `publishedAt` | ISO 8601 with offset; `posted_date_source="platform"`; documented as the *last* publication, so a re-publish can move it |
| `description_snippet` | `descriptionPlain`, else stripped `descriptionHtml` | |

Rule: **skip jobs with `isListed: false`** (reachable only by direct link). Missing fields are
omitted from the response rather than null. A closed job simply disappears from later responses.

Fixtures (all synthetic):
[board_complete](../tests/fixtures/ashby/board_complete.json),
[board_missing_optional_fields](../tests/fixtures/ashby/board_missing_optional_fields.json),
[board_multiple_locations](../tests/fixtures/ashby/board_multiple_locations.json) (Hybrid, `isRemote: true`),
[board_remote](../tests/fixtures/ashby/board_remote.json),
[board_html_description](../tests/fixtures/ashby/board_html_description.json),
[board_missing_published_at](../tests/fixtures/ashby/board_missing_published_at.json),
[board_unlisted_job](../tests/fixtures/ashby/board_unlisted_job.json),
[board_empty](../tests/fixtures/ashby/board_empty.json) (0),
[board_invalid_incomplete](../tests/fixtures/ashby/board_invalid_incomplete.json) (2).
Source: <https://developers.ashbyhq.com/docs/public-job-posting-api>.

### JSON-LD `JobPosting`

| JobRecord | Source | Notes |
| --- | --- | --- |
| `title` | `title` | |
| `company_name` | `hiringOrganization.name` | Google says `"confidential"` may be used for anonymous hiring; treat as not a real name (review) |
| `company_website` | `hiringOrganization.sameAs` / `url` | optional |
| `apply_url` | `url`, else the page URL | **ASSUMPTION:** `JobPosting` has no apply-URL property; `directApply` only hints at the experience |
| `source_url` | the page URL | |
| `external_id` | `identifier.value` (or text) | optional |
| `location` | `jobLocation` (one or several) → `addressLocality, addressRegion, addressCountry`, joined with `"; "` | `addressCountry` may be a code or a name |
| `work_type` | `jobLocationType == "TELECOMMUTE"` → `remote`; otherwise `None` | absence does not mean on-site |
| `employment_type` | `employmentType` (string or array, joined with `", "`) | |
| `salary_text` | `baseSalary` (`MonetaryAmount`/`QuantitativeValue`), formatted | optional |
| `posted_date` | `datePosted` | `jsonld`; date as written, offset not converted |
| `description_snippet` | `description` | HTML, plain, or HTML-escaped (see fixture) |

Structural variants the parser must handle (all in the fixtures): one block; several blocks;
`@graph`; a JSON array holding several objects; other schema.org types mixed in; missing
`datePosted`; malformed JSON. `validThrough` has no `JobRecord` field and is not mapped (a later
closure phase may want it, which would need a schema decision).

**Observed `extruct` 0.18.0 behaviour (run on these fixtures, Phase 3 should account for it):**

- `@graph` is returned as **one item with no `@type`**; the adapter must flatten it.
- A block with a trailing comma is **silently recovered** (it parses via `jstyleson`), so
  `extruct` can report a posting that strict JSON would reject.
- A truncated block makes `extruct.extract()` **raise `JSONDecodeError` for the whole page**
  instead of skipping that block. Parse blocks one by one (as the fixture tests do) or catch
  and fall back.
- Microdata is parsed separately (`syntaxes=["microdata"]`); a page may carry both syntaxes.

Fixtures (all synthetic):
[single_job_posting](../tests/fixtures/jsonld/single_job_posting.html),
[multiple_job_postings](../tests/fixtures/jsonld/multiple_job_postings.html) (2),
[graph_representation](../tests/fixtures/jsonld/graph_representation.html),
[missing_date_posted](../tests/fixtures/jsonld/missing_date_posted.html),
[invalid_structured_data](../tests/fixtures/jsonld/invalid_structured_data.html) (3 blocks: 2 invalid JSON, 1 posting without title),
[mixed_schema_objects](../tests/fixtures/jsonld/mixed_schema_objects.html),
[remote_job_posting](../tests/fixtures/jsonld/remote_job_posting.html),
[multiple_locations_html_description](../tests/fixtures/jsonld/multiple_locations_html_description.html).
Sources: <https://developers.google.com/search/docs/appearance/structured-data/job-posting>,
<https://schema.org/JobPosting>.

### Generic HTML

There is no standard to map from, so these fixtures only capture structural variation; they do
not define a mapping. Relative links must be resolved against the page URL; relative text dates
("Posted 3 days ago") go through `parse_posted_date` with `posted_date_source="page_text"`.
`company_name` normally comes from config rather than the page.

Fixtures (all synthetic):
[listing_unordered_list](../tests/fixtures/generic/listing_unordered_list.html) (3 jobs),
[listing_cards](../tests/fixtures/generic/listing_cards.html) (2),
[listing_empty](../tests/fixtures/generic/listing_empty.html) (0),
[listing_js_rendered_shell](../tests/fixtures/generic/listing_js_rendered_shell.html) (0 in the HTML; needs JavaScript),
[detail_microdata](../tests/fixtures/generic/detail_microdata.html) (1),
[detail_plain_html](../tests/fixtures/generic/detail_plain_html.html) (1).

## Fixture inventory

40 fixtures, **0 captured, 40 synthetic**: Greenhouse 9, Lever 8, Ashby 9, JSON-LD 8, generic 6.

## Unmapped source data

No `JobRecord` field exists for: Greenhouse `requisition_id`, `departments`, `offices`,
`language`, `application_deadline`; Lever `department`/`team`, structured `salaryRange`; Ashby
`department`/`team`, structured compensation; JSON-LD `validThrough`,
`applicantLocationRequirements`. These are deliberately dropped until a schema change is approved.

## Assumptions requiring live validation

1. Lever `createdAt` unit (epoch ms) and meaning (creation vs publication).
2. Ashby `id` presence/stability, and where `compensation` appears with `includeCompensation=true`.
3. Pagination behaviour and page size for Greenhouse (none documented), Lever and Ashby on large
   boards.
4. Whether the API hosts' `robots.txt` and terms allow automated reads (the project requires
   robots.txt compliance); GET rate limits are undocumented for all three platforms.
5. `source_url` = requested URL, and the `applyUrl`→`hostedUrl`/`jobUrl` fallback.
6. Lever `salaryRange.interval` vocabulary.
7. Whether Greenhouse boards expose workplace type or employment type through custom `metadata`.
8. Greenhouse single-job requests per job are acceptable at the expected volume (needed for
   `first_published` and `company_name`).
