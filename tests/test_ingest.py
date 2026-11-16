"""Crawl results -> dedupe -> date window -> SQLite. Offline: scripted results, temporary
databases. The Scrapy-to-SQLite end to end test runs the real engine with a scripted download
handler in a subprocess (see ``tests/crawl_harness.py``); nothing is fetched."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from collector.adapters.common import Extraction
from collector.adapters.greenhouse import extract_greenhouse_jobs
from collector.cli import app
from collector.models import JobRecord
from collector.pipelines.ingest import CollectionError, run_collection
from collector.pipelines.store import finish_run, init_db
from collector.spiders.platforms import FetchFailure
from collector.spiders.run import SourceResult
from collector.spiders.sources import Source
from tests.conftest import make_record

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # "today" in Asia/Kolkata: 2026-01-15
ELIGIBLE = "2026-01-10"
OLD = "2025-11-01"  # far outside the 30-day window
FUTURE = "2026-03-01"  # beyond the one-day tolerance
TODAY = datetime.now(UTC).date().isoformat()  # the CLI reads the real clock
TOLERATED = "2026-01-16"  # one day ahead: still eligible, with a warning


def at(days: int = 0) -> datetime:
    return NOW + timedelta(days=days)


def job(n: int, posted: str | None = ELIGIBLE, **overrides: Any) -> JobRecord:
    fields: dict[str, Any] = {
        "title": f"Engineer {n}",
        "apply_url": f"https://jobs.example.com/apply/{n}",
        "external_id": f"ext-{n}",
        "posted_date": posted,
        "posted_date_source": "platform" if posted else "unknown",
    }
    fields.update(overrides)
    return make_record(**fields)


def src(name: str = "a", platform: str = "greenhouse") -> Source:
    return Source(platform, f"Company {name}", name)


def ok(source: Source, *records: JobRecord) -> SourceResult:
    listing = Extraction(tuple(records), (), "listing", len(records))
    return SourceResult(source, (listing,), ())


def failed(source: Source, reason: str = "HTTP 503", *, blocked: bool = False) -> SourceResult:
    failure = FetchFailure("https://api.example.com/x", reason, blocked)
    return SourceResult(source, (), (failure,))


def collect(db: Path, results: list[SourceResult], when: datetime = NOW) -> Any:
    return run_collection(db, [r.source for r in results], lambda _sources: results, now=when)


def rows(db: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def run_row(db: Path, run_id: int) -> sqlite3.Row:
    (row,) = rows(db, "SELECT * FROM crawl_runs WHERE id = ?", (run_id,))
    return row


def no_run_left_running(db: Path) -> None:
    assert rows(db, "SELECT id FROM crawl_runs WHERE status = 'RUNNING'") == []


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "collector.db"


# --- storing, outcomes and counters --------------------------------------------------------------


def test_successful_extraction_is_stored_and_the_run_is_finalized(db: Path) -> None:
    summary = collect(db, [ok(src(), job(1), job(2))])

    assert summary.status == "SUCCEEDED"
    assert (summary.jobs_seen, summary.jobs_new, summary.jobs_updated) == (2, 2, 0)
    assert [r["title"] for r in rows(db, "SELECT title FROM jobs ORDER BY id")] == [
        "Engineer 1",
        "Engineer 2",
    ]
    run = run_row(db, summary.run_id)
    assert run["status"] == "SUCCEEDED" and run["finished_at"] is not None
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"], run["errors"]) == (2, 2, 0, 0)
    observations = rows(db, "SELECT outcome FROM run_job_observations WHERE run_id = ?", (1,))
    assert [o["outcome"] for o in observations] == ["NEW", "NEW"]
    assert all(j["last_successful_run_id"] == 1 for j in rows(db, "SELECT * FROM jobs"))
    assert rows(db, "PRAGMA user_version")[0][0] == 2
    no_run_left_running(db)


def test_later_runs_report_unchanged_and_updated_without_touching_earlier_runs(db: Path) -> None:
    first = collect(db, [ok(src(), job(1), job(2))])
    second = collect(db, [ok(src(), job(1), job(2, title="Senior Engineer 2"))], at(1))

    assert (second.jobs_seen, second.jobs_new, second.jobs_updated) == (2, 0, 1)
    assert second.jobs_unchanged == 1
    outcomes = {
        r["title"]: r["outcome"]
        for r in rows(
            db,
            "SELECT j.title, o.outcome FROM run_job_observations o "
            "JOIN jobs j ON j.id = o.job_id WHERE o.run_id = ?",
            (second.run_id,),
        )
    }
    assert outcomes == {"Engineer 1": "UNCHANGED", "Senior Engineer 2": "UPDATED"}
    assert len(rows(db, "SELECT id FROM jobs")) == 2  # re-sightings, not new rows
    # The first run's counters are history and never change.
    old_run = run_row(db, first.run_id)
    assert (old_run["jobs_seen"], old_run["jobs_new"], old_run["jobs_updated"]) == (2, 2, 0)
    assert {j["last_successful_run_id"] for j in rows(db, "SELECT * FROM jobs")} == {2}


def test_same_run_duplicates_across_sources_first_record_wins(db: Path) -> None:
    first = job(1, title="From the first source")
    second = job(1, title="From the second source")  # same identity (external id)

    summary = collect(db, [ok(src("a"), first), ok(src("b", "lever"), second, job(2))])

    assert (summary.unique, summary.duplicates) == (2, 1)
    assert (summary.jobs_seen, summary.jobs_new) == (2, 2)  # the duplicate counted nowhere
    titles = {r["title"] for r in rows(db, "SELECT title FROM jobs")}
    assert titles == {"From the first source", "Engineer 2"}


def test_failed_run_never_double_counts_when_finalization_is_repeated(db: Path) -> None:
    summary = collect(db, [ok(src(), job(1))])
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        assert finish_run(conn, summary.run_id, "SUCCEEDED") is False  # already finished
        assert finish_run(conn, summary.run_id, "FAILED") is False
    finally:
        conn.close()
    run = run_row(db, summary.run_id)
    assert (run["status"], run["jobs_seen"], run["jobs_new"]) == ("SUCCEEDED", 1, 1)


# --- date window ---------------------------------------------------------------------------------


def test_date_window_rules_decide_what_is_stored(db: Path) -> None:
    records = [
        job(1, ELIGIBLE),
        job(2, None),  # unknown date
        job(3, OLD),
        job(4, FUTURE),
        job(5, TOLERATED),
    ]

    summary = collect(db, [ok(src(), *records)])

    assert (summary.eligible, summary.unknown_date, summary.old_date, summary.future_date) == (
        2,
        1,
        1,
        1,
    )
    stored = {
        r["external_id"]: r
        for r in rows(db, "SELECT external_id, posted_date, posted_date_source FROM jobs")
    }
    assert set(stored) == {"ext-1", "ext-2", "ext-4", "ext-5"}  # the old one is excluded
    assert stored["ext-2"]["posted_date"] is None  # a missing date is never invented
    assert stored["ext-2"]["posted_date_source"] == "unknown"
    assert stored["ext-4"]["posted_date"] == FUTURE  # kept, flagged in the report
    assert summary.jobs_seen == 4
    assert rows(db, "SELECT COUNT(*) FROM run_job_observations")[0][0] == 4


def test_an_old_posting_is_not_stored_and_does_not_touch_a_stored_job(db: Path) -> None:
    collect(db, [ok(src(), job(1, ELIGIBLE))])
    summary = collect(db, [ok(src(), job(1, OLD))], at(1))  # same job, now dated old

    assert summary.old_date == 1 and summary.jobs_seen == 0
    (stored,) = rows(db, "SELECT posted_date, last_successful_run_id FROM jobs")
    assert stored["posted_date"] == ELIGIBLE and stored["last_successful_run_id"] == 1


# --- invalid jobs, failed and blocked sources ---


def test_invalid_jobs_from_the_adapter_are_reported_and_never_stored(db: Path) -> None:
    payload = json.loads((FIXTURES / "greenhouse/list_invalid_incomplete.json").read_text("utf-8"))
    extraction = extract_greenhouse_jobs(
        payload,
        company_name="Example Corp",
        board_token="b",
        source_url="https://boards-api.greenhouse.io/v1/boards/b/jobs",
        collected_at=NOW,
    )
    assert extraction.failures and not extraction.records
    source = src("b")

    summary = collect(db, [SourceResult(source, (extraction,), ()), ok(src("c"), job(1))])

    assert summary.extraction_failures == len(extraction.failures) == summary.errors
    assert summary.status == "SUCCEEDED" and summary.sources_failed == 0  # recognized listing
    assert [r["external_id"] for r in rows(db, "SELECT external_id FROM jobs")] == ["ext-1"]
    assert run_row(db, summary.run_id)["errors"] == len(extraction.failures)


def test_failed_and_blocked_sources_are_isolated_and_reported_by_count(db: Path) -> None:
    results = [
        ok(src("a"), job(1)),
        failed(src("b", "lever"), "HTTP 503"),
        failed(src("c", "ashby"), "HTTP 403", blocked=True),
        ok(src("d"), job(2)),
    ]

    summary = collect(db, results)

    assert summary.status == "FAILED"  # any failed or blocked source fails the run
    assert (summary.sources, summary.sources_failed, summary.sources_blocked) == (4, 2, 1)
    assert summary.errors == 2
    assert len(rows(db, "SELECT id FROM jobs")) == 2  # healthy sources' jobs were stored
    run = run_row(db, summary.run_id)
    assert run["status"] == "FAILED" and run["errors"] == 2
    assert run["error_message"] == (
        "2 of 4 sources failed (1 blocked); 2 fetch failures, 0 extraction failures"
    )
    assert "example.com" not in run["error_message"]  # counts only: no URLs, no names
    no_run_left_running(db)


def test_a_blocked_source_is_not_an_empty_successful_run(db: Path) -> None:
    summary = collect(db, [failed(src(), "HTTP 429", blocked=True)])

    assert summary.status == "FAILED" and summary.sources_blocked == 1
    run = run_row(db, summary.run_id)
    assert run["status"] == "FAILED" and run["finished_at"] is not None
    assert (run["jobs_seen"], run["errors"]) == (0, 1)


def test_an_unrecognized_response_is_a_failed_source_but_an_empty_listing_is_not(db: Path) -> None:
    from collector.adapters.common import rejected_response

    unrecognized = SourceResult(src("a"), (rejected_response("not a listing"),), ())
    empty = ok(src("b"))

    both = collect(db, [unrecognized, empty])
    only_unrecognized = collect(db, [unrecognized], at(1))

    assert (both.status, both.sources_failed) == ("FAILED", 1)
    assert only_unrecognized.status == "FAILED"


def test_a_failed_crawl_deletes_nothing_and_advances_no_reference(db: Path) -> None:
    collect(db, [ok(src(), job(1), job(2), job(3))])
    collect(db, [failed(src(), "HTTP 503")], at(1))  # whole crawl failed
    collect(db, [ok(src(), job(1))], at(2))  # a later run only sees one job

    jobs = {r["external_id"]: r for r in rows(db, "SELECT * FROM jobs")}
    assert set(jobs) == {"ext-1", "ext-2", "ext-3"}
    assert {j["status"] for j in jobs.values()} == {"OPEN"}  # nothing is closed or removed
    assert jobs["ext-1"]["last_successful_run_id"] == 3
    assert jobs["ext-2"]["last_successful_run_id"] == 1  # unseen jobs keep their reference
    assert [r["status"] for r in rows(db, "SELECT status FROM crawl_runs ORDER BY id")] == [
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
    ]
    no_run_left_running(db)


# --- run status: SUCCEEDED only when every source succeeded ---


def with_failures(source: Source, *records: JobRecord, failures: int = 0, warnings: int = 0) -> Any:
    """A recognized listing in which some jobs were rejected and/or carry warnings."""
    from collector.adapters.common import (
        ExtractionFailure,
        ExtractionWarning,
        FailureKind,
    )
    from collector.pipelines.validate import FieldError

    error = FieldError("title", "missing", "title is missing")
    rejected = tuple(
        ExtractionFailure(FailureKind.MISSING_FIELD, 100 + i, None, (error,))
        for i in range(failures)
    )
    warned = tuple(
        ExtractionWarning(FailureKind.INVALID_DATE, 0, None, "updated_at", "bad date; ignored")
        for _ in range(warnings)
    )
    listing = Extraction(
        tuple(records), rejected, "listing", len(records) + failures, warnings=warned
    )
    return SourceResult(source, (listing,), ())


def unusable(source: Source) -> SourceResult:
    from collector.adapters.common import rejected_response

    return SourceResult(source, (rejected_response("not a listing"),), ())


def test_every_source_succeeding_is_a_succeeded_run(db: Path) -> None:
    summary = collect(db, [ok(src("a"), job(1)), ok(src("b", "lever"), job(2)), ok(src("c"))])

    assert summary.status == "SUCCEEDED" and summary.sources_failed == 0 and summary.errors == 0
    run = run_row(db, summary.run_id)
    assert (run["status"], run["errors"], run["error_message"]) == ("SUCCEEDED", 0, None)
    assert {j["last_successful_run_id"] for j in rows(db, "SELECT * FROM jobs")} == {1}


@pytest.mark.parametrize(
    "broken",
    [
        failed(src("x", "lever"), "HTTP 403", blocked=True),  # blocked
        failed(src("x", "lever"), "HTTP 503"),  # failed fetch
        unusable(src("x", "lever")),  # unrecognized / unusable response
    ],
    ids=["blocked", "fetch-failure", "unusable-response"],
)
def test_one_broken_source_fails_the_run_but_keeps_what_the_healthy_sources_found(
    db: Path, broken: SourceResult
) -> None:
    collect(db, [ok(src("a"), job(1))])  # an earlier successful run

    summary = collect(db, [ok(src("a"), job(1), job(2)), broken, ok(src("c"), job(3))], at(1))

    assert summary.status == "FAILED" and summary.sources_failed == 1
    run = run_row(db, summary.run_id)
    assert run["status"] == "FAILED" and run["finished_at"] is not None
    # The healthy sources' jobs are stored, observed and counted accurately...
    assert {r["external_id"] for r in rows(db, "SELECT external_id FROM jobs")} == {
        "ext-1",
        "ext-2",
        "ext-3",
    }
    assert (run["jobs_seen"], run["jobs_new"], run["jobs_updated"]) == (3, 2, 0)
    observed = rows(db, "SELECT outcome FROM run_job_observations WHERE run_id = 2")
    assert sorted(o["outcome"] for o in observed) == ["NEW", "NEW", "UNCHANGED"]
    # ...but a FAILED run advances no successful-run reference, and nothing is closed.
    references = {
        j["external_id"]: j["last_successful_run_id"]
        for j in rows(db, "SELECT external_id, last_successful_run_id FROM jobs")
    }
    assert references == {"ext-1": 1, "ext-2": None, "ext-3": None}
    assert {j["status"] for j in rows(db, "SELECT status FROM jobs")} == {"OPEN"}
    assert run["error_message"].startswith("1 of 3 sources failed")
    no_run_left_running(db)


def test_a_failing_source_never_deletes_jobs_it_previously_supplied(db: Path) -> None:
    collect(db, [ok(src("a"), job(1)), ok(src("b", "lever"), job(2))])

    summary = collect(db, [ok(src("a"), job(1)), failed(src("b", "lever"))], at(1))

    assert summary.status == "FAILED"
    assert {r["external_id"] for r in rows(db, "SELECT external_id FROM jobs")} == {
        "ext-1",
        "ext-2",
    }
    assert {j["status"] for j in rows(db, "SELECT status FROM jobs")} == {"OPEN"}
    assert {
        r["external_id"]: r["last_successful_run_id"]
        for r in rows(db, "SELECT external_id, last_successful_run_id FROM jobs")
    } == {"ext-1": 1, "ext-2": 1}  # both keep the last successful reference


def test_rejected_jobs_and_adapter_warnings_alone_do_not_fail_the_run(db: Path) -> None:
    rejected = with_failures(src("a"), job(1), failures=2)  # a healthy listing, two bad jobs
    warned = with_failures(src("b", "lever"), job(2), warnings=3)  # warnings only

    summary = collect(db, [rejected, warned, ok(src("c"))])  # c: a recognized empty listing

    assert summary.status == "SUCCEEDED" and summary.sources_failed == 0
    assert summary.errors == 2 and summary.extraction_failures == 2  # reported, not fatal
    run = run_row(db, summary.run_id)
    assert (run["status"], run["errors"], run["jobs_seen"]) == ("SUCCEEDED", 2, 2)
    assert run["error_message"] == (
        "0 of 3 sources failed (0 blocked); 0 fetch failures, 2 extraction failures"
    )
    assert {j["last_successful_run_id"] for j in rows(db, "SELECT * FROM jobs")} == {1}


def test_a_recognized_empty_listing_is_a_succeeded_run(db: Path) -> None:
    summary = collect(db, [ok(src())])
    assert (summary.status, summary.jobs_seen, summary.errors) == ("SUCCEEDED", 0, 0)


def test_status_is_decided_by_sources_not_by_the_combined_error_count(db: Path) -> None:
    many_bad_jobs = with_failures(src("a"), job(1), failures=5)
    one_bad_source = failed(src("b", "lever"))

    healthy = collect(db, [many_bad_jobs])  # errors == 5, no source failed
    broken = collect(db, [ok(src("a"), job(2)), one_bad_source], at(1))  # errors == 1

    assert (healthy.errors, healthy.status) == (5, "SUCCEEDED")
    assert (broken.errors, broken.status) == (1, "FAILED")


# --- database and execution failures -------------------------------------------------------------


def test_a_database_write_failure_rolls_back_that_job_and_fails_the_run(db: Path) -> None:
    init_db(db)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TRIGGER fail_second_observation BEFORE INSERT ON run_job_observations
        WHEN (SELECT COUNT(*) FROM run_job_observations WHERE run_id = NEW.run_id) >= 1
        BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END;
        """
    )
    conn.close()

    with pytest.raises(CollectionError) as failure:
        collect(db, [ok(src(), job(1), job(2), job(3))])

    assert failure.value.run_id == 1
    assert str(failure.value) == "IntegrityError during collection"
    assert [r["external_id"] for r in rows(db, "SELECT external_id FROM jobs")] == ["ext-1"]
    assert len(rows(db, "SELECT * FROM run_job_observations")) == 1  # job 2's insert rolled back
    run = run_row(db, 1)
    assert run["status"] == "FAILED" and run["finished_at"] is not None
    assert "simulated" not in (run["error_message"] or "")
    assert rows(db, "SELECT last_successful_run_id FROM jobs")[0][0] is None  # FAILED: no reference
    no_run_left_running(db)


