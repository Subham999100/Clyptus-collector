# Clyptus Collector

Standalone tool that will collect publicly available job postings from company career sites,
validate and store them in SQLite, and export them to Excel for the Clyptus Job Portal.

**Status: Phase 5.** `run` crawls configured sources into SQLite and `export` writes the stored
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
