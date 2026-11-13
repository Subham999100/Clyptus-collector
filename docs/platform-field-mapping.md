# Platform field mapping

Research and fixture notes for the interfaces the collector reads, and how their fields map onto
the existing `JobRecord` ([collector/models.py](../collector/models.py), unchanged).

**Status:** the **Greenhouse** (Phase 3 step 1), **Lever** (step 2), **Ashby** (step 3) and
**JSON-LD** (step 4) adapters are implemented
([greenhouse.py](../collector/adapters/greenhouse.py), [lever.py](../collector/adapters/lever.py),
[ashby.py](../collector/adapters/ashby.py), [json_ld.py](../collector/adapters/json_ld.py),
sharing [common.py](../collector/adapters/common.py)); their sections below are the actual
behaviour. The generic section is still a *proposal* from Phase 2 research. No spider exists yet.

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
| Lever Postings API | <https://github.com/lever/postings-api> | official README, read (re-read in Phase 3 step 2); **`createdAt` is not documented there at all**, and the response wrapper is not shown |
| Ashby Job Postings API | <https://developers.ashbyhq.com/docs/public-job-posting-api> | official, read (re-read in Phase 3 step 3); **`id` is not in the official field table**, compensation placement inconsistent; auth, pagination, rate limits and errors are not documented |
| JSON-LD `JobPosting` | <https://developers.google.com/search/docs/appearance/structured-data/job-posting> and <https://schema.org/JobPosting> | official, read (re-read in Phase 3 step 4); `datePosted` is documented as "the original date the employer posted the job" |
| Lever `createdAt` as epoch ms | third-party scraper/wrapper descriptions (search results) | **unofficial, unverified; not used by the adapter** |

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
| Posting date | `first_published` **detail only** (ISO 8601, `Z`); `updated_at` (offset, e.g. `-05:00`) is *last modified* | none verified: `createdAt` is undocumented, **not mapped** (Lever adapter leaves the date unknown) | `publishedAt` (ISO 8601 with offset; "last publication") | `datePosted` (`Date` or `DateTime`, ISO 8601, offset optional) |
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
  whitespace, truncate. `JobRecord` has no limit; the Greenhouse and Lever adapters use 500
  characters.
