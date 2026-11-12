"""SQLite storage for jobs and crawl runs (built-in ``sqlite3``, no ORM).

Nothing here touches the filesystem on import. ``init_db`` is the only function that creates a
database file; ``connect`` refuses to create one.

Transactions
    Every write function runs in one explicit ``BEGIN IMMEDIATE`` ... ``COMMIT`` transaction and
    rolls back completely on any error. Connections use ``isolation_level=None`` so the
    transaction boundaries are exactly the ones in this module. If the caller already has a
    transaction open on the connection, the write joins it and the caller commits or rolls back.

Run lifecycle
    ``start_run`` -> any number of ``upsert_job`` calls -> ``finish_run``.
    Each job is recorded at most once per run in ``run_job_observations`` together with the
    outcome of its *first* accepted sighting (NEW / UPDATED / UNCHANGED). A later sighting of the
    same job in the same run is a complete no-op (first accepted record wins): the job row, its
    fingerprint, timestamps, posted date and observation are left alone and nothing is counted.
    The job write and its observation are committed in one transaction.

    ``finish_run`` derives the run's counters from that run's observations only
    (``jobs_seen`` = observations, ``jobs_new`` / ``jobs_updated`` = NEW / UPDATED ones), so
    retries, repeats and later runs can never change a finished run's counters, and calling
    ``finish_run`` again is a no-op. While a run is RUNNING, ``jobs.last_successful_run_id`` is
    never touched; SUCCEEDED points it at the run for exactly the jobs observed in that run;
    FAILED never does. Nothing here ever sets a job to CLOSED (expiry is a later phase).

Schema versions (``PRAGMA user_version``)
    1: ``crawl_runs`` and ``jobs``.
    2: adds ``run_job_observations``.
    ``init_db`` creates a fresh database directly at version 2 and migrates a version-1
    database in one transaction by adding the new table. Existing jobs and completed runs are
    left exactly as they are, and historical observations are *not* reconstructed (completed runs
    keep the counters they were finished with). Migration is refused, with nothing changed, while
    any run is still RUNNING, because its earlier sightings were never observed; such runs must
    be resolved first (they are never silently marked FAILED). A newer, unsupported version is
    rejected. ``connect`` requires version 2.

Limitations
    - A process that crashes mid-run leaves that run RUNNING. Jobs it already wrote are kept
      (each upsert is committed) but never receive a successful-run reference. Nothing sweeps
      stale RUNNING runs automatically; call ``finish_run(..., "FAILED")`` on them.
    - ``last_seen_at`` is the time of the latest *accepted* sighting; a same-run duplicate does
      not move it.
    - Re-sightings of a job update its stored fields without moving ``updated_at`` unless the
      content fingerprint changed (``updated_at`` tracks content, not observation).
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from collector.models import JobRecord
from collector.utils.fingerprint import content_fingerprint
from collector.utils.identity import job_key
from collector.utils.urls import registered_domain

SCHEMA_VERSION = 2

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS crawl_runs (
        id            INTEGER PRIMARY KEY,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        status        TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
        jobs_seen     INTEGER NOT NULL DEFAULT 0 CHECK (jobs_seen >= 0),
        jobs_new      INTEGER NOT NULL DEFAULT 0 CHECK (jobs_new >= 0),
        jobs_updated  INTEGER NOT NULL DEFAULT 0 CHECK (jobs_updated >= 0),
        errors        INTEGER NOT NULL DEFAULT 0 CHECK (errors >= 0),
        error_message TEXT,
        CHECK ((status = 'RUNNING') = (finished_at IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id                     INTEGER PRIMARY KEY,
        job_key                TEXT NOT NULL UNIQUE,
        title                  TEXT NOT NULL CHECK (length(trim(title)) > 0),
        company_name           TEXT NOT NULL CHECK (length(trim(company_name)) > 0),
        company_domain         TEXT,
        apply_url              TEXT NOT NULL CHECK (length(apply_url) > 0),
        source_url             TEXT NOT NULL CHECK (length(source_url) > 0),
        platform               TEXT NOT NULL CHECK (length(trim(platform)) > 0),
        collected_at           TEXT NOT NULL,
        company_website        TEXT,
        location               TEXT,
        work_type              TEXT CHECK (work_type IN ('remote', 'hybrid', 'onsite')),
        employment_type        TEXT,
        salary_text            TEXT,
        posted_date            TEXT,
        posted_date_source     TEXT NOT NULL
                               CHECK (posted_date_source IN
                                      ('jsonld', 'platform', 'page_text', 'unknown')),
        external_id            TEXT,
        description_snippet    TEXT,
        content_fingerprint    TEXT NOT NULL CHECK (length(content_fingerprint) = 64),
        first_seen_at          TEXT NOT NULL,
        last_seen_at           TEXT NOT NULL,
        last_successful_run_id INTEGER REFERENCES crawl_runs (id),
        status                 TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_job_observations (
        run_id      INTEGER NOT NULL REFERENCES crawl_runs (id),
        job_id      INTEGER NOT NULL REFERENCES jobs (id),
        outcome     TEXT NOT NULL CHECK (outcome IN ('NEW', 'UPDATED', 'UNCHANGED')),
        observed_at TEXT NOT NULL,
        PRIMARY KEY (run_id, job_id)
    ) WITHOUT ROWID
    """,
)

