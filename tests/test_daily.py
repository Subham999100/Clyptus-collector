"""Daily collection (Phase 9), in process: run lock, business-date naming, outcome and overwrite
policy, recovery of interrupted runs, the stale-candidate report, export failures and the CLI
exit codes. The crawl is a stub returning prepared ``SourceResult``s; the database, storage and
Excel export are the real ones (temporary files only, sockets blocked). The real crawler is
exercised end to end in tests/test_daily_e2e.py."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook
from typer.testing import CliRunner

from collector.adapters.common import Extraction
from collector.cli import app
from collector.export.excel import HEADERS, SHEET_NAME
from collector.models import JobRecord
from collector.pipelines import daily
from collector.pipelines.daily import (
    INTERRUPTED,
    DailyOutcome,
    OutputExists,
    business_date,
    run_daily,
    workbook_path,
)
from collector.pipelines.ingest import CollectionError
from collector.pipelines.store import SCHEMA_VERSION, connect, init_db, start_run
from collector.spiders.platforms import FetchFailure
from collector.spiders.run import SourceResult
from collector.spiders.sources import Source
from collector.utils.lock import LockBusy, database_lock, lock_path
from tests.conftest import make_record

DAY_1 = datetime(2026, 11, 16, 20, 31, tzinfo=UTC)  # 02:01 on 17 Nov in Asia/Kolkata
LEVER = Source("lever", "Example Corp", "example-site")
GREENHOUSE = Source("greenhouse", "Other Corp", "other-board")
runner = CliRunner()


@pytest.fixture(autouse=True)
def _offline(no_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sockets blocked; default settings (business timezone Asia/Kolkata, 30-day window)."""
    import os

    for name in list(os.environ):
        if name.startswith("COLLECTOR_"):
            monkeypatch.delenv(name)


def job(n: int, source: Source = LEVER, **changes: Any) -> JobRecord:
    return make_record(
        title=f"Job {n}",
        company_name=source.company_name,
        platform=source.platform,
        apply_url=f"https://jobs.example.com/{source.identifier}/{n}",
        source_url=f"https://jobs.example.com/{source.identifier}",
        **changes,
    )


def ok(source: Source, *records: JobRecord) -> SourceResult:
    return SourceResult(source, (Extraction(records, (), "listing", len(records)),), ())


def failed(source: Source, reason: str = "DNSLookupError") -> SourceResult:
    return SourceResult(source, (), (FetchFailure("https://jobs.example.com/x", reason),))


def crawl_returning(*results: SourceResult) -> tuple[Callable[..., Any], list[int]]:
    calls: list[int] = []

    def crawl(sources: list[Source]) -> list[SourceResult]:
        calls.append(len(sources))
        return list(results)

    return crawl, calls


def daily_run(tmp_path: Path, *results: SourceResult, now: datetime = DAY_1, **options: Any) -> Any:
    crawl, _ = crawl_returning(*results)
    sources = [result.source for result in results]
    return run_daily(sources, tmp_path / "c.db", tmp_path / "out", crawl, now=now, **options)


def sheet(path: Path) -> list[dict[str, Any]]:
    ws = load_workbook(path)[SHEET_NAME]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == HEADERS  # the existing columns, unchanged
    return [dict(zip(HEADERS, row, strict=True)) for row in rows[1:]]