def test_a_crawl_crash_fails_the_run_without_leaking_its_message(db: Path) -> None:
    def crash(_sources: list[Source]) -> list[SourceResult]:
        raise RuntimeError("SECRET-PAYLOAD")

    with pytest.raises(CollectionError) as failure:
        run_collection(db, [src()], crash, now=NOW)

    assert "SECRET-PAYLOAD" not in str(failure.value)
    run = run_row(db, failure.value.run_id)
    assert run["status"] == "FAILED" and "SECRET-PAYLOAD" not in run["error_message"]
    no_run_left_running(db)


def test_an_interrupted_run_is_marked_failed_and_the_interrupt_propagates(db: Path) -> None:
    def interrupt(_sources: list[Source]) -> list[SourceResult]:
        raise KeyboardInterrupt

    collect(db, [ok(src(), job(1))])
    with pytest.raises(KeyboardInterrupt):
        run_collection(db, [src()], interrupt, now=at(1))

    run = run_row(db, 2)
    assert (run["status"], run["error_message"]) == ("FAILED", "interrupted")
    assert len(rows(db, "SELECT id FROM jobs")) == 1  # earlier data untouched
    no_run_left_running(db)


def test_an_existing_database_is_kept_and_an_unsupported_one_is_refused_untouched(
    db: Path, tmp_path: Path
) -> None:
    collect(db, [ok(src(), job(1))])
    collect(db, [ok(src(), job(2))], at(1))
    assert len(rows(db, "SELECT id FROM jobs")) == 2  # the second run added to, not replaced, it

    newer = tmp_path / "newer.db"
    conn = sqlite3.connect(newer)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    before = newer.read_bytes()

    with pytest.raises(CollectionError, match="database unavailable"):
        collect(newer, [ok(src(), job(1))])
    assert newer.read_bytes() == before


