"""Connect a crawl to the Phase 1 pipeline: dedupe -> date window -> SQLite store.

No new algorithms live here: ``dedupe`` decides identity (first record wins), ``check_date_window``
classifies the posted date, and ``start_run`` / ``upsert_job`` / ``finish_run`` do all the
storage and counting. The adapters already validated every record (``validate_job``), so only
``JobRecord`` objects arrive. Importing this module opens nothing.

Per run
    1. ``init_db`` (idempotent, never deletes) and ``connect``; ``start_run``.
    2. Crawl. 3. ``dedupe`` across all sources. 4. Drop ``OLD_DATE`` records; keep ELIGIBLE
    (including the one-day future tolerance), UNKNOWN_DATE and FUTURE_DATE records (the latter
    two need review; the database has no column for that, so the date classification is simply
    recomputed from the stored ``posted_date`` when exporting). 5. ``upsert_job`` each kept record.
    6. ``finish_run``.

Run status (the schema only has SUCCEEDED / FAILED; there is no PARTIAL)
    - SUCCEEDED only when every source was usable and nothing fatal happened. A usable source
      has no fetch failure or block and returned a recognized response (a recognized empty
      listing counts). Individual rejected jobs and adapter warnings do not fail the run; they
      are counted in ``errors`` and reported.
    - FAILED when any source is blocked, fails to fetch or returns an unusable response, and on
      any fatal error (database write failure, crawl crash, interrupt), even if other sources
      succeeded. Their valid jobs stay stored with their observations and accurate counters,
      but FAILED never advances ``last_successful_run_id``. Nothing is ever deleted or closed
      because a crawl failed.
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from collector.models import JobRecord
from collector.pipelines.date_window import DateOutcome, check_date_window
from collector.pipelines.dedupe import dedupe
from collector.pipelines.store import connect, finish_run, init_db, start_run, upsert_job
from collector.settings import get_settings
from collector.spiders.run import SourceResult
from collector.spiders.sources import Source


class CollectionError(Exception):
    """The run could not be completed. ``run_id`` (if one was started) was marked FAILED when
    that was possible. Messages never contain source content."""

    def __init__(self, message: str, *, run_id: int | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


@dataclass(frozen=True)
class IngestSummary:
    run_id: int
    status: str
    sources: int
    sources_failed: int  # fetch failure/block, or nothing recognizable came back
    sources_blocked: int
    fetch_failures: int
    extraction_failures: int
    unique: int  # records left after de-duplication
    duplicates: int
    eligible: int
    unknown_date: int
    old_date: int  # excluded, not stored
    future_date: int  # stored; flagged by the date window as suspicious
    jobs_seen: int  # crawl_runs counters, derived by finish_run from the run's observations
    jobs_new: int
    jobs_updated: int
    errors: int

    @property
    def jobs_unchanged(self) -> int:
        return self.jobs_seen - self.jobs_new - self.jobs_updated


def _usable(result: SourceResult) -> bool:
    recognized = any(extraction.response_type is not None for extraction in result.extractions)
    return recognized and not result.fetch_failures


def store_results(
    conn: sqlite3.Connection,
    run_id: int,
    results: Sequence[SourceResult],
    *,
    now: datetime | None = None,
) -> IngestSummary:
    """Process crawl results into the (RUNNING) run and finish it. Returns the summary.

    Raises whatever the store raises on a write failure; the caller (``run_collection``) marks
    the run FAILED. ``now`` fixes the clock for both the date window and the stored timestamps.
    """
    reference = now if now is not None else datetime.now(UTC)
    records: list[JobRecord] = [record for result in results for record in result.records]
    deduped = dedupe(records)

    settings = get_settings()  # read once, not once per record
    counts = dict.fromkeys(DateOutcome, 0)
    for record in deduped.accepted:
        outcome = check_date_window(
            record, reference, settings.date_window_days, settings.tz
        ).outcome
        counts[outcome] += 1
        if outcome is not DateOutcome.OLD_DATE:
            upsert_job(conn, record, run_id, now=now)

    failed = [result for result in results if not _usable(result)]
    blocked = sum(1 for result in results if result.blocked)
    fetch_failures = sum(len(result.fetch_failures) for result in results)
    extraction_failures = sum(len(e.failures) for result in results for e in result.extractions)
    errors = fetch_failures + extraction_failures
    status = "FAILED" if failed or not results else "SUCCEEDED"  # source-level failures only
    message = None
    if errors or failed:
        message = (
            f"{len(failed)} of {len(results)} sources failed ({blocked} blocked); "
            f"{fetch_failures} fetch failures, {extraction_failures} extraction failures"
        )
    finish_run(conn, run_id, status, errors=errors, error_message=message, now=now)

    seen, new, updated = conn.execute(
        "SELECT jobs_seen, jobs_new, jobs_updated FROM crawl_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return IngestSummary(
        run_id=run_id,
        status=status,
        sources=len(results),
        sources_failed=len(failed),
        sources_blocked=blocked,
        fetch_failures=fetch_failures,
        extraction_failures=extraction_failures,
        unique=len(deduped.accepted),
        duplicates=len(deduped.duplicates),
        eligible=counts[DateOutcome.ELIGIBLE],
        unknown_date=counts[DateOutcome.UNKNOWN_DATE],
        old_date=counts[DateOutcome.OLD_DATE],
        future_date=counts[DateOutcome.FUTURE_DATE],
        jobs_seen=seen,
        jobs_new=new,
        jobs_updated=updated,
        errors=errors,
    )


def run_collection(
    db_path: str | Path,
    sources: Sequence[Source],
    crawl: Callable[[list[Source]], list[SourceResult]],
    *,
    now: datetime | None = None,
) -> IngestSummary:
    """Crawl ``sources`` with ``crawl`` and store the outcome in the database at ``db_path``.

    The database is initialized with ``init_db`` first (an existing one is kept as it is, and an
    unsupported schema version is refused). Any failure after the run has started marks the run
    FAILED (when the database still allows it) and raises ``CollectionError``; an interrupt marks
    it FAILED and is re-raised unchanged.
    """
    try:
        init_db(db_path)
        conn = connect(db_path)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise CollectionError(f"database unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        try:
            run_id = start_run(conn, now=now)
        except sqlite3.Error as exc:
            raise CollectionError(f"cannot start a run: {type(exc).__name__}") from exc
        try:
            results = crawl(list(sources))
            return store_results(conn, run_id, results, now=now)
        except BaseException as exc:
            interrupted = isinstance(exc, KeyboardInterrupt | SystemExit)
            reason = "interrupted" if interrupted else f"{type(exc).__name__} during collection"
            try:
                finish_run(conn, run_id, "FAILED", error_message=reason, now=now)
            except sqlite3.Error:
                pass  # the run stays RUNNING; the original failure is what gets reported
            if interrupted:
                raise
            raise CollectionError(reason, run_id=run_id) from exc
    finally:
        conn.close()