def query(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(tmp_path / "c.db") as conn:
        return conn.execute(sql).fetchall()


# --- naming and the run lock --------------------------------------------------------------------


def test_the_business_date_is_the_kolkata_calendar_date() -> None:
    assert business_date(datetime(2026, 11, 16, 18, 29, tzinfo=UTC)) == date(2026, 11, 16)
    assert business_date(datetime(2026, 11, 16, 18, 30, tzinfo=UTC)) == date(2026, 11, 17)
    assert workbook_path("exports", date(2026, 11, 17)) == Path("exports/jobs_2026-11-17.xlsx")
    assert workbook_path("exports", date(2026, 11, 17), partial_run=7) == Path(
        "exports/jobs_2026-11-17_partial_run7.xlsx"
    )


def test_the_lock_is_exclusive_and_released_after_use(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "c.db"
    with database_lock(db) as held:
        assert held == lock_path(db) == tmp_path / "sub" / "c.db.lock"
        with pytest.raises(LockBusy, match="another collection run"), database_lock(db):
            pass
    with database_lock(db):  # released: it can be taken again
        pass
    with pytest.raises(RuntimeError), database_lock(db):  # released when the block fails too
        raise RuntimeError("boom")
    with database_lock(db):
        pass


def test_a_second_run_on_a_locked_database_does_nothing(tmp_path: Path) -> None:
    crawl, calls = crawl_returning(ok(LEVER, job(1)))
    with database_lock(tmp_path / "c.db"), pytest.raises(LockBusy):
        run_daily([LEVER], tmp_path / "c.db", tmp_path / "out", crawl, now=DAY_1)
    assert calls == [] and not (tmp_path / "c.db").exists() and not (tmp_path / "out").exists()


# --- a normal day -------------------------------------------------------------------------------


def test_first_day_stores_jobs_and_writes_the_dated_workbook(tmp_path: Path) -> None:
    report = daily_run(tmp_path, ok(LEVER, job(1), job(2)), ok(GREENHOUSE, job(3, GREENHOUSE)))

    assert report.business_date == date(2026, 11, 17)
    assert (report.outcome, report.ingest.status) == (DailyOutcome.COMPLETE, "SUCCEEDED")
    assert (report.ingest.jobs_new, report.ingest.jobs_updated, report.ingest.jobs_unchanged) == (
        3,
        0,
        0,
    )
    assert (report.sources_completed, report.ingest.sources_failed) == (2, 0)
    assert report.started_at == report.finished_at == "2026-11-16T20:31:00.000000Z"
    assert report.recovered_runs == () and report.stale == () and report.export_error is None
    assert report.workbook is not None
    assert report.workbook.path == tmp_path / "out" / "jobs_2026-11-17.xlsx"
    rows = sheet(report.workbook.path)
    assert report.workbook.rows == len(rows) == 3
    assert sorted(r["Job Title"] for r in rows) == ["Job 1", "Job 2", "Job 3"]
    assert {r["Date Status"] for r in rows} == {"Unknown date - review"}  # never invented
    assert query(tmp_path, "PRAGMA user_version") == [(SCHEMA_VERSION,)] == [(2,)]


def test_second_day_sees_the_same_jobs_without_duplicates(tmp_path: Path) -> None:
    daily_run(tmp_path, ok(LEVER, job(1), job(2)))
    report = daily_run(tmp_path, ok(LEVER, job(1), job(2)), now=DAY_1 + timedelta(days=1))

    assert report.ingest.jobs_new == report.ingest.jobs_updated == 0
    assert report.ingest.jobs_unchanged == report.ingest.jobs_seen == 2
    assert report.workbook is not None and report.workbook.path.name == "jobs_2026-11-18.xlsx"
    assert (tmp_path / "out" / "jobs_2026-11-17.xlsx").is_file()  # yesterday's is kept
    assert query(tmp_path, "SELECT COUNT(*), COUNT(DISTINCT job_key) FROM jobs") == [(2, 2)]
    assert query(tmp_path, "SELECT COUNT(*) FROM run_job_observations") == [(4,)]


def test_an_updated_job_is_updated_not_duplicated(tmp_path: Path) -> None:
    daily_run(tmp_path, ok(LEVER, job(1, location="Pune"), job(2)))
    later = DAY_1 + timedelta(days=1)
    report = daily_run(tmp_path, ok(LEVER, job(1, location="Bengaluru"), job(2)), now=later)

    assert (report.ingest.jobs_new, report.ingest.jobs_updated) == (0, 1)
    assert report.ingest.jobs_unchanged == 1
    assert query(tmp_path, "SELECT COUNT(*) FROM jobs") == [(2,)]
    assert report.workbook is not None
    locations = {r["Job Title"]: r["Location"] for r in sheet(report.workbook.path)}
    assert locations["Job 1"] == "Bengaluru"


def test_todays_workbook_is_never_replaced_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "out" / "jobs_2026-11-17.xlsx"
    target.parent.mkdir()
    target.write_bytes(b"previous workbook")
    crawl, calls = crawl_returning(ok(LEVER, job(1)))

    with pytest.raises(OutputExists, match="already exists"):
        run_daily([LEVER], tmp_path / "c.db", tmp_path / "out", crawl, now=DAY_1)
    assert calls == [] and target.read_bytes() == b"previous workbook"
    assert not (tmp_path / "c.db").exists()  # refused before anything was done

    report = daily_run(tmp_path, ok(LEVER, job(1)), overwrite=True)
    assert report.workbook is not None and report.workbook.path == target
    assert [r["Job Title"] for r in sheet(target)] == ["Job 1"]


# --- failures -----------------------------------------------------------------------------------


def test_a_failed_source_gives_a_partial_workbook_and_closes_nothing(tmp_path: Path) -> None:
    daily_run(tmp_path, ok(LEVER, job(1)), ok(GREENHOUSE, job(2, GREENHOUSE)))
    report = daily_run(
        tmp_path, ok(LEVER, job(1)), failed(GREENHOUSE), now=DAY_1 + timedelta(days=1)
    )

    assert (report.outcome, report.ingest.status) == (DailyOutcome.PARTIAL, "FAILED")
    assert (report.sources_completed, report.ingest.sources_failed) == (1, 1)
    assert report.workbook is not None
    assert report.workbook.path.name == f"jobs_2026-11-18_partial_run{report.ingest.run_id}.xlsx"
    assert not (tmp_path / "out" / "jobs_2026-11-18.xlsx").exists()  # never the complete name
    assert query(tmp_path, "SELECT title, status FROM jobs ORDER BY title") == [
        ("Job 1", "OPEN"),
        ("Job 2", "OPEN"),  # its source failed: the job is kept, not closed
    ]


@pytest.mark.parametrize("reason", ["DNSLookupError", "HTTP 403", "IgnoreRequest", "HTTP 429"])
def test_when_no_source_works_no_workbook_is_written(tmp_path: Path, reason: str) -> None:
    daily_run(tmp_path, ok(LEVER, job(1)))
    report = daily_run(tmp_path, failed(LEVER, reason), now=DAY_1 + timedelta(days=1))
    assert (report.outcome, report.workbook, report.export_error) == (DailyOutcome.FAILED, None, None)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["jobs_2026-11-17.xlsx"]
    assert query(tmp_path, "SELECT status FROM jobs") == [("OPEN",)]


def test_an_unexpected_empty_extraction_is_not_a_successful_source(tmp_path: Path) -> None:
    unrecognized = SourceResult(LEVER, (Extraction((), (), None, 0),), ())  # unusable response
    report = daily_run(tmp_path, unrecognized)
    assert report.outcome is DailyOutcome.FAILED and report.workbook is None


def test_a_crawl_crash_fails_the_run_and_releases_the_lock(tmp_path: Path) -> None:
    def crash(sources: list[Source]) -> list[SourceResult]:
        raise RuntimeError("reactor exploded")

    with pytest.raises(CollectionError):
        run_daily([LEVER], tmp_path / "c.db", tmp_path / "out", crash, now=DAY_1)
    assert query(tmp_path, "SELECT status FROM crawl_runs") == [("FAILED",)]
    assert not (tmp_path / "out").exists()
    with database_lock(tmp_path / "c.db"):
        pass


def test_an_interrupt_marks_the_run_failed_and_releases_the_lock(tmp_path: Path) -> None:
    def interrupted(sources: list[Source]) -> list[SourceResult]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_daily([LEVER], tmp_path / "c.db", tmp_path / "out", interrupted, now=DAY_1)
    assert query(tmp_path, "SELECT status, error_message FROM crawl_runs") == [
        ("FAILED", "interrupted")
    ]
    report = daily_run(tmp_path, ok(LEVER, job(1)), now=DAY_1 + timedelta(minutes=5))
    assert report.outcome is DailyOutcome.COMPLETE  # the next run works normally


def test_runs_left_running_by_a_killed_process_are_recovered(tmp_path: Path) -> None:
    init_db(tmp_path / "c.db")
    conn = connect(tmp_path / "c.db")
    try:
        orphan = start_run(conn, now=DAY_1 - timedelta(hours=1))
    finally:
        conn.close()

    report = daily_run(tmp_path, ok(LEVER, job(1)))

    assert report.recovered_runs == (orphan,)
    assert query(tmp_path, "SELECT id, status, error_message FROM crawl_runs ORDER BY id") == [
        (orphan, "FAILED", INTERRUPTED),
        (report.ingest.run_id, "SUCCEEDED", None),
    ]


def test_an_export_failure_keeps_the_stored_jobs_and_earlier_workbooks(tmp_path: Path) -> None:
    daily_run(tmp_path, ok(LEVER, job(1)))
    yesterday = (tmp_path / "out" / "jobs_2026-11-17.xlsx").read_bytes()
    (tmp_path / "out" / "jobs_2026-11-18.xlsx").mkdir()  # the target cannot be written

    report = daily_run(
        tmp_path, ok(LEVER, job(1), job(2)), now=DAY_1 + timedelta(days=1), overwrite=True
    )

    assert report.workbook is None and "is a directory" in (report.export_error or "")
    assert report.ingest.status == "SUCCEEDED" and report.ingest.jobs_new == 1
    assert query(tmp_path, "PRAGMA integrity_check") == [("ok",)]
    assert (tmp_path / "out" / "jobs_2026-11-17.xlsx").read_bytes() == yesterday
    assert not list((tmp_path / "out").glob("*.tmp"))


# --- stale candidates (Scenario H) --------------------------------------------------------------


def test_stale_candidates_need_two_complete_successful_runs_and_never_close(tmp_path: Path) -> None:
    a, b = job(1), job(2, GREENHOUSE)
    days = [DAY_1 + timedelta(days=n) for n in range(6)]

    def stale(report: Any) -> list[str]:
        return [candidate.title for candidate in report.stale]

    assert stale(daily_run(tmp_path, ok(LEVER, a), ok(GREENHOUSE, b), now=days[0])) == []
    # B's source fails: a FAILED run is no evidence of absence.
    assert stale(daily_run(tmp_path, ok(LEVER, a), failed(GREENHOUSE), now=days[1])) == []
    # B is missing from one complete successful run: not enough.
    assert stale(daily_run(tmp_path, ok(LEVER, a), ok(GREENHOUSE), now=days[2])) == []
    # Missing from a second successful run: listed for review, still OPEN.
    report = daily_run(tmp_path, ok(LEVER, a), ok(GREENHOUSE), now=days[3])
    assert stale(report) == ["Job 2"]
    assert report.stale[0].last_seen_at.startswith("2026-11-16T20:31")
    # It reappears: no longer a candidate.
    assert stale(daily_run(tmp_path, ok(LEVER, a), ok(GREENHOUSE, b), now=days[4])) == []
    assert query(tmp_path, "SELECT COUNT(*) FROM jobs WHERE status = 'CLOSED'") == [(0,)]


# --- the command line ---------------------------------------------------------------------------


def write_sources(tmp_path: Path) -> Path:
    path = tmp_path / "approved.csv"
    path.write_text(
        "platform,company_name,identifier,company_website\n"
        "lever,Example Corp,example-site,\ngreenhouse,Other Corp,other-board,\n",
        encoding="utf-8",
    )
    return path


def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *results: SourceResult, **kw: Any) -> Any:
    crawl, calls = crawl_returning(*results)
    monkeypatch.setattr("collector.cli.crawl_sources", crawl)
    args = ["daily", "--sources", str(kw.get("sources") or write_sources(tmp_path))]
    args += ["--db", str(tmp_path / "c.db"), "--output-dir", str(tmp_path / "out")]
    args += kw.get("extra", [])
    result = runner.invoke(app, args)
    result.calls = calls  # type: ignore[attr-defined]
    return result


def today() -> str:
    return business_date().isoformat()


def test_cli_complete_run_prints_an_honest_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = cli(tmp_path, monkeypatch, ok(LEVER, job(1)), ok(GREENHOUSE, job(2, GREENHOUSE)))
    assert result.exit_code == 0, result.output
    out = result.output
    assert f"DAILY {today()} COMPLETE (run 1 SUCCEEDED)" in out
    assert "sources: 2 configured, 2 completed, 0 failed (0 blocked)" in out
    assert "jobs this run: 2 new, 0 updated, 0 unchanged (2 stored)" in out
    assert "stale candidates: 0" in out
    assert "closed jobs: none (automatic closing is not supported" in out
    assert f"jobs_{today()}.xlsx (2 rows: every open job within the date window" in out


@pytest.mark.parametrize(
    ("results", "code", "expected"),
    [
        ((ok(LEVER, job(1)), failed(GREENHOUSE)), 1, "PARTIAL (run 1 FAILED)"),
        ((failed(LEVER), failed(GREENHOUSE, "HTTP 429")), 3, "workbook: none"),
    ],
)
def test_cli_partial_and_failed_runs_exit_non_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    results: tuple[SourceResult, ...],
    code: int,
    expected: str,
) -> None:
    result = cli(tmp_path, monkeypatch, *results)
    assert result.exit_code == code, result.output
    assert expected in result.output and "SUCCEEDED" not in result.output


