"""Generic HTML sources through the real crawl (Phase 8): real Scrapy engine, middlewares and
settings with a scripted download handler (tests/crawl_harness.py, one subprocess per scenario;
sockets blocked). Covers listing -> detail pages, bounded pagination, host restrictions,
redirects, robots.txt, and the SQLite/Excel path."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openpyxl import load_workbook

from collector.export.excel import HEADERS, SHEET_NAME, export_jobs
from collector.pipelines.store import SCHEMA_VERSION

ROOT = Path(__file__).parent.parent
HTML = {"headers": {"Content-Type": "text/html; charset=utf-8"}}
LISTING = "https://careers.example.com/careers/jobs"
SELECTORS: dict[str, Any] = {
    "listing": "ul.jobs",
    "job": "li.job",
    "title": "a.job-link",
    "url": "a.job-link",
    "location": ".job-location",
    "next_page": "a.next",
    "max_pages": 3,
    "path_prefix": "/careers/",
}
DETAIL = {"location": "dd.location", "posted_date": "dd.posted", "description": "div.description"}


def run_harness(tmp_path: Path, scenario: dict[str, Any]) -> tuple[list[str], str]:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COLLECTOR_")}
    environment.update(COLLECTOR_DOWNLOAD_DELAY_SECONDS="0", COLLECTOR_AUTOTHROTTLE_ENABLED="false")
    completed = subprocess.run(
        [sys.executable, "-c", "from tests.crawl_harness import main; main()", str(path)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    return completed.stdout.splitlines(), completed.stderr


def crawl(tmp_path: Path, sources: list[dict[str, Any]], responses: dict[str, Any]) -> Any:
    lines, stderr = run_harness(tmp_path, {"sources": sources, "responses": responses})
    result = json.loads(next(x for x in lines if x.startswith("RESULT:"))[7:])
    result["by_id"] = {r["identifier"]: r for r in result["results"]}
    result["urls"] = [r["url"] for r in result["requests"]]
    result["stderr"] = stderr
    return result


def html_source(identifier: str, **changes: Any) -> dict[str, Any]:
    selectors = {key: value for key, value in {**SELECTORS, **changes}.items() if value is not None}
    return {
        "platform": "html",
        "company_name": "Example Corp",
        "identifier": identifier,
        "html": selectors,
    }


def fixture(name: str) -> dict[str, Any]:
    return {"fixture": f"generic/{name}", **HTML}


def job(n: int, host: str = "careers.example.com") -> str:
    return f"https://{host}/careers/jobs/{n}"


def test_listing_detail_and_pagination_flow(tmp_path: Path) -> None:
    responses = {
        LISTING: fixture("listing_page_1.html"),
        f"{LISTING}?page=2": fixture("listing_page_2.html"),
        job(111): fixture("detail_page_101.html"),
        job(112): {"status": 404, "body": "SECRET-PAYLOAD", **HTML},
        job(113): fixture("detail_plain_html.html"),
    }
    result = crawl(tmp_path, [html_source(LISTING, detail=DETAIL)], responses)
    outcome = result["by_id"][LISTING]

    assert sorted(outcome["titles"]) == ["Backend Engineer", "Mobile Engineer"]
    assert outcome["extraction_failures"] == ["invalid_response"]  # the missing detail page
    assert outcome["fetch_failures"] == [] and outcome["blocked"] is False
    # Two listing pages and three detail pages, each requested exactly once, plus robots.txt.
    pages = [LISTING, f"{LISTING}?page=2", job(111), job(112), job(113)]
    assert sorted(u for u in result["urls"] if not u.endswith("/robots.txt")) == sorted(pages)
    assert result["urls"].count("https://careers.example.com/robots.txt") == 1
    assert {urlsplit(u).hostname for u in result["urls"]} == {"careers.example.com"}
    assert "SECRET-PAYLOAD" not in json.dumps(result["results"]) + result["stderr"]


def test_links_off_the_source_host_and_redirects_away_are_never_requested(
    tmp_path: Path,
) -> None:
    cross = "https://jobs.example.org/careers/open"
    moved = "https://www.example.org/careers/jobs"
    elsewhere = "https://evil.example.net/careers/jobs"
    responses: dict[str, Any] = {
        cross: fixture("listing_cross_domain.html"),
        moved: {"status": 302, "headers": {"Location": elsewhere}},
        elsewhere: fixture("listing_page_2.html"),  # would show up if it were ever requested
        "https://jobs.example.org/careers/jobs/954": fixture("detail_page_101.html"),
    }
    for target in (
        "https://jobs.example.net/careers/jobs/951",
        "https://other.example.org/careers/jobs?page=2",
    ):
        responses[target] = fixture("listing_page_2.html")
    sources = [html_source(cross, detail=DETAIL), html_source(moved)]

    result = crawl(tmp_path, sources, responses)

    hosts = {urlsplit(u).hostname for u in result["urls"]}
    assert hosts == {"jobs.example.org", "www.example.org"}
    across = result["by_id"][cross]
    assert across["extraction_failures"] == ["invalid_url"] * 3
    assert across["titles"] == ["Account Manager"]
    assert [u for u in result["urls"] if "/careers/jobs/" in u] == [
        "https://jobs.example.org/careers/jobs/954"  # the one job on the source host
    ]
    assert any("another host" in w for w in across["warnings"])  # the next-page link
    redirected = result["by_id"][moved]
    assert redirected["records"] == 0 and redirected["extractions"] == 0
    assert len(redirected["fetch_failures"]) == 1  # refused before any request, reported


def test_pagination_loops_and_javascript_pages(tmp_path: Path) -> None:
    loop = "https://loop.example.org/careers/jobs"
    shell = "https://shell.example.org/careers"
    back_to_first = {
        "body": (ROOT / "tests/fixtures/generic/listing_page_2.html")
        .read_text(encoding="utf-8")
        .replace('class="prev"', 'class="next"'),
        **HTML,
    }
    responses = {
        loop: fixture("listing_page_1.html"),
        f"{loop}?page=2": back_to_first,
        shell: fixture("listing_js_rendered_shell.html"),
    }
    result = crawl(tmp_path, [html_source(loop), html_source(shell)], responses)

    looped = result["by_id"][loop]
    assert result["urls"].count(loop) == 1 and result["urls"].count(f"{loop}?page=2") == 1
    assert looped["records"] == 3
    assert "pagination loop: next page was already fetched" in looped["warnings"]
    javascript = result["by_id"][shell]
    assert javascript["records"] == 0 and javascript["extraction_failures"] == ["invalid_response"]
    assert javascript["fetch_failures"] == []  # a usable page, but no listing: not an empty one
    assert not any("careers-app.js" in u for u in result["urls"])  # scripts are never loaded


def test_robots_txt_denial_applies_to_detail_pages(tmp_path: Path) -> None:
    responses = {
        "https://careers.example.com/robots.txt": {
            "body": "User-agent: *\nDisallow: /careers/jobs/112\n",
            "headers": {"Content-Type": "text/plain"},
        },
        LISTING: fixture("listing_page_1.html"),
        job(111): fixture("detail_page_101.html"),
        job(112): fixture("detail_page_101.html"),
    }
    result = crawl(
        tmp_path, [html_source(LISTING, detail=DETAIL, next_page=None, max_pages=None)], responses
    )
    outcome = result["by_id"][LISTING]
    assert job(112) not in result["urls"]  # refused before any request
    assert outcome["titles"] == ["Backend Engineer"]
    assert [f["reason"] for f in outcome["fetch_failures"]] == ["IgnoreRequest"]


def test_html_jobs_are_stored_in_schema_v2_and_exported_unchanged(tmp_path: Path) -> None:
    database = tmp_path / "html.db"
    scenario = {
        "db": str(database),
        "now": "2026-11-16T09:00:00+00:00",
        "sources": [html_source(LISTING, detail=DETAIL)],
        "responses": {
            LISTING: fixture("listing_page_1.html"),
            f"{LISTING}?page=2": fixture("listing_page_2.html"),
            job(111): fixture("detail_page_101.html"),
            job(112): fixture("detail_page_101.html"),
            job(113): fixture("detail_plain_html.html"),
        },
    }
    lines, _ = run_harness(tmp_path, scenario)
    summary = json.loads(next(x for x in lines if x.startswith("SUMMARY:"))[8:])
    assert (summary["status"], summary["unique"], summary["jobs_new"]) == ("SUCCEEDED", 3, 3)
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
        stored = conn.execute("SELECT platform, title FROM jobs ORDER BY title").fetchall()
    assert stored == [
        ("html", "Backend Engineer"),
        ("html", "Frontend Engineer"),
        ("html", "Mobile Engineer"),
    ]

    target = tmp_path / "jobs.xlsx"
    export = export_jobs(database, target, reference=datetime(2026, 11, 16, 12, tzinfo=UTC))
    ws = load_workbook(target)[SHEET_NAME]
    rows = [dict(zip(HEADERS, r, strict=True)) for r in ws.iter_rows(min_row=2, values_only=True)]
    assert export.rows == 3
    assert {r["Platform"] for r in rows} == {"html"}
    assert {r["Job Title"] for r in rows} == {
        "Backend Engineer",
        "Frontend Engineer",
        "Mobile Engineer",
    }