# Optional values that a later, emptier extraction must not erase.
_KEEP_IF_MISSING = (
    "company_website",
    "location",
    "work_type",
    "employment_type",
    "salary_text",
    "external_id",
    "description_snippet",
)

# Posted-date confidence by source. Platform and JSON-LD are structured data (high); text
# scraped from the page is less reliable (low); an unknown source is the weakest.
_SOURCE_RANK = {"platform": 2, "jsonld": 2, "page_text": 1, "unknown": 0}


class UpsertOutcome(StrEnum):
    NEW = "NEW"  # first sighting: row inserted
    UPDATED = "UPDATED"  # re-sighting whose effective content fingerprint changed
    UNCHANGED = "UNCHANGED"  # re-sighting with identical effective content
    DUPLICATE = "DUPLICATE"  # job already observed in this run: nothing was written


@dataclass(frozen=True)
class PostedDateConflict:
    """Two equally trustworthy posted dates disagreed; the stored one was kept."""

    kept: str
    kept_source: str
    rejected: str
    rejected_source: str


@dataclass(frozen=True)
class UpsertResult:
    outcome: UpsertOutcome
    posted_date_conflict: PostedDateConflict | None = None


def resolve_posted_date(
    old_date: str | None, old_source: str, new_date: str | None, new_source: str
) -> tuple[str | None, str, PostedDateConflict | None]:
    """Decide which posted date (and source) to store after a re-sighting.

    - No incoming date: keep the stored one (a known date is never erased).
    - No stored date: take the incoming one.
    - Both known: the higher-confidence source wins (platform = jsonld > page_text > unknown);
      a lower-confidence date never replaces a higher-confidence one.
    - Equal confidence: keep the stored value (first value wins). If the dates differ, the
      disagreement is returned as a ``PostedDateConflict`` so callers can report it.
    """
    if new_date is None:
        return old_date, old_source, None
    if old_date is None:
        return new_date, new_source, None
    old_rank, new_rank = _SOURCE_RANK[old_source], _SOURCE_RANK[new_source]
    if new_rank > old_rank:
        return new_date, new_source, None
    if new_rank == old_rank and new_date != old_date:
        return old_date, old_source, PostedDateConflict(old_date, old_source, new_date, new_source)
    return old_date, old_source, None


def _now(now: datetime | None) -> datetime:
    return datetime.now(UTC) if now is None else now


