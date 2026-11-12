# Clyptus Collector — Project Instructions

Standalone Python tool that collects publicly available job postings from company career
sites, validates and stores them locally (SQLite), and exports them to Excel for the Clyptus
Job Portal.

## Working rules

- **One phase per session.** Implement only the phase requested; stop and summarize after it.
- **No live websites in tests.** Use fixtures and local mocks only; block sockets where useful.
- **No guessed job information.** Never invent jobs, companies, URLs, or fields. Data comes from
  fetched pages or fixtures only.
- **No database schema changes without approval.**
- **No changes to unrelated repositories.** Work only inside this directory; never touch the
  Clyptus Job Portal (frontend, backend, Prisma schema, shared databases).
- **Minimal code and dependency additions.** No new dependency or abstraction without need.
  No Redis, PostgreSQL, BullMQ, Ollama, or AI integrations unless a phase explicitly adds them.
- **Test and lint before declaring a phase complete:**
  `python -m pytest -q`, `ruff check .`, `ruff format --check .`
- **No automatic commits or pushes.** Wait for explicit approval.
- **Stop and summarize after each phase.** Do not start the next phase.

## Commands

```bash
python -m pip install -e ".[dev]"
collector --help
python -m pytest -q
ruff check . && ruff format --check .
```

## Conventions

- Python 3.12+, full type annotations, Ruff for lint and formatting.
- Configuration lives in `collector/settings.py` (`COLLECTOR_*` environment variables).
- Importing a module must never create files, open databases, or use the network.
