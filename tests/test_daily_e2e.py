"""`collector daily` end to end (Phase 9): the real CLI, crawler (Scrapy engine, middlewares,
robots.txt), adapters, SQLite store and Excel exporter, with scripted responses instead of the
network (tests/crawl_harness.py; sockets blocked). Each invocation is its own process, so two
runs can overlap and a run can be killed, exactly as under a scheduler. Synthetic data only."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from collector.export.excel import HEADERS, SHEET_NAME
from collector.pipelines.daily import INTERRUPTED, business_date

ROOT = Path(__file__).parent.parent
HTML = {"headers": {"Content-Type": "text/html; charset=utf-8"}}
LISTING = "https://careers.example.com/careers/jobs"
LEVER = "https://api.lever.co/v0/postings/example-site?mode=json"
SELECTORS = {
    "listing": "ul.jobs",
    "job": "li.job",
    "title": "a.job-link",
    "url": "a.job-link",
    "location": ".job-location",
    "next_page": "a.next",
    "max_pages": 3,
    "path_prefix": "/careers/",
    "review": {
        "verified_by": "Test Reviewer",
        "ownership_evidence": "synthetic test source",
        "authorization": "APPROVED",
        "authorized_by": "Test Owner",
        "note": "offline test only",
    },
}


def responses(**changes: Any) -> dict[str, Any]:
    page_1 = (ROOT / "tests/fixtures/generic/listing_page_1.html").read_text(encoding="utf-8")
    scripted = {
        LISTING: {"body": page_1, **HTML},
        f"{LISTING}?page=2": {"fixture": "generic/listing_page_2.html", **HTML},
        LEVER: {"fixture": "lever/postings_complete.json"},
    }
    scripted.update(changes)
    return scripted


def setup(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "acme.json").write_text(json.dumps(SELECTORS), encoding="utf-8")
    sources = folder / "approved_sources.csv"
    sources.write_text(
        "platform,company_name,identifier,company_website,html_config\n"
        f"html,Acme Synthetic Ltd,{LISTING},,acme.json\n"
        "lever,Example Synthetic Corp,example-site,,\n",
        encoding="utf-8",
    )
    return sources


def start(folder: Path, scripted: dict[str, Any], *extra: str, name: str = "run") -> Any:
    args = ["daily", "--sources", str(folder / "approved_sources.csv")]
    args += ["--db", str(folder / "collector.db"), "--output-dir", str(folder / "exports")]
    scenario = folder / f"{name}.json"
    scenario.write_text(json.dumps({"cli": [*args, *extra], "responses": scripted}), "utf-8")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COLLECTOR_")}
    environment.update(COLLECTOR_DOWNLOAD_DELAY_SECONDS="0", COLLECTOR_AUTOTHROTTLE_ENABLED="false")
    return subprocess.Popen(
        [sys.executable, "-c", "from tests.crawl_harness import main; main()", str(scenario)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process: Any) -> dict[str, Any]:
    stdout, stderr = process.communicate(timeout=180)
    lines = stdout.splitlines()
    code = json.loads(next(x for x in lines if x.startswith("EXIT:"))[5:])
    requests = json.loads(next(x for x in lines if x.startswith("REQUESTS:"))[9:])
    output = "\n".join(x for x in lines if not x.startswith(("EXIT:", "REQUESTS:")))
    return {"code": code, "requests": requests, "out": output + "\n" + stderr}


def daily(folder: Path, scripted: dict[str, Any], *extra: str) -> dict[str, Any]:
    return finish(start(folder, scripted, *extra))


def db(folder: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(f"{(folder / 'collector.db').as_uri()}?mode=ro", uri=True) as conn:
        return conn.execute(sql).fetchall()


def wait_for_running_run(folder: Path, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if db(folder, "SELECT 1 FROM crawl_runs WHERE status = 'RUNNING'"):
                return
        except sqlite3.Error:
            pass  # not created yet
        time.sleep(0.2)
    raise AssertionError("the first run never started")


def todays_workbooks(folder: Path) -> list[Path]:
    days = {business_date(), business_date(datetime.now(UTC))}  # a run may cross midnight
    return [folder / "exports" / f"jobs_{day.isoformat()}.xlsx" for day in sorted(days)]


def workbook(folder: Path) -> tuple[Path, list[dict[str, Any]]]:
    (path,) = [p for p in todays_workbooks(folder) if p.is_file()]
    rows = list(load_workbook(path)[SHEET_NAME].iter_rows(values_only=True))
    assert rows[0] == HEADERS
    return path, [dict(zip(HEADERS, row, strict=True)) for row in rows[1:]]


def test_first_second_and_updated_day(tmp_path: Path) -> None:
    setup(tmp_path)

    # Scenario A: first successful day.
    first = daily(tmp_path, responses())
    assert first["code"] == 0, first["out"]
    assert "COMPLETE (run 1 SUCCEEDED)" in first["out"]
    assert "jobs this run: 4 new, 0 updated, 0 unchanged (4 stored)" in first["out"]
    assert "sources: 2 configured, 2 completed, 0 failed (0 blocked)" in first["out"]
    path, rows = workbook(tmp_path)
    assert sorted((r["Platform"], r["Job Title"]) for r in rows) == [
        ("Lever", "Product Designer"),
        ("html", "Backend Engineer"),
        ("html", "Frontend Engineer"),
        ("html", "Mobile Engineer"),
    ]
    assert f"{path} (4 rows" in first["out"]
    assert {r["Company Name"] for r in rows} == {"Acme Synthetic Ltd", "Example Synthetic Corp"}
    assert db(tmp_path, "SELECT status, jobs_seen, jobs_new FROM crawl_runs") == [
        ("SUCCEEDED", 4, 4)
    ]

    # Scenario B: the same day again. Without --overwrite nothing at all happens ...
    refused = daily(tmp_path, responses())
    assert refused["code"] == 2 and refused["requests"] == []
    assert "already exists" in refused["out"]
    assert db(tmp_path, "SELECT COUNT(*) FROM crawl_runs") == [(1,)]
    # ... with it, the same jobs are re-observed as unchanged, never duplicated.
    again = daily(tmp_path, responses(), "--overwrite")
    assert again["code"] == 0, again["out"]
    assert "jobs this run: 0 new, 0 updated, 4 unchanged (4 stored)" in again["out"]
    assert db(tmp_path, "SELECT COUNT(*), COUNT(DISTINCT job_key) FROM jobs") == [(4, 4)]

    # Scenario C: a changed listing updates the job in place.
    page_1 = responses()[LISTING]["body"].replace("Pune", "Mumbai", 1)
    changed = daily(tmp_path, responses(**{LISTING: {"body": page_1, **HTML}}), "--overwrite")
    assert changed["code"] == 0, changed["out"]
    assert "jobs this run: 0 new, 1 updated, 3 unchanged (4 stored)" in changed["out"]
    _, rows = workbook(tmp_path)
    assert {r["Job Title"]: r["Location"] for r in rows if r["Platform"] == "html"} == {
        "Backend Engineer": "Mumbai",
        "Frontend Engineer": "Pune",
        "Mobile Engineer": "Remote",
    }
    assert db(tmp_path, "SELECT COUNT(*) FROM jobs") == [(4,)]


def test_a_failed_source_is_reported_as_partial_and_closes_nothing(tmp_path: Path) -> None:
    setup(tmp_path)
    assert daily(tmp_path, responses())["code"] == 0
    complete, _ = workbook(tmp_path)
    before = complete.read_bytes()

    # Scenario D: the Lever API cannot be resolved (DNS), retried twice, then reported.
    result = daily(tmp_path, responses(**{LEVER: {"error": "dns"}}), "--overwrite")

    assert result["code"] == 1, result["out"]
    assert "PARTIAL (run 2 FAILED)" in result["out"]
    assert "sources: 2 configured, 1 completed, 1 failed (0 blocked)" in result["out"]
    assert "fetch error: DNSLookupError" in result["out"]
    assert result["requests"].count(LEVER) == 3
    partial = complete.with_name(complete.stem + "_partial_run2.xlsx")
    assert partial.is_file() and complete.read_bytes() == before  # the complete one is kept
    assert db(tmp_path, "SELECT COUNT(*), SUM(status = 'OPEN') FROM jobs") == [(4, 4)]


def test_an_export_failure_is_distinguished_from_the_collection(tmp_path: Path) -> None:
    setup(tmp_path)
    earlier = tmp_path / "exports" / "jobs_2026-01-01.xlsx"
    earlier.parent.mkdir()
    earlier.write_bytes(b"an earlier report")
    for path in todays_workbooks(tmp_path):
        path.mkdir()  # Scenario F: today's workbook cannot be written

    result = daily(tmp_path, responses(), "--overwrite")

    assert result["code"] == 4, result["out"]
    assert "EXPORT FAILED" in result["out"] and "COMPLETE (run 1 SUCCEEDED)" in result["out"]
    assert db(tmp_path, "PRAGMA integrity_check") == [("ok",)]
    assert db(tmp_path, "SELECT COUNT(*) FROM jobs") == [(4,)]
    assert earlier.read_bytes() == b"an earlier report"
    assert not list((tmp_path / "exports").glob("*.tmp"))


def test_overlapping_runs_do_not_crawl_twice(tmp_path: Path) -> None:
    setup(tmp_path)
    slow = responses(**{LISTING: {**responses()[LISTING], "delay": 6}})
    first = start(tmp_path, slow, name="first")
    try:
        wait_for_running_run(tmp_path)
        # Scenario E: a second invocation while the first is crawling.
        second = daily(tmp_path, responses())
        assert second["code"] == 5, second["out"]
        assert second["requests"] == []  # it never started a crawl
        assert "another collection run is using this database" in second["out"]
    finally:
        outcome = finish(first)
    assert outcome["code"] == 0, outcome["out"]  # the first run was unaffected
    assert db(tmp_path, "SELECT id, status FROM crawl_runs") == [(1, "SUCCEEDED")]


def test_a_killed_run_is_recovered_by_the_next_one(tmp_path: Path) -> None:
    setup(tmp_path)
    stuck = responses(**{LISTING: {**responses()[LISTING], "delay": 120}})
    victim = start(tmp_path, stuck, name="victim")
    wait_for_running_run(tmp_path)
    victim.kill()  # Scenario G: a hard kill, no cleanup code runs
    victim.communicate(timeout=60)
    assert db(tmp_path, "SELECT status FROM crawl_runs") == [("RUNNING",)]
    assert db(tmp_path, "PRAGMA integrity_check") == [("ok",)]

    result = daily(tmp_path, responses())  # the OS released the lock with the process

    assert result["code"] == 0, result["out"]
    assert "recovered: run 1 was left RUNNING by an interrupted process" in result["out"]
    assert db(tmp_path, "SELECT id, status, error_message FROM crawl_runs") == [
        (1, "FAILED", INTERRUPTED),
        (2, "SUCCEEDED", None),
    ]
    _, rows = workbook(tmp_path)
    assert len(rows) == 4