def _utc_text(moment: datetime) -> str:
    """Fixed-width UTC ISO-8601 text (``2026-01-15T12:00:00.000000Z``); sorts chronologically."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, params).fetchone()


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:  # join the caller's transaction
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(path: str | Path) -> None:
    """Create the database (and parent directories) at the current schema version, or migrate
    an existing version-1 database to version 2. Idempotent; never deletes data.

    Raises ``RuntimeError`` for an unsupported schema version, or when a version-1 database
    still has RUNNING runs (nothing is changed in that case).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with _transaction(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(SCHEMA_VERSION + 1):
                raise RuntimeError(f"unsupported database schema version {version}")
            if version == 1:
                running = [
                    row[0]
                    for row in conn.execute("SELECT id FROM crawl_runs WHERE status = 'RUNNING'")
                ]
                if running:
                    raise RuntimeError(
                        f"cannot migrate to schema version {SCHEMA_VERSION}: crawl run(s) "
                        f"{running} are still RUNNING. Resolve them first (for example mark them "
                        "FAILED), then run init_db again."
                    )
            for statement in _SCHEMA:  # every statement is IF NOT EXISTS
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open an existing database at the current schema version, with foreign keys enforced.

    Raises ``sqlite3.OperationalError`` if the file does not exist (it is never created here)
    and ``RuntimeError`` if its schema version is not the current one.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            hint = (
                "run init_db" if version < SCHEMA_VERSION else "it was created by a newer version"
            )
            raise RuntimeError(
                f"database schema version is {version}, expected {SCHEMA_VERSION}; {hint}"
            )
    except BaseException:
        conn.close()
        raise
    return conn


def start_run(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Record a new RUNNING crawl run and return its id."""
    with _transaction(conn):
        cursor = conn.execute(
            "INSERT INTO crawl_runs (started_at, status) VALUES (?, 'RUNNING')",
            (_utc_text(_now(now)),),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _record_columns(record: JobRecord) -> dict[str, Any]:
    website = str(record.company_website) if record.company_website else None
    return {
        "title": record.title,
        "company_name": record.company_name,
        "company_domain": registered_domain(website) if website else None,
        "apply_url": str(record.apply_url),
        "source_url": str(record.source_url),
        "platform": record.platform,
        "collected_at": _utc_text(record.collected_at),
        "company_website": website,
        "location": record.location,
        "work_type": record.work_type,
        "employment_type": record.employment_type,
        "salary_text": record.salary_text,
        "posted_date": record.posted_date.isoformat() if record.posted_date else None,
        "posted_date_source": record.posted_date_source,
        "external_id": record.external_id,
        "description_snippet": record.description_snippet,
    }


def _merge(
    old: sqlite3.Row, new: dict[str, Any]
) -> tuple[dict[str, Any], PostedDateConflict | None]:
    """Effective values after a re-sighting: incoming values win, except that a missing
    optional value never erases a stored one, and the posted date follows
    ``resolve_posted_date``."""
    merged = dict(new)
    for column in _KEEP_IF_MISSING:
        if merged[column] is None:
            merged[column] = old[column]
    merged["posted_date"], merged["posted_date_source"], conflict = resolve_posted_date(
        old["posted_date"],
        old["posted_date_source"],
        new["posted_date"],
        new["posted_date_source"],
    )
    website = merged["company_website"]
    merged["company_domain"] = registered_domain(website) if website else None
    return merged, conflict


def _open_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    run = _fetch_one(conn, "SELECT status, started_at FROM crawl_runs WHERE id = ?", (run_id,))
    if run is None:
        raise ValueError(f"unknown crawl run {run_id}")
    return run


def upsert_job(
    conn: sqlite3.Connection, record: JobRecord, run_id: int, *, now: datetime | None = None
) -> UpsertResult:
    """Record the first accepted sighting of a job in a run, atomically.

    In one transaction this inserts the job (or merges a re-sighting into the existing row)
    and records the run's observation with outcome NEW / UPDATED / UNCHANGED. If the job
    already has an observation in this run, nothing at all is written and ``DUPLICATE`` is
    returned (first accepted record wins).

    New job: all timestamps set to ``now``, status OPEN. Existing job: ``last_seen_at`` moves
    forward; stored fields are refreshed from the incoming record but optional values that
    the incoming record lacks are kept; ``first_seen_at``, ``created_at``, ``status`` and
    ``last_successful_run_id`` are never changed; ``updated_at`` moves only if the content
    fingerprint of the *merged* record differs from the stored one. A CLOSED job stays CLOSED.
    The posted date follows ``resolve_posted_date``; an equal-confidence disagreement is
    reported in ``UpsertResult.posted_date_conflict``. The posted date is not part of the
    fingerprint, so replacing it does not by itself change the outcome or ``updated_at``.

    Only validated ``JobRecord`` objects are accepted, and only for a RUNNING run.
    """
    if not isinstance(record, JobRecord):
        raise TypeError("upsert_job requires a validated JobRecord")
    stamp = _utc_text(_now(now))
    new = _record_columns(record)
    key = job_key(record)

    with _transaction(conn):
        run = _open_run(conn, run_id)
        if run["status"] != "RUNNING":
            raise ValueError(f"crawl run {run_id} is {run['status']}, not RUNNING")
        if stamp < run["started_at"]:
            raise ValueError("timestamp precedes the start of the crawl run")

        old = _fetch_one(conn, "SELECT * FROM jobs WHERE job_key = ?", (key,))
        conflict = None
        if old is None:
            row = {
                **new,
                "job_key": key,
                "content_fingerprint": content_fingerprint(new),
                "first_seen_at": stamp,
                "last_seen_at": stamp,
                "status": "OPEN",
                "created_at": stamp,
                "updated_at": stamp,
            }
            columns = ", ".join(row)
            placeholders = ", ".join(f":{name}" for name in row)
            cursor = conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", row)
            job_id, outcome = cursor.lastrowid, UpsertOutcome.NEW
        else:
            job_id = old["id"]
            observed = conn.execute(
                "SELECT 1 FROM run_job_observations WHERE run_id = ? AND job_id = ?",
                (run_id, job_id),
            ).fetchone()
            if observed:
                return UpsertResult(UpsertOutcome.DUPLICATE)
            merged, conflict = _merge(old, new)
            fingerprint = content_fingerprint(merged)
            changed = fingerprint != old["content_fingerprint"]
            row = {
                **merged,
                "content_fingerprint": fingerprint,
                "last_seen_at": stamp,
                "updated_at": stamp if changed else old["updated_at"],
            }
            assignments = ", ".join(f"{name} = :{name}" for name in row)
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = :id", {**row, "id": job_id})
            outcome = UpsertOutcome.UPDATED if changed else UpsertOutcome.UNCHANGED

        conn.execute(
            "INSERT INTO run_job_observations (run_id, job_id, outcome, observed_at) "
            "VALUES (?, ?, ?, ?)",
            (run_id, job_id, outcome.value, stamp),
        )
        return UpsertResult(outcome, conflict)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: Literal["SUCCEEDED", "FAILED"],
    *,
    errors: int = 0,
    error_message: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Finalize a RUNNING run. Returns True if this call finished it, False if it had already
    been finished (the call is then a no-op, so retries cannot double-count).

    Writes the run's counters, derived from its observations (see module docstring).
    ``errors`` is the number of rejected or failed items, supplied by the caller. Only
    SUCCEEDED advances ``last_successful_run_id`` (for jobs observed in the run); FAILED
    leaves every job as is.
    """
    if status not in ("SUCCEEDED", "FAILED"):
        raise ValueError(f"status must be SUCCEEDED or FAILED, not {status!r}")
    if errors < 0:
        raise ValueError("errors must not be negative")
    stamp = _utc_text(_now(now))

    with _transaction(conn):
        run = _open_run(conn, run_id)
        if run["status"] != "RUNNING":
            return False
        if stamp < run["started_at"]:
            raise ValueError("finish time precedes the start of the crawl run")

        seen, new, updated = conn.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(outcome = 'NEW'), 0),
                   COALESCE(SUM(outcome = 'UPDATED'), 0)
            FROM run_job_observations WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE crawl_runs
            SET status = ?, finished_at = ?, jobs_seen = ?, jobs_new = ?, jobs_updated = ?,
                errors = ?, error_message = ?
            WHERE id = ? AND status = 'RUNNING'
            """,
            (status, stamp, seen, new, updated, errors, error_message, run_id),
        )
        if status == "SUCCEEDED":
            conn.execute(
                "UPDATE jobs SET last_successful_run_id = ? WHERE id IN "
                "(SELECT job_id FROM run_job_observations WHERE run_id = ?)",
                (run_id, run_id),
            )
    return True