- **Identity:** `external_id` is only set when the platform guarantees stability; Phase 1 scopes
  it by `platform + company_name`. Known limitation (`Infosys` vs `Infosys Limited`). The
  Greenhouse, Lever and Ashby adapters additionally scope the id by board token / site slug /
  board name (see their sections); JSON-LD stores `identifier` unscoped (it is already the hiring
  organization's own job id).

### Greenhouse

Implemented in [collector/adapters/greenhouse.py](../collector/adapters/greenhouse.py). Tests:
[tests/test_greenhouse_adapter.py](../tests/test_greenhouse_adapter.py).

**Interface** (pure: no network, files, database, logging, environment or clock; input not mutated):

```python
extract_greenhouse_jobs(
    payload,                    # decoded JSON: listing {"jobs": [...]} or one job object
    *, company_name,            # trusted configuration
    board_token,                # [A-Za-z0-9][A-Za-z0-9._-]*
    source_url,                 # URL that was requested
    collected_at,               # timezone-aware, injected
    company_website=None,
) -> GreenhouseExtraction       # records, failures, warnings, response_type, job_count
```

Invalid *configuration* (blank company, malformed token, non-public URL, naive `collected_at`)
raises `ValueError`. Problems in the *response* never raise: fatal ones are returned as
failures, recoverable ones as warnings.

**Response types:** *listing* (`jobs` is a list; each element is one job) and *detail* (a bare job
object with `id`, `title` or `absolute_url`). Anything else (not an object, `jobs` not a list, an
error body) is one `invalid_response` failure.

| JobRecord | Source | Behaviour |
| --- | --- | --- |
| `title` | `title` | whitespace collapsed; missing/blank → failure |
| `company_name` | **configuration only** | the detail response's own `company_name` is ignored; never inferred from text |
| `apply_url` | `absolute_url` | Greenhouse has no separate apply-URL field; the board hosts the form behind this link. Never built from token + id |
| `source_url` | argument | validated and normalized |
| `external_id` | `"<board_token>:<id>"` | `id` (integer, canonicalized) scoped by board; absent `id` → `None` (identity falls back to the normalized apply URL) |
| `location` | `location.name` | whitespace collapsed, kept unsplit in source order (`"A; B"`); blank → `None`; `offices` unused |
| `work_type` | location text | `remote` only if every `;` part is an explicit remote marker (`Remote`, `Remote - X`, `Remote (X)`, `Remote, X`) and no hybrid/on-site word appears; else `None` |
| `employment_type`, `salary_text` | none | always `None` (not in the public listing; `pay_input_ranges` is not mapped yet) |
| `posted_date` | `first_published` | `posted_date_source="platform"`; absent/null/blank → `None` + `"unknown"`; malformed or wrong-type → same, plus a warning (job kept); **never** `updated_at` or `collected_at` |
| `description_snippet` | `content` | entity-decoded HTML → text → at most 500 characters (see below) |
| `company_website` | argument | validated and normalized, else `None` |
| `platform`, `collected_at` | constant, argument | |

**Validation and failures.** Each job is extracted independently; one bad job never stops the rest
and nothing is dropped silently. `ExtractionFailure` carries `kind`, `index` (position in `jobs`),
`job_id` and every `FieldError` found (source field names). Messages name types and fields, never
source values or HTML. Kinds: `invalid_response`, `missing_field` (title, `absolute_url`),
`invalid_url`, `invalid_type` (a required or consumed field other than the date with the wrong
JSON type, including an unusable `id`). `invalid_date` is a warning kind only (see Dates). After the
adapter's own checks, `validate_job` runs as the final gate;
anything it still rejects (e.g. a URL over the model's length limit) is reported with its own
field/code/message. URLs must be absolute public http(s): no credentials, `localhost`,
single-label hosts or non-global IPs; they are normalized with `normalize_url` (tracking
parameters dropped, `gh_jid` kept). Unknown fields are ignored.

**Dates.** Only ISO 8601 `first_published` (date, or date-time with optional `Z`/offset) is
accepted. The date is the one *written in the source*; an offset is not converted
(`2026-10-01T23:30:00-05:00` → 1 Oct), consistent with `parse_posted_date`. The date is optional,
so a bad one never rejects an otherwise valid job:

| `first_published` | Result |
| --- | --- |
| valid ISO 8601 | `posted_date` set, `"platform"`, no warning |
| absent, `null`, blank | unknown date, no warning |
| a string that is not valid ISO 8601 (free text such as "3 days ago" included) | job kept, unknown date, `invalid_date` warning |
| not a string (number, bool, list, object) | job kept, unknown date, `invalid_type` warning |

Nothing is substituted: not `updated_at` (last modified), not `collected_at`, and no date is
parsed from free text. The Phase 1 date window classifies these jobs as `UNKNOWN_DATE` (kept, needs
review).

**Warnings.** `GreenhouseExtraction.warnings` holds one `ExtractionWarning` per recoverable
problem, in source order: `kind` (`invalid_date` / `invalid_type`), `index` (position in `jobs`,
`None` for a detail response), `job_id` (when usable), `field` (`first_published`) and a message
that names the field and, for a wrong type, the JSON type, never the source value. The posting date is
the only recoverable field today; every other problem is still a fatal failure. If a job also fails
for another reason, its date warning is still reported next to the failure.

**Identity.** `job_key` is unchanged (`platform + company_name + external_id`). Putting the board
token inside `external_id` makes it board-scoped without touching the algorithm: the same
Greenhouse id on two boards gives two keys; the same job re-extracted (even from a changed URL)
gives the same key. `company_name` still scopes the key, so renaming a company in configuration
changes all its keys (Phase 1 limitation).

**Descriptions.** `content` is entity-encoded HTML (`&lt;p&gt;`): decoded once (content that
already has literal tags is not decoded again), parsed with `selectolax` (nothing is executed or
fetched), `script`/`style`/`iframe`/`svg`/`template`/comments are dropped with their content,
block elements separate words, control characters are removed, text is NFC-normalized and
whitespace collapsed to single spaces. The snippet is at most 500 Unicode characters, cut at a word
boundary when one exists (a single longer word is cut hard); no ellipsis is added. Missing or empty
content → `None` (list responses only carry it with `?content=true`).

**Known limitations.** List responses have no posting date, so records from them have
`posted_date=None` until a detail response is extracted (the Phase 1 date filter keeps and flags
them). No pagination, no `meta.total` cross-check, no de-duplication inside the adapter (use the
Phase 1 `Deduper`). `departments`, `offices`, `requisition_id`, `language`, `metadata` and
`application_deadline` are not mapped. The `work_type` rule is a narrow text heuristic (approval
pending); Greenhouse exposes no field for it.

**Assumptions requiring permitted live validation:** `absolute_url` always leads to the
application; `id` is stable and unique within a board; `first_published` is always ISO 8601;
`content` is entity-encoded (or contains literal tags) and not otherwise double-encoded; board
tokens match the accepted pattern and are case-consistent in configuration.

Fixtures (all synthetic, 9): every one is exercised by the adapter tests, and every job in them is
either extracted or reported:
[list_complete](../tests/fixtures/greenhouse/list_complete.json),
[list_missing_optional_fields](../tests/fixtures/greenhouse/list_missing_optional_fields.json),
[list_multiple_locations](../tests/fixtures/greenhouse/list_multiple_locations.json),
[list_remote](../tests/fixtures/greenhouse/list_remote.json),
[list_html_description](../tests/fixtures/greenhouse/list_html_description.json),
[list_empty](../tests/fixtures/greenhouse/list_empty.json) (0 jobs),
[list_invalid_incomplete](../tests/fixtures/greenhouse/list_invalid_incomplete.json) (2 failures: no title / no URL),
[detail_complete](../tests/fixtures/greenhouse/detail_complete.json),
[detail_missing_posted_date](../tests/fixtures/greenhouse/detail_missing_posted_date.json).
Source: <https://docs.greenhouse.io/job-board.html>.

### Lever

Implemented in [collector/adapters/lever.py](../collector/adapters/lever.py) (Phase 3 step 2), on
the result types and text/URL helpers shared with Greenhouse in
[collector/adapters/common.py](../collector/adapters/common.py). Tests:
[tests/test_lever_adapter.py](../tests/test_lever_adapter.py).

**Interface** (pure: no network, files, database, logging, environment or clock; input not mutated):

```python
extract_lever_jobs(
    payload,                    # decoded JSON: the array returned by GET /v0/postings/{site}
    *, company_name,            # trusted configuration
    site_slug,                  # [A-Za-z0-9][A-Za-z0-9._-]*
    source_url,                 # URL that was requested
    collected_at,               # timezone-aware, injected
    company_website=None,
) -> LeverExtraction            # same type as GreenhouseExtraction: records, failures,
                                # warnings, response_type ("listing"), job_count
```

Invalid *configuration* raises `ValueError`. Problems in the *response* never raise: fatal ones are
failures, recoverable ones are warnings. **Response type:** only the JSON array (*listing*). The
README does not show a response wrapper or a single-posting shape; anything else (object, error
body, string, `null`) is one `invalid_response` failure.

| JobRecord | Source | Behaviour |
| --- | --- | --- |
| `title` | `text` | whitespace collapsed; missing/blank → failure |
| `company_name` | **configuration only** | Lever responses carry none; never inferred from text |
| `apply_url` | `applyUrl`, else `hostedUrl` | `applyUrl` preferred. `hostedUrl` (the posting page, which links to the form) is used when `applyUrl` is absent, or present but unusable (then with a warning). Both unusable → failure. No URL is ever built |
| `external_id` | `"<site_slug>:<id>"` | `id` UUIDs are canonicalized (lower case, hyphenated); other strings kept; absent `id` → `None` (identity falls back to the normalized apply URL); wrong type / blank → failure |
| `location` | `categories.location`, then the other `categories.allLocations` | primary first, then the rest in source order, case-insensitive duplicates removed, joined with `"; "`; only `allLocations` present → it alone; blank → `None` |
| `work_type` | top-level `workplaceType` | `remote`→`remote`, `hybrid`→`hybrid`, `on-site`→`onsite`; `unspecified`, unknown values and absence → `None`. Never inferred from location or description |
| `employment_type` | `categories.commitment` | verbatim (whitespace collapsed), only when it names **one** commitment: text with `, ; / \| &`, `or`/`and`, or over 100 characters is ambiguous → `None`. Not mapped to a vocabulary |
| `salary_text` | none | always `None` (`salaryRange`/`salaryDescription*` exist but are not mapped) |
| `posted_date` | none | always `None` / `"unknown"`: `createdAt` is deliberately not mapped (see Dates) |
| `description_snippet` | `descriptionPlain`, else `description` | see Descriptions |
| `company_website`, `platform`, `collected_at`, `source_url` | arguments / constant | as for Greenhouse |

`workplaceType` is read at the **top level**, as the README and the fixtures place it; a value
inside `categories` is not read because that placement is undocumented.

**Validation and failures.** As for Greenhouse: each posting is extracted independently, a failure
carries `kind`, `index`, `job_id` and every `FieldError` (source field names), messages never
contain source values or HTML, and `validate_job` is the final gate. Fatal: missing/blank `text`,
both URLs missing or unusable, unusable `id`, and a wrong JSON type in `categories`,
`categories.location`, `categories.allLocations`, `categories.commitment`, `workplaceType`,
`descriptionPlain` or `description`. URLs must be absolute public http(s): no credentials,
`localhost`, single-label hosts or non-global IPs; they are normalized with `normalize_url`.
Unknown fields are ignored.

**Dates: `createdAt` is deliberately not mapped.** Every Lever record has `posted_date=None` and
`posted_date_source="unknown"`.

- Lever's public README documents **no publication-date field**. `createdAt` is not in it at all
  (re-read in Phase 3 step 2), so its unit (epoch milliseconds is a third-party claim) and its
  meaning (creation time, which can precede the day a posting went live) are both unverified.
- Using it would put an unverified date into the Phase 1 date window, which could wrongly mark a
  recently published job `OLD_DATE` and drop it from the eligible set. Unknown dates are instead
  kept and flagged for review (`UNKNOWN_DATE`), so nothing is lost.
- The adapter does not read, parse or validate `createdAt`: it is out of the extraction path, not
  parsed and discarded. Whatever it holds (a valid value, garbage, a wrong type, or nothing) has no
  effect on the record and produces **no warning**. `collected_at` is never substituted, no other
  timestamp field replaces it, and no date is parsed from text.
- **Future work, after permitted live validation:** only a field whose publication semantics are
  confirmed (against Lever documentation or live responses fetched within robots and terms) may
  populate `posted_date`. Until then the fixtures' `createdAt` values (2026-10-05 as epoch
  milliseconds, per the Phase 2 notes) are present in the data but unused.

**Warnings.** `LeverExtraction.warnings` holds `ExtractionWarning` objects (`kind`, `index`,
`job_id`, `field`, safe `message`) in source order. There is one recoverable problem: an `applyUrl`
that is present but unusable while `hostedUrl` saved the job (`invalid_url` / `invalid_type`, field
`applyUrl`). A posting that fails for another reason still reports its warning next to the failure.

**Descriptions.** `descriptionPlain` is used when it has content; it is text, so it is **not**
parsed as HTML (a literal `<b>` stays). Otherwise `description` (HTML) is converted with
`selectolax` exactly as for Greenhouse (nothing executed or fetched; `script`/`style`/`iframe`/
`svg`/`template`/comments dropped with their content; block elements separate words; entities
decoded once). In both cases control characters are removed, text is NFC-normalized and whitespace
collapsed, and the snippet is at most 500 Unicode characters, cut at a word boundary when one
exists (a single longer word is cut hard; no ellipsis). `descriptionBody*`, `opening*`, `lists` and
`additional*` are not used: `descriptionPlain` already combines opening and body per the README.

**Known limitations.** **No posting date for any Lever job** (see Dates), so every Lever record is
`UNKNOWN_DATE` in the Phase 1 window and needs review; the date filter cannot exclude old Lever
postings. No pagination (`skip`/`limit` handling belongs to the later fetch layer; the stop
condition is undocumented), no de-duplication inside the adapter (use the Phase 1 `Deduper`), no
EU/host handling, and no salary. `department`, `team`, `country`, `lists` and `additional` are not
mapped. The commitment rule is a conservative text check, not a taxonomy.

**Assumptions requiring permitted live validation:** whether any Lever field is a verified
publication date (`createdAt` is undocumented and unused); `id` is a stable UUID within a site;
`hostedUrl` leads to the application; `workplaceType` is at the top level with the documented
values; the response is a bare array; `allLocations` lists every location including the primary
one.

Fixtures (all synthetic, 8): every one is exercised by the adapter tests, and every posting in
them is either extracted or reported:
[postings_complete](../tests/fixtures/lever/postings_complete.json),
[postings_missing_optional_fields](../tests/fixtures/lever/postings_missing_optional_fields.json),
[postings_multiple_locations](../tests/fixtures/lever/postings_multiple_locations.json),
[postings_remote](../tests/fixtures/lever/postings_remote.json),
[postings_html_description](../tests/fixtures/lever/postings_html_description.json),
[postings_missing_posted_date](../tests/fixtures/lever/postings_missing_posted_date.json),
[postings_empty](../tests/fixtures/lever/postings_empty.json) (0),
[postings_invalid_incomplete](../tests/fixtures/lever/postings_invalid_incomplete.json) (2 failures: no title / no URL).
Source: <https://github.com/lever/postings-api>.

### Ashby

Implemented in [collector/adapters/ashby.py](../collector/adapters/ashby.py) (Phase 3 step 3), on
the result types, URL checks and ISO-date helper shared in
[common.py](../collector/adapters/common.py). Tests:
[tests/test_ashby_adapter.py](../tests/test_ashby_adapter.py).

**Interface** (pure: no network, files, database, logging, environment or clock; input not mutated):

```python
extract_ashby_jobs(
    payload,                    # decoded JSON: {"apiVersion": "1", "jobs": [...]}
    *, company_name,            # trusted configuration
    board_name,                 # [A-Za-z0-9][A-Za-z0-9._-]*
    source_url,                 # URL that was requested
    collected_at,               # timezone-aware, injected
    company_website=None,
) -> AshbyExtraction            # records, failures, warnings, skipped, response_type, job_count
```

Invalid *configuration* raises `ValueError`. Problems in the *response* never raise: fatal ones
are failures, recoverable ones are warnings, and unlisted jobs are skips. **Response type:** only
the `{"jobs": [...]}` object (*listing*); the documentation shows no single-job shape, so anything
else (array, error body, no `jobs` list) is one `invalid_response` failure. `apiVersion` is not
checked; unknown fields are ignored.

| JobRecord | Source | Behaviour |
| --- | --- | --- |
| `title` | `title` | whitespace collapsed; missing/blank → failure |
| `company_name` | **configuration only** | Ashby responses carry none; never inferred from text |
| `apply_url` | `applyUrl`, else `jobUrl` | `applyUrl` (the hosted application page) preferred. `jobUrl` (the posting page) is used when `applyUrl` is absent, or present but unusable (then with a warning). Both unusable → failure. No URL is ever built |
| `external_id` | `"<board_name>:<id>"` | **ASSUMPTION:** `id` is absent from the official field table; observed as a UUID string. UUIDs are canonicalized (lower case, hyphenated); other strings kept; absent `id` → `None` (identity falls back to the normalized apply URL); wrong type / blank → failure |
| `location` | `location`, then `secondaryLocations[].location` | primary first, then the rest in source order, case-insensitive duplicates removed, joined with `"; "`; the structured `address` objects are not read; blank → `None` |
| `work_type` | `workplaceType` | `Remote`→`remote`, `Hybrid`→`hybrid`, `OnSite`→`onsite` (case-insensitive); unknown values and absence → `None`. **`isRemote` is deliberately not read** (documented as true for Hybrid too) and the type is never inferred from location or description |
| `employment_type` | `employmentType` | verbatim (whitespace collapsed); the docs list `FullTime`, `PartTime`, `Intern`, `Contract`, `Temporary`, but the value is stored as sent, not mapped onto a vocabulary |
| `salary_text` | none | always `None` (`compensation` only appears with `includeCompensation=true` and its placement is documented inconsistently; not mapped) |
| `posted_date` | `publishedAt` | `posted_date_source="platform"`; see Dates |
| `description_snippet` | `descriptionPlain`, else `descriptionHtml` | as for Lever: plain text is never parsed as HTML; HTML is converted with `selectolax` (nothing executed or fetched), 500-character snippet |
| `company_website`, `platform`, `collected_at`, `source_url` | arguments / constant | as for Greenhouse |

**Unlisted jobs are skipped, not collected.** `isListed: false` is documented as "should only be
reachable by direct link", so the job must not be collected from the board listing. It becomes a
`SkippedJob` (`index`, `job_id`, safe `reason`) in `AshbyExtraction.skipped` — a rule, not a data
problem — and is never extracted, even if it is also malformed. For a recognized response
`job_count == len(records) + len(failures) + len(skipped)`, so nothing is dropped silently.
`isListed` absent or `null` means the job is collected (the response came from the public board
endpoint); a non-boolean value is an `invalid_type` failure.

**Validation and failures.** As for Greenhouse and Lever: each job is extracted independently, a
failure carries `kind`, `index`, `job_id` and every `FieldError` (source field names), messages
never contain source values or HTML, and `validate_job` is the final gate. Fatal: missing/blank
`title`, both URLs missing or unusable, unusable `id`, non-boolean `isListed`, and a wrong JSON
type in `location`, `secondaryLocations(.location)`, `workplaceType`, `employmentType`,
`descriptionPlain` or `descriptionHtml`. URLs must be absolute public http(s), normalized with
`normalize_url`.

**Dates.** Only ISO 8601 `publishedAt` (date, or date-time with optional `Z`/offset) is accepted,
via the shared `read_iso_date`: the date is the one *written in the source*, offsets are not
converted. `publishedAt` is documented as the "ISO DateTime when the job was **last** published":
a verified publication timestamp, with the caveat that re-publishing a job moves it forward (it
can only make an old job look newer — such a job stays in the eligible set and is never wrongly
dropped as old).

| `publishedAt` | Result |
| --- | --- |
| valid ISO 8601 | `posted_date` set, `"platform"`, no warning |
| absent, `null`, blank | unknown date, no warning |
| a string that is not valid ISO 8601 | job kept, unknown date, `invalid_date` warning |
| not a string | job kept, unknown date, `invalid_type` warning |

Nothing is substituted: not `collected_at`, and no date is parsed from free text. Jobs without a
usable `publishedAt` are `UNKNOWN_DATE` in the Phase 1 window (kept, flagged for review).

**Warnings.** As for Lever: an `applyUrl` present but unusable while `jobUrl` saved the job, plus
the date warnings above. A job that fails for another reason still reports its warnings.

**Known limitations.** No pagination (none documented), no de-duplication inside the adapter (use
the Phase 1 `Deduper`), no salary/compensation, `department`, `team`, `isRemote` and `address`
are not mapped. Auth, rate limits and error shapes are undocumented.

**Assumptions requiring permitted live validation:** `id` exists, is a stable UUID and is unique
within a board (it is **not** in the official field table); `applyUrl` leads to the application
and `jobUrl` to the posting page; `publishedAt` is always ISO 8601; the response shape of error
cases.

Fixtures (all synthetic, 9): every one is exercised by the adapter tests, and every job in them is
extracted, reported or skipped:
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

Implemented in [collector/adapters/json_ld.py](../collector/adapters/json_ld.py) (Phase 3 step 4),
on the result types, URL checks and ISO-date helper shared in
[common.py](../collector/adapters/common.py). Tests:
[tests/test_json_ld_adapter.py](../tests/test_json_ld_adapter.py).

**Interface** (pure: no network, files, database, logging, environment or clock; nothing in the
page is executed — no browser, no crawler, no JavaScript):

```python
extract_json_ld_jobs(
    html,                       # the already-fetched page markup (never fetched here)
    *, page_url,                # public URL the page came from; source_url + apply fallback
    collected_at,               # timezone-aware, injected
    company_name=None,          # optional trusted configuration; wins over the page
    company_website=None,       # optional trusted configuration; wins over the page
) -> JsonLdExtraction           # records, failures, warnings, response_type ("listing"), job_count
```

Invalid *configuration* raises `ValueError`. Problems in the *page* never raise. A non-string page
is one `invalid_response` failure; a page without JSON-LD blocks extracts nothing (0 jobs).

**Finding postings.** Every `<script type="application/ld+json">` block (type match is
case-insensitive) is read as text and parsed **one by one with strict `json`** — `extruct` is
deliberately not used for extraction (its recovery/failure behaviour, below, is unsuitable).
Within a decoded block, `JobPosting` objects are taken from a lone object, a top-level array and
`@graph` lists, nested at most 6 levels (iterative walk; other properties are never descended
into, so hostile nesting is bounded). `@type` may be a string or a list; the bare name, a
schema.org IRI (`https://schema.org/JobPosting`) or a prefixed `schema:JobPosting` all count,
case-insensitively. Objects of other types are ignored.

**Extraction units** are counted in document order: each malformed (non-blank) block is one
`invalid_response` failure (field `"(script)"`), and each identified `JobPosting` is one record
or failure, so `job_count == len(records) + len(failures)`; a blank block carries no data and is
ignored. One malformed block never hides the postings in the other blocks.

| JobRecord | Source | Behaviour |
| --- | --- | --- |
| `title` | `title` | whitespace collapsed; missing/blank → failure |
| `company_name` | configured `company_name`, else `hiringOrganization.name` (or a plain-string organization) | Google's `"confidential"` (anonymous hiring) is not a usable name → failure unless a name is configured; missing/blank/wrong type likewise |
| `company_website` | configured `company_website`, else `hiringOrganization.sameAs`, else `.url` | optional: a present but unusable value is only a warning |
| `apply_url` | `url`, else the page URL | `JobPosting` has **no apply-URL property** (`directApply` is only a boolean hint); `url` is the posting's own page. Present but unusable → warning, page URL used. No URL is ever invented |
| `source_url` | `page_url` argument | validated and normalized |
| `external_id` | `identifier.value` (a `PropertyValue`), or `identifier` as a plain string/number | whitespace collapsed, stored unscoped (Google: "the hiring organization's unique job ID"; `job_key` already scopes by platform + company); absent → `None` (identity falls back to the normalized apply URL); wrong type / blank → failure |
| `location` | `jobLocation` (one `Place` or a list) | per place: `address` parts `addressLocality, addressRegion, addressCountry` joined with `", "` (`addressCountry` may be a `Country` object with a `name`), or a plain-text `address`, or the place `name`; places joined with `"; "`, case-insensitive duplicates removed. `applicantLocationRequirements` (eligibility area, not a workplace) is never read |
| `work_type` | `jobLocationType` | `TELECOMMUTE` (string or in a list, case-insensitive) → `remote`; anything else → `None`. The vocabulary has no hybrid/on-site value and absence does not mean on-site; nothing is inferred from text |
| `employment_type` | `employmentType` (string or array) | verbatim (whitespace collapsed), array joined with `", "` in source order, case-insensitive duplicates removed; not mapped onto a vocabulary |
| `salary_text` | none | always `None` (`baseSalary` is structured data; formatting it into text is deferred) |
| `posted_date` | `datePosted` | `posted_date_source="jsonld"`; see Dates |
| `description_snippet` | `description` | HTML per Google; content without literal tags is entity-decoded once, then converted exactly as for the other adapters (`selectolax`, nothing executed, 500-character snippet) |
| `platform`, `collected_at` | constant (`jsonld`), argument | |

**Dates.** Only ISO 8601 `datePosted` is accepted, via the shared `read_iso_date` (same table as
Ashby: absent/blank → unknown silently; malformed string → `invalid_date` warning; wrong type →
`invalid_type` warning; the job is always kept). Google documents `datePosted` as "the original
date the employer posted the job". **`validThrough` is an expiry, never a posting date, and is
not read at all**; page modification times and `collected_at` are never substituted, and no date
is parsed from free text. Jobs without a usable `datePosted` are `UNKNOWN_DATE` in the Phase 1
window (kept, flagged for review).

**Validation and failures.** As for the other adapters: each posting is extracted independently,
failures carry `kind`, `index` (extraction-unit position), `job_id` and every `FieldError`,
messages never contain source values or HTML, and `validate_job` is the final gate. Fatal:
missing/blank `title`, no usable company name, unusable `identifier`, and a wrong JSON type in
`hiringOrganization(.name)`, `jobLocation` (and its `address` parts), `jobLocationType`,
`employmentType` or `description`.

**Warnings.** A present but unusable `url` (page URL used), `hiringOrganization.sameAs`/`url`
(website unset or the fallback used), and the date warnings above.

**Identity fallback.** One posting without an `identifier` or usable job-specific `url` uses the
page URL as its identity. If multiple such postings appear on the same page, each is reported as
an `invalid_response` failure with an ambiguous-identity error instead of emitting records that
would share one identity. Postings with distinct usable URLs can still use URL-based identity.

**Known limitations.** `baseSalary`,
`validThrough`, `directApply` and `applicantLocationRequirements` are not mapped. Microdata (the
other structured-data syntax) is not read — that is generic-page territory. The host page itself
may be JavaScript-rendered; this adapter only sees the HTML it is given.

**Assumptions requiring permitted live validation:** `url` leads to the posting/application;
`identifier` is stable within an organization; real-world `@type`/`@graph` shapes beyond the
documented ones.

Historical note from Phase 2 (why strict `json` instead of `extruct` 0.18.0): `extruct` returns
`@graph` as one item with no `@type`, silently recovers blocks with trailing commas, raises
`JSONDecodeError` for the **whole page** on one truncated block, and parses microdata separately.
Parsing blocks one by one with strict `json` keeps failures isolated and explicit. `extruct`
remains a declared dependency for later generic-page work.

Fixtures (all synthetic, 8): every one is exercised by the adapter tests, and every posting or
malformed block in them is extracted or reported:
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
`department`/`team`, `address`, structured compensation; JSON-LD `validThrough`, `baseSalary`,
`directApply`, `applicantLocationRequirements`. These are deliberately dropped until a schema
change is approved.

## Assumptions requiring live validation

1. Lever publication date: `createdAt` is undocumented (unit and meaning unverified) and is
   deliberately **not mapped**; Lever jobs have an unknown posted date until a verified field is
   confirmed.
2. Ashby `id` presence/stability (it is **not** in the official field table; the adapter maps it
   as an assumption), and where `compensation` appears with `includeCompensation=true`.
3. Pagination behaviour and page size for Greenhouse (none documented), Lever and Ashby on large
   boards.
4. Whether the API hosts' `robots.txt` and terms allow automated reads (the project requires
   robots.txt compliance); GET rate limits are undocumented for all three platforms.
5. `source_url` = requested URL, and the `applyUrl`→`hostedUrl`/`jobUrl` fallback.
6. Lever `salaryRange.interval` vocabulary.
7. Whether Greenhouse boards expose workplace type or employment type through custom `metadata`.
8. Greenhouse single-job requests per job are acceptable at the expected volume (needed for
   `first_published`).
9. The adapters' own assumptions (listed per adapter in their sections above), including JSON-LD
   `url`/`identifier` semantics on real pages.