def test_the_schema_is_unchanged(db: Path) -> None:
    collect(db, [ok(src(), job(1))])
    tables = {
        r["name"]
        for r in rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")
        if not r["name"].startswith("sqlite_")
    }
    assert tables == {"crawl_runs", "jobs", "run_job_observations"}
    assert rows(db, "PRAGMA user_version")[0][0] == 2


# --- the CLI -------------------------------------------------------------------------------------

runner = CliRunner()
HEADER = "platform,company_name,identifier,company_website\n"


@pytest.fixture
def sources_csv(tmp_path: Path) -> Path:
    path = tmp_path / "sources.csv"
    path.write_text(HEADER + "greenhouse,Company a,a,\n" + "lever,Company b,b,\n", encoding="utf-8")
    return path


def fake_crawl(monkeypatch: pytest.MonkeyPatch, *per_source: list[JobRecord] | str) -> None:
    """Replace the crawl: records for a source, or "blocked" / "failed" for a failing one."""

    def crawl(sources: Any, **_kwargs: Any) -> list[SourceResult]:
        out = []
        for source, outcome in zip(sources, per_source, strict=True):
            if outcome == "blocked":
                out.append(failed(source, "HTTP 403", blocked=True))
            elif outcome == "failed":
                out.append(failed(source, "HTTP 503"))
            else:
                assert isinstance(outcome, list)
                out.append(ok(source, *outcome))
        return out

    monkeypatch.setattr("collector.cli.crawl_sources", crawl)


