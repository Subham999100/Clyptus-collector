"""The daily collection: lock -> recover -> collect -> stale report -> dated Excel workbook.

Nothing new is collected, stored or written here: the crawl is the caller's (the CLI passes
``crawl_sources``), storage is ``run_collection`` and the workbook is ``export_jobs``. This
module only composes them for an unattended, scheduled run:

1. The output name is fixed from the business date (``Settings.timezone``, Asia/Kolkata):
   ``jobs_<YYYY-MM-DD>.xlsx``. If it already exists the run is refused *before* anything is
   crawled, unless ``overwrite`` is set (the file is then replaced atomically by the exporter).
2. ``database_lock`` makes the run exclusive for its database (another run fails at once with
   ``LockBusy``; nothing is crawled or written).
3. Runs left RUNNING by an interrupted process are marked FAILED. Holding the lock proves no
   live collector process owns them (``collector run`` and ``collector daily`` both take it).
4. ``run_collection`` crawls and stores, with its existing run accounting and status policy.
5. Stale candidates are *reported*, never closed (see ``stale_candidates``).
6. The workbook is exported according to the outcome:

   - COMPLETE (run SUCCEEDED: every source was usable): ``jobs_<date>.xlsx``.
   - PARTIAL (run FAILED, but at least one source was usable): ``jobs_<date>_partial_run<id>.xlsx``.
     Its name says that not every source was collected; it never takes the complete name and
     never replaces an existing file.
   - FAILED (no source was usable): no workbook, so earlier data is never presented as today's.

Every workbook holds *all* stored OPEN jobs within the date window (the existing export rules),
not only jobs first seen today; the run's own new/updated/unchanged counts are reported
separately.

Job closing: schema version 2 cannot prove that a job is gone. ``crawl_runs`` keeps one status
per run, not per source; jobs carry no reference to the configured source they came from; and
nothing records whether a source's listing was complete (pagination stopped by the page limit,
a missing detail page, a source dropped from the sources file). So no job is ever closed here.
``stale_candidates`` lists OPEN jobs absent from the last two successful runs for a person to
review; automatic closing needs per-source completeness records, i.e. an approved schema change.
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from collector.export.excel import ExportError, ExportSummary, export_jobs
from collector.pipelines.ingest import CollectionError, IngestSummary, run_collection
from collector.pipelines.store import connect, finish_run, init_db
from collector.settings import get_settings
from collector.spiders.run import SourceResult
from collector.spiders.sources import Source
from collector.utils.lock import database_lock

STALE_AFTER_SUCCESSFUL_RUNS = 2
INTERRUPTED = "interrupted: the process ended before the run finished"


class DailyOutcome(StrEnum):
    COMPLETE = "COMPLETE"  # every source usable; the dated workbook was written
    PARTIAL = "PARTIAL"  # some sources failed; a workbook named as partial was written
    FAILED = "FAILED"  # no source usable; no workbook


class OutputExists(Exception):
    """Today's workbook already exists and ``overwrite`` was not set; nothing was done."""


@dataclass(frozen=True)
class StaleCandidate:
    company_name: str
    title: str
    platform: str
    apply_url: str
    last_seen_at: str


@dataclass(frozen=True)
class DailyReport:
    business_date: date
    outcome: DailyOutcome
    ingest: IngestSummary
    started_at: str
    finished_at: str
    recovered_runs: tuple[int, ...]  # interrupted runs marked FAILED before this run
    stale: tuple[StaleCandidate, ...]
    workbook: ExportSummary | None
    export_error: str | None

    @property
    def duration_seconds(self) -> float:
        start = datetime.fromisoformat(self.started_at)
        return (datetime.fromisoformat(self.finished_at) - start).total_seconds()

    @property
    def sources_completed(self) -> int:
        return self.ingest.sources - self.ingest.sources_failed


def business_date(now: datetime | None = None) -> date:
    """Today's date in the business timezone."""
    moment = now if now is not None else datetime.now(UTC)
    return moment.astimezone(get_settings().tz).date()


def workbook_path(output_dir: str | Path, day: date, *, partial_run: int | None = None) -> Path:
    """``jobs_<date>.xlsx``, or ``jobs_<date>_partial_run<id>.xlsx`` for a partial run."""
    suffix = f"_partial_run{partial_run}" if partial_run is not None else ""
    return Path(output_dir) / f"jobs_{day.isoformat()}{suffix}.xlsx"


