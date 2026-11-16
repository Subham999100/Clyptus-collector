# Daily collection (Phase 9)

`collector daily` is the unattended daily run: it crawls the approved sources, stores the jobs
in SQLite and writes a dated Excel workbook. It composes the existing pieces (`load_sources`,
the protected crawler, `run_collection`, `export_jobs`); it adds only a run lock, recovery of
interrupted runs, a stale-job report and the file-naming rules below. It never prompts, so it can
run from cron or a systemd timer. Nothing here has been run against real company sites yet.

```bash
collector daily --sources /srv/clyptus/approved_sources.csv \
                --db /srv/clyptus/data/collector.db --output-dir /srv/clyptus/exports
```

`--sources` is the same file `collector run` reads (for example the output of
`collector sources export`, plus any reviewed `html` sources). `--db` defaults to
`COLLECTOR_SQLITE_PATH` (else `data/collector.db`) and is created if missing; `--output-dir`
defaults to `COLLECTOR_OUTPUT_DIR` (else `output/`).

## What one run does

1. Loads and validates the sources file (unapproved or invalid sources stop the run, exit 2).
2. Fixes the workbook name from the **business date** (`COLLECTOR_TIMEZONE`, default
   `Asia/Kolkata`): `jobs_YYYY-MM-DD.xlsx`. If that file already exists the run stops before
   anything is crawled (exit 2) unless `--overwrite` is given.
