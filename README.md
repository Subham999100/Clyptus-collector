# Clyptus Collector

Standalone tool that will collect publicly available job postings from company career sites,
validate and store them in SQLite, and export them to Excel for the Clyptus Job Portal.

**Status: Phase 6.** `discover` and `sources` turn a company list into reviewed, approved
crawler sources; `run` crawls configured sources into SQLite and `export` writes the stored
jobs to Excel. `report` is still a placeholder. No live pilot has been run yet; see
[docs/phase5-pilot-report.md](docs/phase5-pilot-report.md).

## Usage

```bash
collector run --sources sources.csv --db data/collector.db
collector export --db data/collector.db --output output/jobs.xlsx   # add --overwrite to replace
```

`sources.csv` has the header `platform,company_name,identifier,company_website`, where
`platform` is `greenhouse`, `lever`, `ashby` or `jsonld`. `identifier` is the board token, site
slug or board name, or for `jsonld` a public HTTPS page URL. Only list sources you are
permitted to collect from.

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

`discover` is offline: it fetches no pages and has no website search. It classifies supplied
careers URLs by their documented shapes (Greenhouse, Lever and Ashby boards, a same-site
JSON-LD candidate page, or an unsupported ATS such as Workday or Keka) and reads identifiers only
from those URLs. Ownership is not confirmed offline, so candidates stay `REVIEW_REQUIRED` until
a reviewer confirms them with `sources verify`. A company with no supplied website is
`NOT_FOUND`. The discovery logic can confirm ownership from fetched official pages, but no live
page fetcher is wired in yet.

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

`tests/fixtures/` holds small **synthetic** samples of Greenhouse, Lever, Ashby, JSON-LD and
generic HTML pages, described by `tests/fixtures/manifest.json`. How their fields map onto
`JobRecord` (and what still needs live validation) is in
[docs/platform-field-mapping.md](docs/platform-field-mapping.md).

## Docker

```bash
docker build -t clyptus-collector:phase0 .
docker run --rm clyptus-collector:phase0   # prints CLI help
```
