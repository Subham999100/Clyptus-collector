Crawler
# Clyptus Collector

Standalone tool that will collect publicly available job postings from company career sites,
validate and store them in SQLite, and export them to Excel for the Clyptus Job Portal.

**Status: Phase 6.** `discover` and `sources` turn a company list into reviewed, approved
crawler sources; `run` crawls configured sources into SQLite and `export` writes the stored
jobs to Excel. `report` is still a placeholder. No live pilot has been run yet; see
[docs/phase5-pilot-report.md](docs/phase5-pilot-report.md).

Phase 7 added an offline Keka Hire adapter for already-fetched, authorized API responses. Keka,
Darwinbox and Zoho Recruit stay unsupported for crawling, because their job APIs need
credentials; Darwinbox and Zoho Recruit adapters are blocked on undocumented schemas. See
"Phase 7 providers" in [docs/platform-field-mapping.md](docs/platform-field-mapping.md).

Phase 8 added generic HTML extraction (platform `html`) for career sites that list jobs in
ordinary server-rendered HTML. It is not a universal scraper: each such source needs its own
reviewed, explicitly approved selector configuration, and it is verified only against synthetic
fixtures so far; see "Generic HTML" in the same document.

Phase 9 added `collector daily`, the unattended daily run for cron or a systemd timer: one run
per database at a time, recovery of interrupted runs, a dated workbook
(`jobs_YYYY-MM-DD.xlsx`, business date Asia/Kolkata; partial runs are named as such), a stale-job
review list (jobs are never closed automatically) and documented exit codes. See
[docs/daily-operations.md](docs/daily-operations.md). Verified offline only.

## Usage

```bash
collector run --sources sources.csv --db data/collector.db
collector export --db data/collector.db --output output/jobs.xlsx   # add --overwrite to replace
collector daily --sources approved_sources.csv --db data/collector.db --output-dir exports
```

`sources.csv` has the header `platform,company_name,identifier,company_website`, where
`platform` is `greenhouse`, `lever`, `ashby`, `jsonld` or `html`. `identifier` is the board
token, site slug or board name, or for `jsonld` and `html` a public HTTPS page URL. Only list
sources you are permitted to collect from.

The optional fifth column, `html_config`, is required for `html` sources: the path (relative to
the CSV) of a JSON file with the site's CSS selectors and a `review` block recording who verified
the page and who approved collection (`"authorization": "APPROVED"`). Without it, the source is
refused. The crawler then fetches the listing page, at most `max_pages` next pages (limit 10),
and, only if `detail` selectors are configured, each job's own page, all on the listing page's
host, with the same robots.txt, TLS and address checks as every other source. Pages that need
JavaScript are reported as unsupported, not as empty listings. Known job boards and ATS hosts
(Greenhouse, Lever, Ashby, Keka, Darwinbox, Zoho Recruit, Workday ...) cannot be configured as
`html`. Format and field rules: "Generic HTML" in
[docs/platform-field-mapping.md](docs/platform-field-mapping.md).

The export writes one `Jobs` sheet with open jobs only. Postings older than the date window are
left out. Jobs without a posted date, or with one in the future, are included and labelled in
the "Date Status" column. The database is opened read-only. Exit codes for `export`: 0 success,
2 output exists (without `--overwrite`) or cannot be written, 3 database missing or unusable.

### From company names to approved sources

```bash
collector discover --companies companies.csv --batch-size 10      # repeat for the next batch
collector sources verify "Acme" --by "Reviewer" --evidence "careers page links the board"
collector sources authorize "Acme" --decision approved --by "Owner" --note "written permission"
collector sources export --output approved_sources.csv           # then: collector run --sources ...
collector sources status --companies companies.csv
```

`companies.csv` has the header `company_name,official_website,careers_url` (template:
[collector/companies/seed_companies.csv](collector/companies/seed_companies.csv)); only the
name is required. Invalid rows are reported and skipped, exact repeats are skipped, and similar
or conflicting entries are never merged but left for review.

By default `discover` is offline: it fetches no pages and looks nothing up. It classifies
supplied careers URLs by their documented shapes (Greenhouse, Lever and Ashby boards, a same-site
JSON-LD candidate page, or an unsupported ATS such as Workday or Keka) and reads identifiers only
from those URLs. Ownership is not confirmed offline, so candidates stay `REVIEW_REQUIRED` until
a reviewer confirms them with `sources verify`; a company with no website is `NOT_FOUND`.