def test_cli_stores_jobs_in_the_database_and_reports_the_run(
    tmp_path: Path, sources_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "out" / "jobs.db"
    fake_crawl(monkeypatch, [job(1, TODAY)], [job(2, None)])

    result = runner.invoke(app, ["run", "--sources", str(sources_csv), "--db", str(database)])

    assert result.exit_code == 0, result.output
    assert "RUN 1 SUCCEEDED: 2 new, 0 updated, 0 unchanged" in result.output
    assert "TOTAL: 2 unique records" in result.output
    assert len(rows(database, "SELECT id FROM jobs")) == 2
    again = runner.invoke(app, ["run", "--sources", str(sources_csv), "--db", str(database)])
    assert "RUN 2 SUCCEEDED: 0 new, 0 updated, 2 unchanged" in again.output
    assert len(rows(database, "SELECT id FROM jobs")) == 2


def test_cli_database_defaults_to_the_configured_path(
    tmp_path: Path, sources_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured.db"
    monkeypatch.setenv("COLLECTOR_SQLITE_PATH", str(configured))
    fake_crawl(monkeypatch, [job(1, TODAY)], [])

    result = runner.invoke(app, ["run", "--sources", str(sources_csv)])

    assert result.exit_code == 0, result.output
    assert configured.exists() and len(rows(configured, "SELECT id FROM jobs")) == 1


def test_cli_reports_blocked_sources_stores_the_rest_and_exits_nonzero(
    tmp_path: Path, sources_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "jobs.db"
    fake_crawl(monkeypatch, [job(1, TODAY)], "blocked")

    result = runner.invoke(app, ["run", "--sources", str(sources_csv), "--db", str(database)])

    assert result.exit_code == 1
    assert "(BLOCKED)" in result.output and "blocked: HTTP 403" in result.output
    assert "RUN 1 FAILED" in result.output and "1 of 2 sources failed (1 blocked)" in result.output
    assert len(rows(database, "SELECT id FROM jobs")) == 1


def test_cli_marks_the_run_failed_and_exits_1_when_every_source_fails(
    tmp_path: Path, sources_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "jobs.db"
    fake_crawl(monkeypatch, "blocked", "failed")

    result = runner.invoke(app, ["run", "--sources", str(sources_csv), "--db", str(database)])

    assert result.exit_code == 1  # a finished-but-FAILED run: reported, exit non-zero
    assert "RUN 1 FAILED" in result.output
    assert run_row(database, 1)["status"] == "FAILED"


def test_cli_exit_code_policy_for_partial_failures_and_rejected_jobs(
    tmp_path: Path, sources_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial failure: run FAILED, exit 1. Rejected jobs only: run SUCCEEDED, but still exit 1
    (the existing policy: any fetch or extraction failure is a non-zero exit)."""
    partial = tmp_path / "partial.db"
    fake_crawl(monkeypatch, [job(1, TODAY)], "failed")
    result = runner.invoke(app, ["run", "--sources", str(sources_csv), "--db", str(partial)])
    assert result.exit_code == 1 and "RUN 1 FAILED" in result.output
    assert (
        run_row(partial, 1)["status"] == "FAILED" and len(rows(partial, "SELECT * FROM jobs")) == 1
    )

    rejected = tmp_path / "rejected.db"
    monkeypatch.setattr(
        "collector.cli.crawl_sources",
        lambda sources, **kwargs: [
            with_failures(sources[0], job(1, TODAY), failures=1),
            ok(sources[1]),
        ],
    )
    result = runner.invoke(app, ["run", "--sources", str(sources_csv), "--db", str(rejected)])
    assert result.exit_code == 1 and "RUN 1 SUCCEEDED" in result.output
    assert run_row(rejected, 1)["status"] == "SUCCEEDED"


def test_cli_reports_an_unusable_database_clearly_before_crawling(
    tmp_path: Path, sources_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr("collector.cli.crawl_sources", lambda s, **k: calls.append(1) or [])
    not_a_file = tmp_path  # a directory cannot be a database

    result = runner.invoke(app, ["run", "--sources", str(sources_csv), "--db", str(not_a_file)])

    assert result.exit_code == 3
    assert result.output.startswith("error: database unavailable")
    assert calls == []


def test_cli_does_not_touch_the_database_for_an_invalid_sources_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "jobs.db"
    bad = tmp_path / "bad.csv"
    bad.write_text(HEADER + "workday,X,y,\n", encoding="utf-8")

    result = runner.invoke(app, ["run", "--sources", str(bad), "--db", str(database)])

    assert result.exit_code == 2
    assert not database.exists()


def test_cli_run_failure_is_reported_with_the_run_marked_failed(
    tmp_path: Path, sources_csv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "jobs.db"

    def crash(sources: Any, **kwargs: Any) -> list[SourceResult]:
        raise RuntimeError("SECRET-PAYLOAD")

    monkeypatch.setattr("collector.cli.crawl_sources", crash)

    result = runner.invoke(app, ["run", "--sources", str(sources_csv), "--db", str(database)])

    assert result.exit_code == 3
    assert "run 1 marked FAILED" in result.output and "SECRET-PAYLOAD" not in result.output
    assert run_row(database, 1)["status"] == "FAILED"


# --- Scrapy -> SQLite end to end ---


def test_scrapy_crawl_to_sqlite_end_to_end(tmp_path: Path) -> None:
    gh = "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true"
    ab = "https://api.ashbyhq.com/posting-api/job-board/{}"
    lv = "https://api.lever.co/v0/postings/{}?mode=json"
    page = "https://jobs.example.org/careers"
    html = {"headers": {"Content-Type": "text/html"}}
    database = tmp_path / "e2e.db"
    scenario = {
        "db": str(database),
        "now": "2026-10-20T12:00:00+00:00",
        "sources": [
            {"platform": "greenhouse", "company_name": "Co G", "identifier": "gh-ok"},
            {"platform": "lever", "company_name": "Co L", "identifier": "lv-blocked"},
            {"platform": "ashby", "company_name": "Co A", "identifier": "ab-ok"},
            {"platform": "jsonld", "company_name": "Co J", "identifier": page},
            {"platform": "greenhouse", "company_name": "Co B", "identifier": "gh-bad"},
        ],
        "responses": {
            gh.format("gh-ok"): {"fixture": "greenhouse/list_complete.json"},
            lv.format("lv-blocked"): {"status": 403, "body": "SECRET-PAYLOAD"},
            ab.format("ab-ok"): {"fixture": "ashby/board_complete.json"},
            page: {"fixture": "jsonld/multiple_job_postings.html", **html},
            gh.format("gh-bad"): {"body": "{broken SECRET-PAYLOAD"},
        },
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COLLECTOR_")}
    env.update(COLLECTOR_DOWNLOAD_DELAY_SECONDS="0", COLLECTOR_AUTOTHROTTLE_ENABLED="false")

    completed = subprocess.run(
        [sys.executable, "-c", "from tests.crawl_harness import main; main()", str(path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-3000:]
    summary = json.loads(
        next(x for x in completed.stdout.splitlines() if x.startswith("SUMMARY:"))[8:]
    )
    assert summary["status"] == "FAILED"  # a blocked source and an unusable response
    assert (summary["sources"], summary["sources_failed"], summary["sources_blocked"]) == (5, 2, 1)
    # Greenhouse (1, unknown date) + Ashby (1) + two JSON-LD postings, all stored once.
    assert (summary["unique"], summary["unknown_date"], summary["eligible"]) == (4, 1, 3)
    assert (summary["jobs_seen"], summary["jobs_new"], summary["old_date"]) == (4, 4, 0)
    assert len(rows(database, "SELECT id FROM jobs")) == 4
    run = run_row(database, summary["run_id"])
    assert (run["status"], run["errors"]) == ("FAILED", 2)  # one block + one malformed body
    assert "SECRET-PAYLOAD" not in completed.stdout + completed.stderr + str(dict(run))
    no_run_left_running(database)