3. Takes the database lock (`<db>.lock`). If another `daily` or `run` holds it, exits 5 at once.
4. Marks runs that an interrupted process left `RUNNING` as `FAILED` ("interrupted: the process
   ended before the run finished"). Holding the lock proves no live process owns them.
5. Crawls and stores with the existing `run_collection` (deduplication, date window, run
   counters, SUCCEEDED/FAILED policy, unchanged).
6. Lists stale candidates (see below). No job is ever closed.
7. Exports with the existing exporter (same columns, OPEN-job filter, date window, text-only
   cells, safe links, deterministic order, temp-file-then-rename writing).
8. Prints a summary and exits with a status code.

## Workbook and outcome policy

| Outcome | When | Workbook | Exit |
| --- | --- | --- | --- |
| COMPLETE | run SUCCEEDED: every source returned a usable response | `jobs_<date>.xlsx` | 0 |
| PARTIAL | run FAILED, at least one source usable | `jobs_<date>_partial_run<id>.xlsx` | 1 |
| FAILED | no source usable, crawl crashed or database unusable | none | 3 |

- A workbook always holds **every stored OPEN job within the date window** (unknown-date jobs
  labelled for review), not only jobs first seen today. The run's own new / updated / unchanged
  counts are printed separately.
- A partial workbook never takes the complete name and never replaces any file; a complete
  workbook is replaced only with `--overwrite`, atomically. Earlier days' workbooks are never
  touched.
- Individual invalid jobs do not make a run partial (the existing run policy); they are counted
  as "invalid" in the summary.

Other exit codes: 2 unusable sources file or today's workbook exists (nothing crawled); 4 the
export failed after the jobs were stored (the database is consistent; no workbook was written,
earlier ones are kept); 5 another run is using the database; 130 interrupted (run marked
FAILED). `collector run` now takes the same lock and also exits 5 when it is held.

## Summary output

Per source: records, failures, warnings, skipped, fetch errors (reasons only, URLs without
query strings). Then: business date, outcome, run id and status, start/finish time and
duration, sources configured / completed / failed / blocked, jobs new / updated / unchanged /
stored, invalid, skipped, duplicates, warnings, posted-date classes, stale candidates (count and
up to 20 listed), "closed jobs: none", the workbook path and row counts, and error counts. All
numbers come from the run's results and the `crawl_runs` / `run_job_observations` tables; there
is no separate monitoring store. Scrapy's own log goes to stderr.

## Job lifecycle: why nothing is closed

Closing a job needs proof that its own source was listed completely, successfully and more than
once without it. Database schema version 2 cannot provide that:

- `crawl_runs` stores one status per run, not per source; per-source outcomes are not stored.
- `jobs` has no reference to the configured source a job came from (`source_url` is the page
  read, which for `html` detail pages is the job's own page).
- Nothing records whether a listing was complete: an `html` source stopped by `max_pages`, a
  skipped detail page or a source removed from the sources file all look like absence.

So automatic closing stays disabled. Instead the summary lists **stale candidates**: OPEN jobs
not seen since before the last two SUCCEEDED runs started. Failed runs never count, and a job
that reappears drops off the list. It is a review list only; jobs stay OPEN and keep appearing
in the workbook. Enabling closure would need per-run, per-source completeness records, i.e. an
approved schema change.

## Locking and recovery

The lock is an OS file lock (`flock` on Linux, `msvcrt.locking` on Windows) on `<db>.lock`. The
operating system releases it when the process ends for any reason, including a crash or
`kill -9`, so there are no permanent false locks; the file itself stays and is harmless. It works
for one host only; keep the database on a local disk (network filesystems may ignore it).

After a crash or kill: jobs already stored stay stored (each write is its own transaction), the
run stays `RUNNING` until the next `daily` marks it `FAILED`, and no workbook was written (the
offline tests kill a run mid-crawl and check exactly this). On `SIGTERM`/Ctrl+C Scrapy's own
shutdown handler stops the crawl; sources that did not finish then count as failed, so the run
cannot be reported as complete (inherited Scrapy behaviour, not covered by the offline tests).

## Scheduling at 02:00 Asia/Kolkata (examples, not installed)

Use a dedicated user (for example `clyptus`), absolute paths, the project's virtual
environment, and one working directory. Install with `pip install -e .` into
`/srv/clyptus/app/.venv`.

### systemd (preferred)

`/etc/systemd/system/clyptus-daily.service`:

```ini
[Unit]
Description=Clyptus Collector daily job collection
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=clyptus
Group=clyptus
WorkingDirectory=/srv/clyptus/app
Environment=COLLECTOR_TIMEZONE=Asia/Kolkata
EnvironmentFile=-/etc/clyptus/collector.env
ExecStart=/srv/clyptus/app/.venv/bin/collector daily \
    --sources /srv/clyptus/config/approved_sources.csv \
    --db /srv/clyptus/data/collector.db \
    --output-dir /srv/clyptus/exports
# Any non-zero exit marks the unit failed (alert with OnFailure=): 1 partial, 3 failed,
# 4 export failed, 5 a previous run was still going.
TimeoutStartSec=4h
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/srv/clyptus/data /srv/clyptus/exports
```

`/etc/systemd/system/clyptus-daily.timer`:

```ini
[Unit]
Description=Run the Clyptus Collector daily at 02:00 Asia/Kolkata

[Timer]
OnCalendar=*-*-* 02:00:00 Asia/Kolkata
Persistent=true
RandomizedDelaySec=0

[Install]
WantedBy=timers.target
```

Enable with `systemctl daemon-reload && systemctl enable --now clyptus-daily.timer`; read the
summaries with `journalctl -u clyptus-daily.service`. `Persistent=true` runs a missed day after
downtime; the lock stops it from overlapping a run that is still going.

### cron

```cron
CRON_TZ=Asia/Kolkata
0 2 * * * cd /srv/clyptus/app && /srv/clyptus/app/.venv/bin/collector daily --sources /srv/clyptus/config/approved_sources.csv --db /srv/clyptus/data/collector.db --output-dir /srv/clyptus/exports >> /srv/clyptus/logs/daily.log 2>&1
```

`CRON_TZ` is honoured by cronie (most Linux distributions); with other cron implementations set
the server timezone or convert 02:00 IST to the server's time. Keep `/etc/clyptus/collector.env`
(any `COLLECTOR_*` overrides) readable by the service user only.

## Before production

Offline tests prove the mechanics only. Still required: authorized real sources, a first pilot
on real company sites, the real-network/TLS smoke checks from Phase 4, installing the timer on
the server, and alerting on non-zero exit codes.
