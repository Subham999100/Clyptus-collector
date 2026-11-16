"""Excel export (Phase 5). Offline: temporary SQLite databases filled through the real store, and
one end-to-end run through the real Scrapy engine with a scripted download handler."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tracemalloc
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook
from typer.testing import CliRunner

from collector.cli import app
from collector.export.excel import HEADERS, SHEET_NAME, ExportError, export_jobs
from collector.models import JobRecord
from collector.pipelines.store import connect, finish_run, init_db, start_run, upsert_job
from tests.test_ingest import ELIGIBLE, FUTURE, OLD, TOLERATED, job

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # 17:30 on 2026-01-15 in Asia/Kolkata
TODAY = datetime.now(UTC).date().isoformat()  # for the CLI, which reads the real clock
runner = CliRunner()


def build_db(path: Path, records: list[JobRecord], *, closed: tuple[str, ...] = ()) -> Path:
    """A real collector database: records stored through the store in one run."""
    init_db(path)
    conn = connect(path)
    try:
        run = start_run(conn, now=NOW)
        conn.execute("BEGIN IMMEDIATE")  # the store's writes join it (fast for many rows)
        for record in records:
            upsert_job(conn, record, run, now=NOW)
        conn.execute("COMMIT")
        finish_run(conn, run, "SUCCEEDED", now=NOW)
        for external_id in closed:  # the store never closes jobs; a later phase will
            conn.execute("UPDATE jobs SET status = 'CLOSED' WHERE external_id = ?", (external_id,))
    finally:
        conn.close()
    return path


def sheet_rows(path: Path) -> list[dict[str, Any]]:
    ws = load_workbook(path)[SHEET_NAME]
    return [
        dict(zip(HEADERS, row, strict=True)) for row in ws.iter_rows(min_row=2, values_only=True)
    ]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "collector.db"


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "exports" / "jobs.xlsx"


# --- workbook structure --------------------------------------------------------------------------


def test_creates_a_valid_xlsx_with_one_jobs_sheet_and_the_agreed_headers(
    db: Path, out: Path
) -> None:
    build_db(db, [job(1)])

    summary = export_jobs(db, out, reference=NOW)

    assert zipfile.is_zipfile(out)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Jobs"]
    assert [c.value for c in wb["Jobs"][1]] == [
        "Company Name", "Job Title", "Location", "Work Type", "Employment Type", "Salary",
        "Posted Date", "Posted Date Source", "Date Status", "Job Description",
        "Application URL", "Source URL", "Platform", "First Seen", "Last Seen",
    ]  # fmt: skip
    assert summary.rows == 1 and summary.path == out


def test_every_field_is_mapped_from_the_stored_job(db: Path, out: Path) -> None:
    record = job(
        1,
        ELIGIBLE,
        company_name="Example Corp",
        location="Bengaluru, India",
        work_type="remote",
        employment_type="Full-time",
        salary_text="INR 20-30 LPA",
        description_snippet="Build and run services.",
        apply_url="https://jobs.example.com/apply/1",
        source_url="https://boards.example.com/example",
        platform="greenhouse",
    )
    build_db(db, [record])

    export_jobs(db, out, reference=NOW)

    (row,) = sheet_rows(out)
    assert row == {
        "Company Name": "Example Corp",
        "Job Title": "Engineer 1",
        "Location": "Bengaluru, India",
        "Work Type": "Remote",
        "Employment Type": "Full-time",
        "Salary": "INR 20-30 LPA",
        "Posted Date": datetime(2026, 1, 10),
        "Posted Date Source": "Platform",
        "Date Status": "Eligible",
        "Job Description": "Build and run services.",
        "Application URL": "https://jobs.example.com/apply/1",
        "Source URL": "https://boards.example.com/example",
        "Platform": "Greenhouse",
        "First Seen": datetime(2026, 1, 15, 17, 30),  # stored UTC, shown in Asia/Kolkata
        "Last Seen": datetime(2026, 1, 15, 17, 30),
    }


def test_missing_optional_values_stay_blank_and_nothing_is_invented(db: Path, out: Path) -> None:
    build_db(db, [job(1, None)])  # no location, salary, description, posted date...

    export_jobs(db, out, reference=NOW)

    (row,) = sheet_rows(out)
    for column in ("Location", "Work Type", "Employment Type", "Salary", "Job Description"):
        assert row[column] is None, column
    assert row["Posted Date"] is None  # never filled from collection or first-seen time
    assert row["Posted Date Source"] == "Unknown"


def test_an_empty_database_gives_a_header_only_workbook(db: Path, out: Path) -> None:
    build_db(db, [])

    summary = export_jobs(db, out, reference=NOW)

    ws = load_workbook(out)["Jobs"]
    assert summary.rows == 0 and ws.max_row == 1
    assert ws.auto_filter.ref == "A1:O1"


# --- which jobs, and the date status -------------------------------------------------------------


def test_date_window_decides_rows_and_labels(db: Path, out: Path) -> None:
    build_db(
        db,
        [job(1, ELIGIBLE), job(2, None), job(3, FUTURE), job(4, TOLERATED)],
    )
    conn = sqlite3.connect(db)  # a job stored earlier that has aged out of the window since
    conn.execute("UPDATE jobs SET posted_date = ? WHERE external_id = 'ext-1'", (OLD,))
    conn.commit()
    conn.close()

    summary = export_jobs(db, out, reference=NOW)

    status = {r["Job Title"]: r["Date Status"] for r in sheet_rows(out)}
    assert status == {
        "Engineer 2": "Unknown date - review",
        "Engineer 3": "Future date - review",
        "Engineer 4": "Eligible (dated 1 day ahead)",
    }  # Engineer 1 is now OLD: excluded
    assert (summary.rows, summary.eligible, summary.unknown_date) == (3, 1, 1)
    assert (summary.future_date, summary.old_date, summary.closed) == (1, 1, 0)


def test_the_window_is_evaluated_at_the_reference_moment(db: Path, out: Path) -> None:
    build_db(db, [job(1, ELIGIBLE)])  # 2026-01-10

    assert export_jobs(db, out, reference=NOW).rows == 1
    later = export_jobs(db, out, reference=NOW + timedelta(days=40), overwrite=True)
    assert (later.rows, later.old_date) == (0, 1)


def test_closed_jobs_are_excluded(db: Path, out: Path) -> None:
    build_db(db, [job(1), job(2), job(3)], closed=("ext-2",))

    summary = export_jobs(db, out, reference=NOW)

    assert sorted(r["Job Title"] for r in sheet_rows(out)) == ["Engineer 1", "Engineer 3"]
    assert (summary.rows, summary.closed) == (2, 1)


def test_rows_are_ordered_newest_first_unknown_dates_last_and_stably(
    db: Path, tmp_path: Path
) -> None:
    records = [
        job(1, "2026-01-02"),
        job(2, None),
        job(3, "2026-01-12"),
        job(4, None),
        job(5, "2026-01-12"),
        job(6, "2026-01-05"),
    ]
    build_db(db, records)
    first, second = tmp_path / "a.xlsx", tmp_path / "b.xlsx"

    export_jobs(db, first, reference=NOW)
    export_jobs(db, second, reference=NOW)

    rows = sheet_rows(first)
    dates = [r["Posted Date"] for r in rows]
    known = [d for d in dates if d is not None]
    assert known == sorted(known, reverse=True)
    assert dates[-2:] == [None, None]  # unknown dates form the last group
    assert rows == sheet_rows(second)  # unchanged data: identical rows in identical order
    with sqlite3.connect(db) as conn:  # ties (same date, unknown dates) follow the job key
        keys = dict(conn.execute("SELECT title, job_key FROM jobs").fetchall())
    same_day = [r["Job Title"] for r in rows if r["Posted Date"] == datetime(2026, 1, 12)]
    assert same_day == sorted(same_day, key=keys.__getitem__)


# --- safety --------------------------------------------------------------------------------------


HOSTILE = {
    "title": '=HYPERLINK("https://evil.example","Click")',
    "company_name": "+cmd|' /C calc'!A0",
    "location": "-2+3",
    "salary_text": "@SUM(A1:A9)",
    "employment_type": "\t=1+1",
    "description_snippet": " \r=2+2",
}


def test_scraped_text_can_never_become_a_formula(db: Path, out: Path) -> None:
    build_db(db, [job(1, ELIGIBLE, **HOSTILE)])

    export_jobs(db, out, reference=NOW)

    sheet_xml = zipfile.ZipFile(out).read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet_xml and "<f " not in sheet_xml  # no formula anywhere
    ws = load_workbook(out)["Jobs"]
    cells = {ws.cell(row=1, column=c).value: ws.cell(row=2, column=c) for c in range(1, 16)}
    assert cells["Job Title"].value == HOSTILE["title"]  # shown literally
    assert cells["Company Name"].value == HOSTILE["company_name"]
    assert cells["Location"].value == HOSTILE["location"]
    assert cells["Salary"].value == HOSTILE["salary_text"]
    for header in ("Job Title", "Company Name", "Location", "Salary", "Employment Type"):
        assert cells[header].data_type == "s", header
        assert cells[header]._style.quotePrefix, header  # Excel "treat as text"


def test_illegal_control_characters_are_dropped_not_fatal(db: Path, out: Path) -> None:
    conn_record = job(1, ELIGIBLE, description_snippet="ok\x00\x07=3 done")
    build_db(db, [conn_record])

    export_jobs(db, out, reference=NOW)

    assert sheet_rows(out)[0]["Job Description"] == "ok=3 done"


def test_hyperlinks_only_for_public_http_urls(db: Path, out: Path) -> None:
    records = [
        job(1, ELIGIBLE, apply_url="https://jobs.example.com/apply/1"),
        job(2, ELIGIBLE, apply_url="https://user:secret@jobs.example.com/apply/2"),
        job(3, ELIGIBLE, apply_url="http://127.0.0.1/apply/3"),
        job(4, ELIGIBLE, apply_url="http://intranet/apply/4"),
    ]
    build_db(db, records)

    export_jobs(db, out, reference=NOW)

    ws = load_workbook(out)["Jobs"]
    by_title = {ws.cell(row=r, column=2).value: ws.cell(row=r, column=11) for r in range(2, 6)}
    good = by_title["Engineer 1"]
    assert good.value == "https://jobs.example.com/apply/1"
    assert good.hyperlink is not None and good.hyperlink.target == good.value
    source = ws.cell(row=2, column=12)  # the source URL is shown, not linked (memory, see module)
    assert source.value.startswith("https://") and source.hyperlink is None
    for unsafe in ("Engineer 2", "Engineer 3", "Engineer 4"):  # not shown, not clickable
        assert by_title[unsafe].value is None and by_title[unsafe].hyperlink is None
    assert "secret" not in zipfile.ZipFile(out).read("xl/worksheets/sheet1.xml").decode()
    rels = zipfile.ZipFile(out).read("xl/worksheets/_rels/sheet1.xml.rels").decode()
    assert "secret" not in rels and "127.0.0.1" not in rels


def test_long_descriptions_are_wrapped_and_capped_at_excels_cell_limit(db: Path, out: Path) -> None:
    build_db(db, [job(1, ELIGIBLE, description_snippet="word " * 9000)])  # 45,000 characters

    export_jobs(db, out, reference=NOW)

    cell = load_workbook(out)["Jobs"]["J2"]
    assert len(cell.value) == 32767
    assert cell.alignment.wrap_text is True


# --- formatting ----------------------------------------------------------------------------------


def test_header_filters_freeze_and_formats(db: Path, out: Path) -> None:
    build_db(db, [job(1, ELIGIBLE), job(2, None)])

    export_jobs(db, out, reference=NOW)

    ws = load_workbook(out)["Jobs"]
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == "A1:O3"
    for cell in ws[1]:
        assert cell.font.bold and cell.font.color.rgb.endswith("FFFFFF")
        assert cell.fill.fgColor.rgb.endswith("F26B21")  # Clyptus orange
    assert ws["A2"].fill.fgColor.rgb in (None, "00000000")  # body stays white
    assert ws["G2"].number_format == "yyyy-mm-dd"
    assert ws["N2"].number_format == "yyyy-mm-dd hh:mm"
    assert ws["J2"].alignment.wrap_text and ws["B2"].alignment.wrap_text
    assert ws.column_dimensions["J"].width == 70 and ws.column_dimensions["A"].width == 28
    assert ws["K2"].font.underline == "single"


def test_large_export_streams_with_bounded_memory(db: Path, out: Path) -> None:
    count = 5000
    records = [
        job(n, (NOW.date() - timedelta(days=n % 45)).isoformat() if n % 4 else None,
            description_snippet="Design, build and operate services. " * 12)
        for n in range(count)
    ]  # fmt: skip
    build_db(db, records)

    tracemalloc.start()
    try:
        summary = export_jobs(db, out, reference=NOW)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    expected_old = sum(1 for n in range(count) if n % 4 and n % 45 > 30)
    assert summary.old_date == expected_old
    written = sum(1 for _ in load_workbook(out, read_only=True)["Jobs"].iter_rows(min_row=2))
    assert summary.rows == count - expected_old == written
    assert peak < 40 * 1024 * 1024  # Python allocations stay small: rows are streamed


# --- the database is never modified --------------------------------------------------------------


def test_export_does_not_modify_the_database(db: Path, out: Path) -> None:
    build_db(db, [job(1), job(2, None)])
    before, mtime = digest(db), db.stat().st_mtime_ns

    export_jobs(db, out, reference=NOW)
    export_jobs(db, out, reference=NOW, overwrite=True)

    assert digest(db) == before and db.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in db.parent.iterdir() if p.name.startswith(db.name)) == [db.name]


def test_export_refuses_missing_unsupported_and_foreign_databases(
    tmp_path: Path, out: Path
) -> None:
    with pytest.raises(ExportError, match="database not found") as missing:
        export_jobs(tmp_path / "nope.db", out)
    assert missing.value.database and not (tmp_path / "nope.db").exists()

    old = tmp_path / "v1.db"
    with sqlite3.connect(old) as conn:
        conn.execute("PRAGMA user_version = 1")
    before = digest(old)
    with pytest.raises(ExportError, match="schema version 1"):
        export_jobs(old, out)
    assert digest(old) == before  # never migrated by an export

    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    with pytest.raises(ExportError, match="not a collector database"):
        export_jobs(junk, out)
    assert not out.exists()


def test_output_is_protected_and_replaced_only_on_request(db: Path, out: Path) -> None:
    build_db(db, [job(1)])
    out.parent.mkdir(parents=True)
    out.write_bytes(b"precious")

    with pytest.raises(ExportError, match="already exists") as refused:
        export_jobs(db, out, reference=NOW)
    assert not refused.value.database and out.read_bytes() == b"precious"

    export_jobs(db, out, reference=NOW, overwrite=True)
    assert zipfile.is_zipfile(out)
    assert [p.name for p in out.parent.iterdir()] == ["jobs.xlsx"]  # no temporary file left


def test_a_failed_export_leaves_no_partial_file(
    db: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_db(db, [job(1)])

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("openpyxl.workbook.workbook.Workbook.save", explode)
    with pytest.raises(RuntimeError):
        export_jobs(db, out, reference=NOW)

    assert list(out.parent.iterdir()) == []


# --- CLI -----------------------------------------------------------------------------------------


def test_cli_export_writes_the_workbook_and_reports_counts(tmp_path: Path) -> None:
    db = build_db(tmp_path / "c.db", [job(1, TODAY), job(2, None), job(3, "2020-01-01")])
    target = tmp_path / "nested" / "dir" / "jobs.xlsx"  # the directory is created

    result = runner.invoke(app, ["export", "--db", str(db), "--output", str(target)])

    assert result.exit_code == 0, result.output
    assert f"Export completed: 2 jobs written to {target}" in result.output
    assert "1 eligible, 1 unknown date, 0 future date (review); excluded: 1 old, 0 closed" in (
        result.output
    )
    assert len(sheet_rows(target)) == 2


def test_cli_export_defaults_to_the_configured_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = build_db(tmp_path / "configured.db", [job(1, TODAY)])
    monkeypatch.setenv("COLLECTOR_SQLITE_PATH", str(db))
    monkeypatch.setenv("COLLECTOR_OUTPUT_DIR", str(tmp_path / "out"))

    result = runner.invoke(app, ["export"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "jobs.xlsx").is_file()


@pytest.mark.parametrize("kind", ["missing", "old-schema", "junk"])
def test_cli_export_reports_database_problems_with_exit_3(tmp_path: Path, kind: str) -> None:
    db = tmp_path / "x.db"
    if kind == "old-schema":
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA user_version = 1")
    elif kind == "junk":
        db.write_bytes(b"junk" * 500)
    target = tmp_path / "jobs.xlsx"

    result = runner.invoke(app, ["export", "--db", str(db), "--output", str(target)])

    assert result.exit_code == 3
    assert result.output.startswith("error: ")
    assert "Traceback" not in result.output and isinstance(result.exception, SystemExit)
    assert not target.exists()
    assert db.exists() == (kind != "missing")  # a missing database is never created


def test_cli_export_protects_an_existing_file_and_overwrites_on_request(tmp_path: Path) -> None:
    db = build_db(tmp_path / "c.db", [job(1, TODAY)])
    target = tmp_path / "jobs.xlsx"
    target.write_bytes(b"keep me")

    refused = runner.invoke(app, ["export", "--db", str(db), "--output", str(target)])
    assert refused.exit_code == 2 and "already exists" in refused.output
    assert "--overwrite" in refused.output and target.read_bytes() == b"keep me"

    replaced = runner.invoke(
        app, ["export", "--db", str(db), "--output", str(target), "--overwrite"]
    )
    assert replaced.exit_code == 0 and zipfile.is_zipfile(target)


def test_cli_export_refuses_a_directory_as_output(tmp_path: Path) -> None:
    db = build_db(tmp_path / "c.db", [job(1, TODAY)])

    result = runner.invoke(app, ["export", "--db", str(db), "--output", str(tmp_path)])

    assert result.exit_code == 2 and "is a directory" in result.output


def test_cli_export_never_crawls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scrapy.crawler

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("crawler started")

    monkeypatch.setattr(scrapy.crawler.CrawlerProcess, "__init__", fail)
    monkeypatch.setattr("collector.cli.crawl_sources", fail)
    db = build_db(tmp_path / "c.db", [job(1, TODAY)])

    result = runner.invoke(app, ["export", "--db", str(db), "--output", str(tmp_path / "j.xlsx")])

    assert result.exit_code == 0, result.output


# --- end to end: scripted Scrapy responses -> adapters -> SQLite -> Excel ------------------------


def collect(tmp_path: Path, scenario: dict[str, Any], name: str) -> dict[str, Any]:
    path = tmp_path / f"{name}.json"
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
    lines = completed.stdout.splitlines()
    summary = json.loads(next(x for x in lines if x.startswith("SUMMARY:"))[8:])
    requests = json.loads(next(x for x in lines if x.startswith("REQUESTS:"))[9:])
    # The harness's own REQUESTS line lists every URL it served; it is test instrumentation, so
    # it is left out of what is checked for leaks (the collector's output and logs).
    output = "\n".join(x for x in lines if not x.startswith("REQUESTS:")) + completed.stderr
    return {"summary": summary, "requests": requests, "output": output}


def test_offline_crawl_to_sqlite_to_excel_end_to_end(tmp_path: Path) -> None:
    gh = "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true"
    ab = "https://api.ashbyhq.com/posting-api/job-board/{}"
    lv = "https://api.lever.co/v0/postings/{}?mode=json"
    html = {"headers": {"Content-Type": "text/html"}}
    page_a, page_b = "https://jobs.example.org/careers", "https://jobs.example.org/mirror"
    blocked_page = "https://jobs.example.org/blocked?token=SECRET-TOKEN"
    database = tmp_path / "e2e.db"
    scenario = {
        "db": str(database),
        "now": "2026-10-20T12:00:00+00:00",
        "sources": [
            {"platform": "greenhouse", "company_name": "Co G", "identifier": "gh-ok"},
            {"platform": "lever", "company_name": "Co L", "identifier": "lv-ok"},
            {"platform": "ashby", "company_name": "Co A", "identifier": "ab-ok"},
            {"platform": "jsonld", "company_name": "Co J", "identifier": page_a},
            {"platform": "jsonld", "company_name": "Co J", "identifier": page_b},  # same jobs
            {"platform": "jsonld", "company_name": "Co X", "identifier": blocked_page},
            {"platform": "greenhouse", "company_name": "Co B", "identifier": "gh-bad"},
        ],
        "responses": {
            gh.format("gh-ok"): {"fixture": "greenhouse/list_complete.json"},
            lv.format("lv-ok"): {"fixture": "lever/postings_complete.json"},
            ab.format("ab-ok"): {"fixture": "ashby/board_complete.json"},
            page_a: {"fixture": "jsonld/multiple_job_postings.html", **html},
            page_b: {"fixture": "jsonld/multiple_job_postings.html", **html},
            blocked_page: {"status": 403, "body": "SECRET-PAYLOAD", **html},
            gh.format("gh-bad"): {"body": "{broken SECRET-PAYLOAD"},
        },
    }

    first = collect(tmp_path, scenario, "first")
    second = collect(tmp_path, scenario, "second")  # the same crawl again

    # Collection: duplicates across the two mirror pages are stored once; a blocked and a broken
    # source fail the run (FAILED, by the existing policy) but the healthy sources' jobs stay.
    s1, s2 = first["summary"], second["summary"]
    assert (s1["status"], s1["sources_failed"], s1["sources_blocked"]) == ("FAILED", 2, 1)
    assert (s1["unique"], s1["duplicates"], s1["jobs_new"]) == (5, 2, 5)
    assert (s2["jobs_new"], s2["jobs_seen"], s2["jobs_updated"]) == (0, 5, 0)  # re-sightings
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5  # no duplicate rows
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE last_successful_run_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )  # FAILED runs advance no successful-run reference
    before = digest(database)

    # Export two weeks later: the two JSON-LD postings (Oct 1 and 3) are then over 30 days old.
    target = tmp_path / "out" / "jobs.xlsx"
    summary = export_jobs(database, target, reference=datetime(2026, 11, 4, 12, tzinfo=UTC))

    rows = sheet_rows(target)
    assert (summary.rows, summary.old_date, summary.unknown_date, summary.eligible) == (3, 2, 2, 1)
    assert (rows[0]["Platform"], rows[0]["Date Status"]) == ("Ashby", "Eligible")  # 2026-10-06
    assert {(r["Platform"], r["Date Status"]) for r in rows[1:]} == {
        ("Greenhouse", "Unknown date - review"),
        ("Lever", "Unknown date - review"),
    }  # unknown dates last; Greenhouse and Lever expose no posted date
    assert all(r["Application URL"] and r["Application URL"].startswith("https://") for r in rows)
    assert digest(database) == before  # export read only

    # Nothing left the process, and no secret reached the reports or the workbook.
    assert {r["url"].split("/")[2] for r in first["requests"]} <= {
        "boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com", "jobs.example.org",
    }  # fmt: skip
    workbook_text = "".join(
        zipfile.ZipFile(target).read(name).decode("utf-8", "ignore")
        for name in zipfile.ZipFile(target).namelist()
    )
    for text in (first["output"], second["output"], workbook_text):
        assert "SECRET-PAYLOAD" not in text and "SECRET-TOKEN" not in text


# --- regressions found in review ---------------------------------------------------------------


def test_cli_reports_a_database_without_a_jobs_table_without_a_traceback(tmp_path: Path) -> None:
    db = tmp_path / "empty-v2.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 2")  # right version, but not a collector database
    target = tmp_path / "jobs.xlsx"

    result = runner.invoke(app, ["export", "--db", str(db), "--output", str(target)])

    assert result.exit_code == 3 and "cannot read jobs" in result.output
    assert isinstance(result.exception, SystemExit) and "Traceback" not in result.output
    assert list(tmp_path.iterdir()) == [db]  # no workbook, no temporary file


def test_cli_reports_a_write_failure_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = build_db(tmp_path / "c.db", [job(1, TODAY)])
    target = tmp_path / "out" / "jobs.xlsx"

    def disk_full(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("openpyxl.workbook.workbook.Workbook.save", disk_full)
    result = runner.invoke(app, ["export", "--db", str(db), "--output", str(target)])

    assert result.exit_code == 2 and "No space left on device" in result.output
    assert isinstance(result.exception, SystemExit)
    assert list(target.parent.iterdir()) == []  # the temporary file was removed
