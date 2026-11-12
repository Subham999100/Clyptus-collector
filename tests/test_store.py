import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from collector.models import JobRecord
from collector.pipelines.dedupe import dedupe
from collector.pipelines.store import (
    SCHEMA_VERSION,
    PostedDateConflict,
    UpsertOutcome,
    UpsertResult,
    connect,
    finish_run,
    init_db,
    resolve_posted_date,
    start_run,
    upsert_job,
)
from collector.pipelines.validate import Rejection, validate_job
from collector.utils.fingerprint import content_fingerprint
from tests.conftest import make_raw, make_record

T0 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def stamp(minutes: int) -> str:
    return at(minutes).isoformat(timespec="microseconds").replace("+00:00", "Z")


def later_run(conn: sqlite3.Connection, minute: int) -> int:
    """Start another run, for re-sighting a job that an earlier run already observed."""
    return start_run(conn, now=at(minute))


def query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, params).fetchall()


def the_job(conn: sqlite3.Connection) -> sqlite3.Row:
    (row,) = query(conn, "SELECT * FROM jobs")
    return row


def the_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    (row,) = query(conn, "SELECT * FROM crawl_runs WHERE id = ?", (run_id,))
    return row


@pytest.fixture(autouse=True)
def _offline(no_network: None) -> None:
    """Every store test runs with sockets blocked."""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "collector.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    yield connection
    connection.close()


# --- initialization ------------------------------------------------------------------------


def test_database_created_only_by_explicit_init(tmp_path: Path) -> None:
    path = tmp_path / "data" / "collector.db"
    assert not path.exists()
    with pytest.raises(sqlite3.OperationalError):
        connect(path)  # connecting must not create the file
    assert not path.exists()
    init_db(path)
    assert path.exists()


def test_tables_and_version(conn: sqlite3.Connection) -> None:
    tables = {r["name"] for r in query(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"crawl_runs", "jobs", "run_job_observations"}  # no registry/expiry tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2


def test_expected_columns(conn: sqlite3.Connection) -> None:
    jobs = [r["name"] for r in query(conn, "PRAGMA table_info(jobs)")]
    assert jobs == [
        "id", "job_key", "title", "company_name", "company_domain", "apply_url", "source_url",
        "platform", "collected_at", "company_website", "location", "work_type",
        "employment_type", "salary_text", "posted_date", "posted_date_source", "external_id",
        "description_snippet", "content_fingerprint", "first_seen_at", "last_seen_at",
        "last_successful_run_id", "status", "created_at", "updated_at",
    ]  # fmt: skip
    runs = [r["name"] for r in query(conn, "PRAGMA table_info(crawl_runs)")]
    assert runs == [
        "id", "started_at", "finished_at", "status", "jobs_seen", "jobs_new", "jobs_updated",
        "errors", "error_message",
    ]  # fmt: skip


def test_job_key_is_uniquely_indexed(conn: sqlite3.Connection) -> None:
    unique_columns = []
    for index in query(conn, "PRAGMA index_list(jobs)"):
        if index["unique"]:
            unique_columns += [
                r["name"] for r in query(conn, f"PRAGMA index_info({index['name']})")
            ]
    assert unique_columns == ["job_key"]


def test_foreign_keys_enabled_on_every_connection(db_path: Path) -> None:
    for _ in range(2):
        connection = connect(db_path)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.close()


def test_foreign_key_enforced(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET last_successful_run_id = 999")


def test_init_is_idempotent_and_keeps_data(db_path: Path) -> None:
    connection = connect(db_path)
    upsert_job(connection, make_record(), start_run(connection, now=T0), now=at(1))
    connection.close()
    init_db(db_path)
    connection = connect(db_path)
    assert len(query(connection, "SELECT * FROM jobs")) == 1
    connection.close()


def test_connect_rejects_uninitialized_or_newer_database(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(RuntimeError, match="init_db"):
        connect(empty)

    future = tmp_path / "future.db"
    raw = sqlite3.connect(future)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(RuntimeError):
        connect(future)
    with pytest.raises(RuntimeError):
        init_db(future)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO crawl_runs (started_at, status) VALUES ('x', 'BOGUS')",
        "INSERT INTO crawl_runs (started_at, status, jobs_seen) VALUES ('x', 'RUNNING', -1)",
        "INSERT INTO crawl_runs (started_at, status, errors) VALUES ('x', 'RUNNING', -1)",
        # a finished run needs finished_at; a running run must not have one
        "INSERT INTO crawl_runs (started_at, status) VALUES ('x', 'SUCCEEDED')",
        "INSERT INTO crawl_runs (started_at, finished_at, status) VALUES ('x', 'y', 'RUNNING')",
        "INSERT INTO crawl_runs (status) VALUES ('RUNNING')",
    ],
)
def test_crawl_runs_constraints(conn: sqlite3.Connection, sql: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("status", "REOPENED"),
        ("work_type", "onsite-ish"),
        ("posted_date_source", "guess"),
        ("title", "   "),
        ("title", None),
        ("content_fingerprint", "short"),
        ("job_key", None),
    ],
)
def test_jobs_constraints(conn: sqlite3.Connection, column: str, value: Any) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE jobs SET {column} = ?", (value,))