`discover --online` (only for companies you are authorized to contact) fetches pages through the
same protected Scrapy crawler as `run`: robots.txt, HTTPS only, exact-host requests,
connection-time public-address checks, verified TLS, the download delay, and blocking on
HTTP 403/429 and challenge pages. Per company it fetches the official homepage and at most one
careers page on the same registered domain. It never fetches job boards: a board is recognized
from a link or redirect on the official pages. A redirect to another host is judged but never
followed, except a same-domain hop such as `example.com` to `www.example.com`, which is
requested separately with all checks. A source becomes `VERIFIED` only when the homepage names
the company and an official page links (or redirects) to one supported board or carries
readable JSON-LD `JobPosting` markup.

For companies listed without a website, `--provider wikidata` (or
`COLLECTOR_DISCOVERY_PROVIDER=wikidata`) looks up Wikidata's "official website" (P856) on the
public query service, which is free and needs no key. An entry counts only when its English label
or alias equals the company name exactly (normalized), so an acronym matches only when Wikidata
lists it as an alias. Entries on different domains go to review. The default provider is
`none`.

Collection authorization is separate: every company is `PENDING` until someone records an
explicit `APPROVED` (only for a `VERIFIED` source, and only for that source) or `RESTRICTED`
decision. `sources export` writes only verified, approved, supported sources, in the format
`collector run --sources` reads (checked with the loader before writing), plus a
`<output>_review.csv` listing every other company with the reason. Nothing is crawled by these
commands. Results live in `data/discovery.json` (`COLLECTOR_DISCOVERY_STATE_PATH`), are saved
atomically while a batch runs, and a rerun resumes with the companies not yet processed; a
verified source is never overwritten by a later inconclusive check. Run one `discover` at a
time per state file.

## Setup

Requires Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check                  # confirms no broken requirements
collector --help
```

Dependencies are declared with bounded ranges in [pyproject.toml](pyproject.toml); there is no
lock file. Phase 0 was verified with Python 3.13.9 (Docker image: Python 3.12) and these
resolved versions:

| Package | Version | Package | Version |
| --- | --- | --- | --- |
| scrapy | 2.20.0 | dateparser | 1.5.0 |
| pydantic | 2.14.3 | tldextract | 5.4.0 |
| pydantic-settings | 2.16.2 | extruct | 0.18.0 |
| typer | 0.29.1 | selectolax | 0.4.13 |
| structlog | 25.5.0 | pytest | 9.2.0 |
| openpyxl | 3.1.5 | pytest-cov | 7.1.0 |
| ruff | 0.17.4 | tzdata | 2026.6 |

To reproduce this exact set later, install with the versions above pinned
(e.g. `pip install -e ".[dev]" scrapy==2.20.0 ...`) or capture your own with
`python -m pip freeze > constraints.txt` and install using `-c constraints.txt`.

## Configuration

Defaults can be overridden with `COLLECTOR_<NAME>` environment variables
(e.g. `COLLECTOR_DATE_WINDOW_DAYS=14`). The business timezone used for the posted-date
window is `COLLECTOR_TIMEZONE` (default `Asia/Kolkata`). See [collector/settings.py](collector/settings.py).

## Database

SQLite, created only by `collector.pipelines.store.init_db(path)` (schema version 2). A version-1
database is migrated in place by `init_db`: it adds `run_job_observations` and keeps all existing
jobs and run counters. Migration is refused while any run is still `RUNNING`; resolve such runs
first. See the module docstring in [collector/pipelines/store.py](collector/pipelines/store.py).

## Development

```bash
python -m pytest -q    # run from the project root; temp files go to ./.pytest_tmp
ruff check .
ruff format --check .
```

## Fixtures

`tests/fixtures/` holds small **synthetic** samples of Greenhouse, Lever, Ashby, Keka, JSON-LD
and generic HTML pages, described by `tests/fixtures/manifest.json`. How their fields map onto
`JobRecord` (and what still needs live validation) is in
[docs/platform-field-mapping.md](docs/platform-field-mapping.md).

## Docker

```bash
docker build -t clyptus-collector:phase0 .
docker run --rm clyptus-collector:phase0   # prints CLI help
```