def test_cli_refusals_do_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("platform,company_name,identifier\nbogus,X,y\n", encoding="utf-8")
    assert cli(tmp_path, monkeypatch, sources=bad).exit_code == 2
    missing = cli(tmp_path, monkeypatch, sources=tmp_path / "missing.csv")
    assert missing.exit_code == 2 and missing.calls == []

    (tmp_path / "out").mkdir()
    (tmp_path / "out" / f"jobs_{today()}.xlsx").write_bytes(b"x")
    exists = cli(tmp_path, monkeypatch, ok(LEVER, job(1)))
    assert exists.exit_code == 2 and exists.calls == [] and "nothing was done" in exists.output

    with database_lock(tmp_path / "c.db"):
        busy = cli(tmp_path, monkeypatch, ok(LEVER, job(1)), extra=["--overwrite"])
    assert busy.exit_code == 5 and busy.calls == [] and "another collection run" in busy.output
    assert not (tmp_path / "c.db").exists()


def test_cli_export_failure_exits_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "out" / f"jobs_{today()}.xlsx").mkdir(parents=True)
    result = cli(tmp_path, monkeypatch, ok(LEVER, job(1)), extra=["--overwrite"])
    assert result.exit_code == 4, result.output
    assert "EXPORT FAILED" in result.output and "the collected jobs are stored" in result.output


def test_cli_run_takes_the_same_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crawl, calls = crawl_returning(ok(LEVER, job(1)))
    monkeypatch.setattr("collector.cli.crawl_sources", crawl)
    args = ["run", "--sources", str(write_sources(tmp_path)), "--db", str(tmp_path / "c.db")]
    with database_lock(tmp_path / "c.db"):
        busy = runner.invoke(app, args)
    assert busy.exit_code == 5 and calls == []
    assert runner.invoke(app, args).exit_code == 0 and calls == [2]  # free again: it runs


def test_module_constants() -> None:
    assert daily.STALE_AFTER_SUCCESSFUL_RUNS == 2