def test_unique_job_key_enforced(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    row = dict(the_job(conn))
    del row["id"]
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", row)


# --- insert / re-sighting ------------------------------------------------------------------


def test_first_insert(conn: sqlite3.Connection) -> None:
    record = make_record(company_website="https://www.example.co.uk/about", location="Testville")
    run_id = start_run(conn, now=T0)

    result = upsert_job(conn, record, run_id, now=at(1))

    assert result.outcome is UpsertOutcome.NEW
    job = the_job(conn)
    assert job["first_seen_at"] == job["last_seen_at"] == stamp(1)
    assert job["created_at"] == job["updated_at"] == stamp(1)
    assert job["status"] == "OPEN"
    assert job["last_successful_run_id"] is None
    assert job["content_fingerprint"] == content_fingerprint(record.model_dump())
    assert job["company_domain"] == "example.co.uk"
    assert job["title"] == "Test Engineer"
    assert job["apply_url"] == "https://jobs.example.com/apply/123"
    assert job["collected_at"] == "2026-01-15T12:00:00.000000Z"
    assert job["posted_date"] is None
    assert job["posted_date_source"] == "unknown"
    assert job["job_key"].startswith("url:")


def test_missing_values_are_stored_as_null_not_guessed(conn: sqlite3.Connection) -> None:
    upsert_job(conn, make_record(), start_run(conn, now=T0), now=at(1))
    job = the_job(conn)
    for column in (
        "company_website", "company_domain", "location", "work_type", "employment_type",
        "salary_text", "posted_date", "external_id", "description_snippet",
    ):  # fmt: skip
        assert job[column] is None


def test_repeated_insert_is_a_resighting(conn: sqlite3.Connection) -> None:
    record = make_record(location="Testville")
    first_run = start_run(conn, now=T0)
    upsert_job(conn, record, first_run, now=at(1))
    before = dict(the_job(conn))
    finish_run(conn, first_run, "SUCCEEDED", now=at(2))
    second_run = start_run(conn, now=at(60))

    result = upsert_job(conn, make_record(location="Testville"), second_run, now=at(61))

    assert result.outcome is UpsertOutcome.UNCHANGED
    job = the_job(conn)  # still exactly one row
    assert job["id"] == before["id"]
    assert job["first_seen_at"] == before["first_seen_at"] == stamp(1)
    assert job["created_at"] == before["created_at"]
    assert job["last_seen_at"] == stamp(61)
    assert job["updated_at"] == before["updated_at"] == stamp(1)
    assert job["content_fingerprint"] == before["content_fingerprint"]


def test_stable_identity_across_url_variants(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(apply_url="https://Jobs.example.com/apply/1?utm_source=a"), run_id)
    upsert_job(conn, make_record(apply_url="https://jobs.example.com/apply/1#top"), run_id)
    assert len(query(conn, "SELECT * FROM jobs")) == 1
    upsert_job(conn, make_record(external_id="E1", apply_url="https://x.example.com/1"), run_id)
    upsert_job(conn, make_record(external_id="E1", apply_url="https://x.example.com/new"), run_id)
    assert len(query(conn, "SELECT * FROM jobs")) == 2


def test_meaningful_change_updates_updated_at_and_fingerprint(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(salary_text="10 LPA"), run_id, now=at(1))
    before = the_job(conn)

    result = upsert_job(conn, make_record(salary_text="12 LPA"), later_run(conn, 4), now=at(5))

    assert result.outcome is UpsertOutcome.UPDATED
    after = the_job(conn)
    assert after["salary_text"] == "12 LPA"
    assert after["updated_at"] == stamp(5) != before["updated_at"]
    assert after["content_fingerprint"] != before["content_fingerprint"]
    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["created_at"] == before["created_at"]
    assert after["last_seen_at"] == stamp(5)


def test_non_content_changes_do_not_touch_updated_at(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    later = make_record(
        collected_at="2026-02-01T00:00:00Z", source_url="https://other.example.com/c"
    )
    result = upsert_job(conn, later, later_run(conn, 1), now=at(2))
    assert result.outcome is UpsertOutcome.UNCHANGED
    job = the_job(conn)
    assert job["updated_at"] == stamp(1)
    assert job["source_url"] == "https://other.example.com/c"  # observation data is refreshed
    assert job["collected_at"] == "2026-02-01T00:00:00.000000Z"


def test_missing_optional_fields_do_not_erase_values(conn: sqlite3.Connection) -> None:
    full = make_record(
        company_website="https://www.example.com",
        location="Testville",
        work_type="hybrid",
        employment_type="Full-time",
        salary_text="negotiable",
        posted_date="2026-01-10",
        posted_date_source="jsonld",
        external_id="E1",
        description_snippet="Snippet",
    )
    run_id = start_run(conn, now=T0)
    upsert_job(conn, full, run_id, now=at(1))
    before = dict(the_job(conn))

    sparse = make_record(external_id="E1")  # same job, everything optional omitted
    result = upsert_job(conn, sparse, later_run(conn, 1), now=at(2))

    after = the_job(conn)
    assert result.outcome is UpsertOutcome.UNCHANGED  # fingerprint comes from the merged record
    for column in (
        "company_website", "company_domain", "location", "work_type", "employment_type",
        "salary_text", "posted_date", "posted_date_source", "description_snippet",
        "content_fingerprint", "updated_at",
    ):  # fmt: skip
        assert after[column] == before[column], column


def test_known_posted_date_never_overwritten_with_null(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(
        conn,
        make_record(posted_date="2026-01-10", posted_date_source="platform"),
        run_id,
        now=at(1),
    )
    upsert_job(conn, make_record(), later_run(conn, 1), now=at(2))
    job = the_job(conn)
    assert job["posted_date"] == "2026-01-10"
    assert job["posted_date_source"] == "platform"


def test_new_posted_date_replaces_old_one(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(posted_date="2026-01-10", posted_date_source="page_text"), run_id)
    upsert_job(
        conn,
        make_record(posted_date="2026-01-11", posted_date_source="jsonld"),
        later_run(conn, 1),
        now=at(2),
    )
    job = the_job(conn)
    assert (job["posted_date"], job["posted_date_source"]) == ("2026-01-11", "jsonld")


def test_new_info_fills_missing_and_counts_as_update(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    result = upsert_job(conn, make_record(location="Testville"), later_run(conn, 1), now=at(2))
    assert result.outcome is UpsertOutcome.UPDATED
    assert the_job(conn)["location"] == "Testville"


def test_closed_job_is_not_reopened(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    conn.execute("UPDATE jobs SET status = 'CLOSED'")
    upsert_job(conn, make_record(), later_run(conn, 1), now=at(2))
    job = the_job(conn)
    assert job["status"] == "CLOSED"
    assert job["last_seen_at"] == stamp(2)


# --- validation before storage -------------------------------------------------------------


def test_invalid_records_are_never_persisted(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    for raw in (make_raw(title=""), make_raw(apply_url="nope"), make_raw(extra="x")):
        result = validate_job(raw)
        assert isinstance(result, Rejection)  # rejected here, so never reaches upsert_job
    finish_run(conn, run_id, "SUCCEEDED", errors=3, now=at(1))
    assert query(conn, "SELECT * FROM jobs") == []
    assert the_run(conn, run_id)["jobs_seen"] == 0


def test_store_refuses_unvalidated_input(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    with pytest.raises(TypeError):
        upsert_job(conn, make_raw(), run_id)  # type: ignore[arg-type]
    assert query(conn, "SELECT * FROM jobs") == []


def test_validate_dedupe_store_pipeline(conn: sqlite3.Connection) -> None:
    raws = [make_raw(), make_raw(), make_raw(title=""), make_raw(apply_url="https://a.example/2")]
    valid: list[JobRecord] = []
    rejected = 0
    for raw in raws:
        result = validate_job(raw)
        if isinstance(result, Rejection):
            rejected += 1
        else:
            valid.append(result)
    deduped = dedupe(valid)
    run_id = start_run(conn, now=T0)
    for record in deduped.accepted:
        upsert_job(conn, record, run_id, now=at(1))
    finish_run(conn, run_id, "SUCCEEDED", errors=rejected, now=at(2))

    run = the_run(conn, run_id)
    assert (run["jobs_seen"], run["jobs_new"], run["errors"]) == (2, 2, 1)
    assert len(deduped.duplicates) == 1


# --- run lifecycle -------------------------------------------------------------------------


def test_start_run_records_running_run(conn: sqlite3.Connection) -> None:
    run = the_run(conn, start_run(conn, now=T0))
    assert run["status"] == "RUNNING"
    assert run["started_at"] == stamp(0)
    assert run["finished_at"] is None
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"], run["errors"]) == (0, 0, 0, 0)


def test_successful_reference_set_only_after_success(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    assert the_job(conn)["last_successful_run_id"] is None  # still RUNNING

    assert finish_run(conn, run_id, "SUCCEEDED", now=at(2)) is True

    assert the_job(conn)["last_successful_run_id"] == run_id
    run = the_run(conn, run_id)
    assert run["status"] == "SUCCEEDED"
    assert run["finished_at"] == stamp(2)


def test_later_successful_run_advances_reference_only_for_seen_jobs(
    conn: sqlite3.Connection,
) -> None:
    run1 = start_run(conn, now=T0)
    upsert_job(conn, make_record(apply_url="https://a.example/seen"), run1, now=at(1))
    upsert_job(conn, make_record(apply_url="https://a.example/unseen"), run1, now=at(1))
    finish_run(conn, run1, "SUCCEEDED", now=at(2))

    run2 = start_run(conn, now=at(60))
    upsert_job(conn, make_record(apply_url="https://a.example/seen"), run2, now=at(61))
    finish_run(conn, run2, "SUCCEEDED", now=at(62))

    refs = {r["apply_url"]: r["last_successful_run_id"] for r in query(conn, "SELECT * FROM jobs")}
    assert refs == {"https://a.example/seen": run2, "https://a.example/unseen": run1}


def test_failed_run_does_not_close_jobs_or_advance_reference(conn: sqlite3.Connection) -> None:
    run1 = start_run(conn, now=T0)
    upsert_job(conn, make_record(apply_url="https://a.example/seen"), run1, now=at(1))
    upsert_job(conn, make_record(apply_url="https://a.example/unseen"), run1, now=at(1))
    finish_run(conn, run1, "SUCCEEDED", now=at(2))

    run2 = start_run(conn, now=at(60))
    upsert_job(conn, make_record(apply_url="https://a.example/seen"), run2, now=at(61))
    finish_run(conn, run2, "FAILED", errors=1, error_message="boom", now=at(62))

    for job in query(conn, "SELECT * FROM jobs"):
        assert job["status"] == "OPEN"
        assert job["last_successful_run_id"] == run1
    run = the_run(conn, run2)
    assert (run["status"], run["errors"], run["error_message"]) == ("FAILED", 1, "boom")


def test_failed_run_with_no_jobs_touches_nothing(conn: sqlite3.Connection) -> None:
    run1 = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run1, now=at(1))
    finish_run(conn, run1, "SUCCEEDED", now=at(2))
    before = dict(the_job(conn))

    run2 = start_run(conn, now=at(60))
    finish_run(conn, run2, "FAILED", error_message="site down", now=at(61))

    assert dict(the_job(conn)) == before


def test_duplicate_processing_does_not_inflate_counters(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    for minute in (1, 2, 3):  # the same job processed three times in one run
        upsert_job(conn, make_record(), run_id, now=at(minute))
    upsert_job(conn, make_record(apply_url="https://a.example/other"), run_id, now=at(4))
    finish_run(conn, run_id, "SUCCEEDED", now=at(5))

    run = the_run(conn, run_id)
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"]) == (2, 2, 0)


def test_counters_count_real_changes_once(conn: sqlite3.Connection) -> None:
    run1 = start_run(conn, now=T0)
    upsert_job(
        conn, make_record(apply_url="https://a.example/1", salary_text="10"), run1, now=at(1)
    )
    upsert_job(conn, make_record(apply_url="https://a.example/2"), run1, now=at(1))
    upsert_job(conn, make_record(apply_url="https://a.example/3"), run1, now=at(1))
    finish_run(conn, run1, "SUCCEEDED", now=at(2))

    run2 = start_run(conn, now=at(60))
    upsert_job(
        conn, make_record(apply_url="https://a.example/1", salary_text="12"), run2, now=at(61)
    )
    upsert_job(
        conn, make_record(apply_url="https://a.example/1", salary_text="14"), run2, now=at(62)
    )
    upsert_job(conn, make_record(apply_url="https://a.example/2"), run2, now=at(63))  # identical
    upsert_job(conn, make_record(apply_url="https://a.example/4"), run2, now=at(64))  # new
    finish_run(conn, run2, "SUCCEEDED", now=at(65))

    run = the_run(conn, run2)
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"]) == (3, 1, 1)
    assert the_run(conn, run1)["jobs_new"] == 3  # an earlier run's counters are not disturbed


def test_finish_run_is_idempotent(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    assert finish_run(conn, run_id, "SUCCEEDED", errors=2, now=at(2)) is True
    snapshot = dict(the_run(conn, run_id))

    assert finish_run(conn, run_id, "SUCCEEDED", errors=5, now=at(9)) is False
    assert finish_run(conn, run_id, "FAILED", error_message="late", now=at(9)) is False

    assert dict(the_run(conn, run_id)) == snapshot
    assert the_job(conn)["last_successful_run_id"] == run_id


def test_cannot_store_into_finished_or_unknown_run(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    finish_run(conn, run_id, "FAILED", now=at(1))
    with pytest.raises(ValueError, match="not RUNNING"):
        upsert_job(conn, make_record(), run_id, now=at(2))
    with pytest.raises(ValueError, match="unknown"):
        upsert_job(conn, make_record(), 999, now=at(2))
    with pytest.raises(ValueError, match="unknown"):
        finish_run(conn, 999, "SUCCEEDED", now=at(2))
    assert query(conn, "SELECT * FROM jobs") == []


def test_argument_validation(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    with pytest.raises(ValueError, match="status"):
        finish_run(conn, run_id, "RUNNING", now=at(1))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        finish_run(conn, run_id, "FAILED", errors=-1, now=at(1))
    with pytest.raises(ValueError, match="timezone-aware"):
        upsert_job(conn, make_record(), run_id, now=datetime(2026, 1, 15, 12, 5))
    with pytest.raises(ValueError, match="precedes"):
        upsert_job(conn, make_record(), run_id, now=T0 - timedelta(minutes=1))
    assert the_run(conn, run_id)["status"] == "RUNNING"


# --- transactions --------------------------------------------------------------------------

_FAIL_ON_JOB_UPDATE = """
    CREATE TRIGGER fail_job_update BEFORE UPDATE ON jobs
    BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
"""


def test_upsert_rolls_back_on_database_error(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(salary_text="10"), run_id, now=at(1))
    before = dict(the_job(conn))
    conn.execute(_FAIL_ON_JOB_UPDATE)

    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        upsert_job(conn, make_record(salary_text="99"), later_run(conn, 1), now=at(2))

    assert not conn.in_transaction
    assert dict(the_job(conn)) == before


def test_finish_run_is_atomic(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    conn.execute(_FAIL_ON_JOB_UPDATE)  # the job-reference UPDATE inside finish_run will fail

    with pytest.raises(sqlite3.IntegrityError):
        finish_run(conn, run_id, "SUCCEEDED", now=at(2))

    assert not conn.in_transaction
    run = the_run(conn, run_id)
    assert (run["status"], run["finished_at"], run["jobs_seen"]) == ("RUNNING", None, 0)
    assert the_job(conn)["last_successful_run_id"] is None

    conn.execute("DROP TRIGGER fail_job_update")  # the retry then succeeds exactly once
    assert finish_run(conn, run_id, "SUCCEEDED", now=at(3)) is True
    assert the_run(conn, run_id)["jobs_seen"] == 1


def test_writes_join_a_callers_transaction(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    conn.execute("BEGIN")
    upsert_job(conn, make_record(), run_id, now=at(1))
    assert conn.in_transaction  # not committed behind the caller's back
    conn.execute("ROLLBACK")
    assert query(conn, "SELECT * FROM jobs") == []


def test_data_survives_reconnect(db_path: Path) -> None:
    first = connect(db_path)
    run_id = start_run(first, now=T0)
    upsert_job(first, make_record(), run_id, now=at(1))
    finish_run(first, run_id, "SUCCEEDED", now=at(2))
    first.close()

    second = connect(db_path)
    assert the_job(second)["last_successful_run_id"] == run_id
    second.close()


# --- posted-date precedence ----------------------------------------------------------------

D1, D2 = "2026-01-10", "2026-01-12"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # known date is never erased by NULL, whatever the sources
        ((D1, "jsonld"), (None, "unknown"), (D1, "jsonld", False)),
        ((D1, "page_text"), (None, "unknown"), (D1, "page_text", False)),
        ((D1, "unknown"), (None, "unknown"), (D1, "unknown", False)),
        # nothing known yet: take the incoming date and its source
        ((None, "unknown"), (D1, "page_text"), (D1, "page_text", False)),
        ((None, "unknown"), (D1, "jsonld"), (D1, "jsonld", False)),
        ((None, "unknown"), (None, "unknown"), (None, "unknown", False)),
        # higher confidence replaces lower
        ((D1, "page_text"), (D2, "jsonld"), (D2, "jsonld", False)),
        ((D1, "page_text"), (D2, "platform"), (D2, "platform", False)),
        ((D1, "unknown"), (D2, "page_text"), (D2, "page_text", False)),
        ((D1, "unknown"), (D2, "platform"), (D2, "platform", False)),
        # lower confidence never replaces higher (and is not reported as a conflict)
        ((D1, "jsonld"), (D2, "page_text"), (D1, "jsonld", False)),
        ((D1, "platform"), (D2, "page_text"), (D1, "platform", False)),
        ((D1, "page_text"), (D2, "unknown"), (D1, "page_text", False)),
        ((D1, "jsonld"), (D2, "unknown"), (D1, "jsonld", False)),
        # equal confidence, different dates: keep the existing value and report the conflict
        ((D1, "jsonld"), (D2, "jsonld"), (D1, "jsonld", True)),
        ((D1, "jsonld"), (D2, "platform"), (D1, "jsonld", True)),  # platform == jsonld
        ((D1, "platform"), (D2, "jsonld"), (D1, "platform", True)),
        ((D1, "page_text"), (D2, "page_text"), (D1, "page_text", True)),
        ((D1, "unknown"), (D2, "unknown"), (D1, "unknown", True)),
        # equal confidence, same date: nothing to report, existing source kept
        ((D1, "jsonld"), (D1, "platform"), (D1, "jsonld", False)),
        ((D1, "page_text"), (D1, "page_text"), (D1, "page_text", False)),
    ],
)
def test_resolve_posted_date(
    old: tuple[str | None, str], new: tuple[str | None, str], expected: tuple[str | None, str, bool]
) -> None:
    date, source, conflict = resolve_posted_date(old[0], old[1], new[0], new[1])
    assert (date, source, conflict is not None) == expected


def test_conflict_describes_both_values() -> None:
    _, _, conflict = resolve_posted_date(D1, "jsonld", D2, "platform")
    assert conflict == PostedDateConflict(D1, "jsonld", D2, "platform")


def posted(conn: sqlite3.Connection, minute: int, date: str | None, source: str) -> UpsertResult:
    """One sighting of the same job, in its own run (a same-run repeat would be a no-op)."""
    extra = {"posted_date": date, "posted_date_source": source} if date else {}
    run_id = start_run(conn, now=at(minute))
    return upsert_job(conn, make_record(**extra), run_id, now=at(minute))


def test_store_keeps_higher_confidence_date(conn: sqlite3.Connection) -> None:
    posted(conn, 1, D1, "jsonld")
    result = posted(conn, 2, D2, "page_text")
    job = the_job(conn)
    assert (job["posted_date"], job["posted_date_source"]) == (D1, "jsonld")
    assert result.posted_date_conflict is None


def test_store_upgrades_to_higher_confidence_date(conn: sqlite3.Connection) -> None:
    posted(conn, 1, D1, "page_text")
    posted(conn, 2, D2, "platform")
    job = the_job(conn)
    assert (job["posted_date"], job["posted_date_source"]) == (D2, "platform")


def test_store_reports_equal_confidence_conflict_and_keeps_existing(
    conn: sqlite3.Connection,
) -> None:
    first = posted(conn, 1, D1, "jsonld")
    result = posted(conn, 2, D2, "platform")
    assert first.posted_date_conflict is None
    assert result.posted_date_conflict == PostedDateConflict(D1, "jsonld", D2, "platform")
    job = the_job(conn)
    assert (job["posted_date"], job["posted_date_source"]) == (D1, "jsonld")


def test_store_null_incoming_keeps_date_and_reports_nothing(conn: sqlite3.Connection) -> None:
    posted(conn, 1, D1, "page_text")
    result = posted(conn, 2, None, "unknown")
    job = the_job(conn)
    assert (job["posted_date"], job["posted_date_source"]) == (D1, "page_text")
    assert result.posted_date_conflict is None


def test_store_fills_unknown_date_later(conn: sqlite3.Connection) -> None:
    posted(conn, 1, None, "unknown")
    posted(conn, 2, D1, "page_text")
    job = the_job(conn)
    assert (job["posted_date"], job["posted_date_source"]) == (D1, "page_text")


# --- schema v2: observations table and migration -------------------------------------------


def raw_rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in query(conn, sql)]


def test_fresh_database_initializes_directly_at_v2(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    init_db(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
    (sql,) = raw.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'run_job_observations'"
    ).fetchone()
    assert "WITHOUT ROWID" in sql
    columns = [r[1] for r in raw.execute("PRAGMA table_info(run_job_observations)")]
    assert columns == ["run_id", "job_id", "outcome", "observed_at"]
    raw.close()


@pytest.fixture
def v1_db(tmp_path: Path) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]]]:
    """A realistic version-1 database: real data written by the store, observations table
    removed and user_version set back to 1. Returns (path, jobs snapshot, runs snapshot)."""
    path = tmp_path / "v1.db"
    init_db(path)
    c = connect(path)
    run1 = start_run(c, now=T0)
    upsert_job(c, make_record(apply_url="https://a.example/A", salary_text="10"), run1, now=at(1))
    upsert_job(c, make_record(apply_url="https://a.example/B"), run1, now=at(1))
    finish_run(c, run1, "SUCCEEDED", errors=1, error_message="one bad row", now=at(2))
    run2 = start_run(c, now=at(60))
    upsert_job(c, make_record(apply_url="https://a.example/A", salary_text="12"), run2, now=at(61))
    finish_run(c, run2, "SUCCEEDED", now=at(62))
    run3 = start_run(c, now=at(120))
    finish_run(c, run3, "FAILED", error_message="site down", now=at(121))
    c.execute("DROP TABLE run_job_observations")
    c.execute("PRAGMA user_version = 1")
    jobs = raw_rows(c, "SELECT * FROM jobs ORDER BY id")
    runs = raw_rows(c, "SELECT * FROM crawl_runs ORDER BY id")
    c.close()
    return path, jobs, runs


def test_v1_database_migrates_to_v2(v1_db: tuple[Path, Any, Any]) -> None:
    path, _, _ = v1_db
    with pytest.raises(RuntimeError, match="init_db"):
        connect(path)  # connect() requires v2

    init_db(path)

    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert raw_rows(conn, "SELECT * FROM run_job_observations") == []  # nothing reconstructed
    conn.close()


def test_migration_preserves_existing_records_and_historical_counters(
    v1_db: tuple[Path, list[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    path, jobs_before, runs_before = v1_db
    init_db(path)
    conn = connect(path)
    assert raw_rows(conn, "SELECT * FROM jobs ORDER BY id") == jobs_before
    runs_after = raw_rows(conn, "SELECT * FROM crawl_runs ORDER BY id")
    assert runs_after == runs_before
    counters = [(r["jobs_seen"], r["jobs_new"], r["jobs_updated"], r["errors"]) for r in runs_after]
    assert counters == [(2, 2, 0, 1), (1, 0, 1, 0), (0, 0, 0, 0)]
    assert {j["last_successful_run_id"] for j in jobs_before} == {1, 2}
    conn.close()


def test_migrated_database_is_usable_and_migration_is_repeatable(
    v1_db: tuple[Path, list[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    path, jobs_before, _ = v1_db
    init_db(path)
    init_db(path)  # already v2: no-op
    conn = connect(path)
    run_id = start_run(conn, now=at(200))
    result = upsert_job(conn, make_record(apply_url="https://a.example/B"), run_id, now=at(201))
    assert result.outcome is UpsertOutcome.UNCHANGED
    finish_run(conn, run_id, "SUCCEEDED", now=at(202))
    assert the_run(conn, run_id)["jobs_seen"] == 1
    assert len(raw_rows(conn, "SELECT * FROM jobs")) == len(jobs_before)
    conn.close()


def test_running_run_blocks_migration(
    v1_db: tuple[Path, list[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    path, jobs_before, runs_before = v1_db
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO crawl_runs (started_at, status) VALUES ('2026-01-15T14:00:00Z', 'RUNNING')"
    )
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match=r"RUNNING.*\[4\]|\[4\].*RUNNING"):
        init_db(path)

    # Nothing changed: still v1, no new table, the run was not silently failed, data intact.
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "run_job_observations" not in tables
    assert raw_rows(raw, "SELECT status, finished_at FROM crawl_runs WHERE id = 4") == [
        {"status": "RUNNING", "finished_at": None}
    ]
    assert raw_rows(raw, "SELECT * FROM jobs ORDER BY id") == jobs_before
    assert raw_rows(raw, "SELECT * FROM crawl_runs WHERE id <= 3 ORDER BY id") == runs_before
    # Once the operator resolves the run, migration succeeds.
    raw.execute("UPDATE crawl_runs SET status = 'FAILED', finished_at = 'x' WHERE id = 4")
    raw.commit()
    raw.close()
    init_db(path)
    connect(path).close()


@pytest.mark.parametrize("version", [3, 99])
def test_unsupported_future_version_rejected_and_untouched(tmp_path: Path, version: int) -> None:
    path = tmp_path / "future.db"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {version}")
    raw.close()
    with pytest.raises(RuntimeError, match="unsupported"):
        init_db(path)
    with pytest.raises(RuntimeError, match="newer"):
        connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == version
    assert raw.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    raw.close()


# --- observations --------------------------------------------------------------------------


def test_observation_recorded_once_per_job_per_run(conn: sqlite3.Connection) -> None:
    run1 = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run1, now=at(1))
    job_id = the_job(conn)["id"]
    assert raw_rows(conn, "SELECT * FROM run_job_observations") == [
        {"run_id": run1, "job_id": job_id, "outcome": "NEW", "observed_at": stamp(1)}
    ]

    run2 = later_run(conn, 10)
    upsert_job(conn, make_record(salary_text="12"), run2, now=at(11))
    obs = raw_rows(conn, "SELECT * FROM run_job_observations ORDER BY run_id")
    assert [(o["run_id"], o["outcome"], o["observed_at"]) for o in obs] == [
        (run1, "NEW", stamp(1)),
        (run2, "UPDATED", stamp(11)),
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO run_job_observations VALUES (999, {job}, 'NEW', 'x')",  # unknown run
        "INSERT INTO run_job_observations VALUES ({run}, 999, 'NEW', 'x')",  # unknown job
        "INSERT INTO run_job_observations VALUES ({run}, {job}, 'DUPLICATE', 'x')",  # not storable
        "INSERT INTO run_job_observations VALUES ({run}, {job}, NULL, 'x')",
        "INSERT INTO run_job_observations VALUES ({run}, {job}, 'NEW', 'x')",  # (run, job) exists
    ],
)
def test_observation_constraints(conn: sqlite3.Connection, sql: str) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql.format(run=run_id, job=the_job(conn)["id"]))


# --- same-run duplicates are a no-op -------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"salary_text": "99 LPA"},
        {"location": "Elsewhere", "description_snippet": "new text"},
        {"posted_date": "2026-01-01", "posted_date_source": "jsonld"},
        {"collected_at": "2026-03-01T00:00:00Z", "source_url": "https://other.example.com/c"},
        {},
    ],
)
def test_same_run_duplicate_is_a_complete_noop(
    conn: sqlite3.Connection, changes: dict[str, Any]
) -> None:
    run_id = start_run(conn, now=T0)
    original = make_record(posted_date="2026-01-10", posted_date_source="platform")
    assert upsert_job(conn, original, run_id, now=at(1)).outcome is UpsertOutcome.NEW
    job_before = dict(the_job(conn))
    observations_before = raw_rows(conn, "SELECT * FROM run_job_observations")

    duplicate = make_record(
        **{"posted_date": "2026-01-10", "posted_date_source": "platform"} | changes
    )
    result = upsert_job(conn, duplicate, run_id, now=at(30))

    assert result.outcome is UpsertOutcome.DUPLICATE
    assert result.posted_date_conflict is None
    job = the_job(conn)
    assert dict(job) == job_before  # nothing in the job row changed...
    assert job["last_seen_at"] == stamp(1) and job["updated_at"] == stamp(1)  # timestamps
    assert job["content_fingerprint"] == job_before["content_fingerprint"]  # fingerprint
    assert (job["posted_date"], job["posted_date_source"]) == ("2026-01-10", "platform")
    assert raw_rows(conn, "SELECT * FROM run_job_observations") == observations_before


def test_same_run_duplicate_does_not_inflate_counters(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    for minute in range(1, 6):
        upsert_job(conn, make_record(salary_text=str(minute)), run_id, now=at(minute))
    finish_run(conn, run_id, "SUCCEEDED", now=at(10))
    run = the_run(conn, run_id)
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"]) == (1, 1, 0)
    assert the_job(conn)["salary_text"] == "1"  # first accepted record wins


def test_first_observation_outcome_is_preserved(conn: sqlite3.Connection) -> None:
    run1 = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run1, now=at(1))
    finish_run(conn, run1, "SUCCEEDED", now=at(2))

    run2 = later_run(conn, 10)
    first = upsert_job(conn, make_record(), run2, now=at(11))  # identical: UNCHANGED
    later = upsert_job(conn, make_record(salary_text="12"), run2, now=at(12))  # would be UPDATED
    assert (first.outcome, later.outcome) == (UpsertOutcome.UNCHANGED, UpsertOutcome.DUPLICATE)
    finish_run(conn, run2, "SUCCEEDED", now=at(13))

    (obs,) = raw_rows(conn, f"SELECT * FROM run_job_observations WHERE run_id = {run2}")
    assert obs["outcome"] == "UNCHANGED"
    run = the_run(conn, run2)
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"]) == (1, 0, 0)
    assert the_job(conn)["salary_text"] is None


# --- atomicity -----------------------------------------------------------------------------

_FAIL_ON_OBSERVATION = """
    CREATE TRIGGER fail_observation BEFORE INSERT ON run_job_observations
    BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
"""


def test_new_job_and_observation_are_atomic(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    conn.execute(_FAIL_ON_OBSERVATION)

    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        upsert_job(conn, make_record(), run_id, now=at(1))

    assert not conn.in_transaction
    assert query(conn, "SELECT * FROM jobs") == []  # the job insert was rolled back too
    assert query(conn, "SELECT * FROM run_job_observations") == []

    conn.execute("DROP TRIGGER fail_observation")  # a retry then succeeds cleanly
    assert upsert_job(conn, make_record(), run_id, now=at(2)).outcome is UpsertOutcome.NEW


def test_resighting_and_observation_are_atomic(conn: sqlite3.Connection) -> None:
    run1 = start_run(conn, now=T0)
    upsert_job(conn, make_record(salary_text="10"), run1, now=at(1))
    finish_run(conn, run1, "SUCCEEDED", now=at(2))
    job_before = dict(the_job(conn))
    run2 = later_run(conn, 10)
    conn.execute(_FAIL_ON_OBSERVATION)

    with pytest.raises(sqlite3.IntegrityError):
        upsert_job(conn, make_record(salary_text="99"), run2, now=at(11))

    assert dict(the_job(conn)) == job_before  # the job UPDATE was rolled back
    assert len(query(conn, "SELECT * FROM run_job_observations")) == 1  # only run1's


# --- observation-based counters and references ----------------------------------------------


def test_historical_counters_unchanged_after_later_runs(conn: sqlite3.Connection) -> None:
    run1 = start_run(conn, now=T0)
    upsert_job(conn, make_record(apply_url="https://a.example/1"), run1, now=at(1))
    upsert_job(conn, make_record(apply_url="https://a.example/2"), run1, now=at(1))
    finish_run(conn, run1, "SUCCEEDED", errors=1, now=at(2))
    snapshot = dict(the_run(conn, run1))

    run2 = later_run(conn, 60)
    upsert_job(
        conn, make_record(apply_url="https://a.example/1", salary_text="9"), run2, now=at(61)
    )
    upsert_job(conn, make_record(apply_url="https://a.example/3"), run2, now=at(61))
    finish_run(conn, run2, "SUCCEEDED", now=at(62))
    run3 = later_run(conn, 120)
    upsert_job(conn, make_record(apply_url="https://a.example/2"), run3, now=at(121))
    finish_run(conn, run3, "FAILED", now=at(122))

    assert dict(the_run(conn, run1)) == snapshot
    assert (snapshot["jobs_seen"], snapshot["jobs_new"], snapshot["jobs_updated"]) == (2, 2, 0)
    run = the_run(conn, run2)
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"]) == (2, 1, 1)
    run = the_run(conn, run3)
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"]) == (1, 0, 0)


def test_overlapping_runs_are_accounted_independently(conn: sqlite3.Connection) -> None:
    """Membership comes from observations, not timestamps, so overlap cannot mix runs up."""
    run1 = start_run(conn, now=T0)
    run2 = start_run(conn, now=at(10))
    upsert_job(conn, make_record(apply_url="https://a.example/X"), run2, now=at(11))
    upsert_job(
        conn, make_record(apply_url="https://a.example/Y"), run1, now=at(12)
    )  # after run2 began

    finish_run(conn, run1, "SUCCEEDED", now=at(13))
    refs = {r["apply_url"]: r["last_successful_run_id"] for r in query(conn, "SELECT * FROM jobs")}
    assert refs == {"https://a.example/X": None, "https://a.example/Y": run1}
    assert the_run(conn, run1)["jobs_seen"] == 1

    finish_run(conn, run2, "SUCCEEDED", now=at(14))
    refs = {r["apply_url"]: r["last_successful_run_id"] for r in query(conn, "SELECT * FROM jobs")}
    assert refs == {"https://a.example/X": run2, "https://a.example/Y": run1}
    assert the_run(conn, run2)["jobs_seen"] == 1


def test_failed_run_never_sets_a_reference_even_for_new_jobs(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    finish_run(conn, run_id, "FAILED", now=at(2))
    job = the_job(conn)
    assert job["last_successful_run_id"] is None
    assert job["status"] == "OPEN"
    assert the_run(conn, run_id)["jobs_seen"] == 1  # the sighting itself is still counted


def test_repeated_finalization_leaves_observation_counters_stable(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, now=T0)
    upsert_job(conn, make_record(), run_id, now=at(1))
    assert finish_run(conn, run_id, "SUCCEEDED", now=at(2)) is True
    snapshot = (dict(the_run(conn, run_id)), dict(the_job(conn)))
    observations = raw_rows(conn, "SELECT * FROM run_job_observations")

    for _ in range(3):
        assert finish_run(conn, run_id, "SUCCEEDED", now=at(9)) is False

    assert (dict(the_run(conn, run_id)), dict(the_job(conn))) == snapshot
    assert raw_rows(conn, "SELECT * FROM run_job_observations") == observations