def recover_interrupted_runs(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[int]:
    """Mark every RUNNING run FAILED. Only call this while holding the database lock: then no
    live collector process owns a RUNNING run. The jobs such a run stored stay as they are and
    get no successful-run reference (``finish_run`` FAILED semantics)."""
    running = [row[0] for row in conn.execute("SELECT id FROM crawl_runs WHERE status='RUNNING'")]
    for run_id in running:
        finish_run(conn, run_id, "FAILED", error_message=INTERRUPTED, now=now)
    return running


def stale_candidates(conn: sqlite3.Connection) -> list[StaleCandidate]:
    """OPEN jobs not seen since before the last ``STALE_AFTER_SUCCESSFUL_RUNS`` successful runs
    began (none until there have been that many). A report for review only: a successful run
    does not prove every source's listing was complete, so nothing is closed."""
    rows = conn.execute(
        """
        WITH recent AS (
            SELECT started_at FROM crawl_runs WHERE status = 'SUCCEEDED'
            ORDER BY id DESC LIMIT ?
        )
        SELECT company_name, title, platform, apply_url, last_seen_at FROM jobs
        WHERE status = 'OPEN'
          AND (SELECT COUNT(*) FROM recent) = ?
          AND last_seen_at < (SELECT MIN(started_at) FROM recent)
        ORDER BY last_seen_at, job_key
        """,
        (STALE_AFTER_SUCCESSFUL_RUNS, STALE_AFTER_SUCCESSFUL_RUNS),
    ).fetchall()
    return [StaleCandidate(*row) for row in rows]


def run_daily(
    sources: Sequence[Source],
    db_path: str | Path,
    output_dir: str | Path,
    crawl: Callable[[list[Source]], list[SourceResult]],
    *,
    overwrite: bool = False,
    now: datetime | None = None,
) -> DailyReport:
    """Run one daily collection (see the module docstring).

    Raises ``OutputExists`` (nothing done), ``LockBusy`` (nothing done), ``CollectionError``
    (the database could not be used or the crawl failed as a whole; the run, if started, is
    FAILED and no workbook is written) and lets ``KeyboardInterrupt`` through after the run is
    marked FAILED. An export failure is not raised: it is reported in ``export_error``, after
    the collection itself was stored.
    """
    day = business_date(now)
    complete_path = workbook_path(output_dir, day)
    if complete_path.exists() and not overwrite:
        raise OutputExists(f"today's workbook already exists: {complete_path}")
    with database_lock(db_path):
        try:
            init_db(db_path)
            conn = connect(db_path)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            raise CollectionError(f"database unavailable: {type(exc).__name__}: {exc}") from exc
        try:
            try:
                recovered = recover_interrupted_runs(conn, now=now)
            except (sqlite3.Error, ValueError) as exc:  # ValueError: a run starts in the future
                raise CollectionError(f"cannot recover runs: {type(exc).__name__}") from exc
        finally:
            conn.close()

        ingest = run_collection(db_path, sources, crawl, now=now)

        conn = connect(db_path)
        try:
            started_at, finished_at = conn.execute(
                "SELECT started_at, finished_at FROM crawl_runs WHERE id = ?", (ingest.run_id,)
            ).fetchone()
            stale = stale_candidates(conn)
        finally:
            conn.close()

        usable = ingest.sources - ingest.sources_failed
        if ingest.status == "SUCCEEDED":
            outcome, target, replace = DailyOutcome.COMPLETE, complete_path, overwrite
        elif usable > 0:
            target = workbook_path(output_dir, day, partial_run=ingest.run_id)
            outcome, replace = DailyOutcome.PARTIAL, False
        else:
            outcome, target, replace = DailyOutcome.FAILED, None, False

        workbook, export_error = None, None
        if target is not None:
            try:
                workbook = export_jobs(db_path, target, overwrite=replace, reference=now)
            except ExportError as exc:
                export_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - reported as an export failure, never lost
                export_error = f"unexpected export error ({type(exc).__name__})"
    return DailyReport(
        business_date=day,
        outcome=outcome,
        ingest=ingest,
        started_at=started_at,
        finished_at=finished_at,
        recovered_runs=tuple(recovered),
        stale=tuple(stale),
        workbook=workbook,
        export_error=export_error,
    )
