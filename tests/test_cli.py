import json
import socket
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from collector.adapters.common import Extraction
from collector.adapters.greenhouse import extract_greenhouse_jobs
from collector.cli import app
from collector.settings import Settings, get_settings
from collector.spiders.platforms import FetchFailure
from collector.spiders.run import SourceResult
from collector.spiders.sources import Source

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"

COMMANDS = ["run", "export", "report"]
PLACEHOLDER_COMMANDS = ["export", "report"]  # `run` is implemented (Phase 4)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any COLLECTOR_* variables from the developer's environment."""
    import os

    for name in list(os.environ):
        if name.startswith("COLLECTOR_"):
            monkeypatch.delenv(name)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    for command in COMMANDS:
        assert command in result.output


@pytest.mark.parametrize("command", PLACEHOLDER_COMMANDS)
def test_placeholder_commands_do_nothing(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    import scrapy.crawler

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("crawler started")

    monkeypatch.setattr(scrapy.crawler.CrawlerProcess, "__init__", fail)
    monkeypatch.setattr(scrapy.crawler.CrawlerRunner, "__init__", fail)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command])

    assert result.exit_code != 0
    assert "not implemented" in result.output
    assert list(tmp_path.iterdir()) == []


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.date_window_days == 30
    assert settings.timezone == "Asia/Kolkata"
    assert settings.robots_txt_obey is True
    assert settings.concurrent_requests_per_domain == 1
    assert settings.concurrent_requests == 4
    assert settings.request_timeout_seconds == 20
    assert settings.retry_times == 2
    assert settings.download_delay_seconds == 1.0
    assert settings.autothrottle_enabled is True
    assert settings.sqlite_path == Path("data/collector.db")
    assert settings.output_dir == Path("output")
    assert settings.log_level == "INFO"


def test_settings_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COLLECTOR_DATE_WINDOW_DAYS", "7")
    monkeypatch.setenv("COLLECTOR_ROBOTS_TXT_OBEY", "false")
    monkeypatch.setenv("COLLECTOR_CONCURRENT_REQUESTS", "8")
    monkeypatch.setenv("COLLECTOR_SQLITE_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("COLLECTOR_LOG_LEVEL", "DEBUG")

    settings = get_settings()

    assert settings.date_window_days == 7
    assert settings.robots_txt_obey is False
    assert settings.concurrent_requests == 8
    assert settings.sqlite_path == tmp_path / "x.db"
    assert settings.log_level == "DEBUG"


def test_loading_settings_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings()
    assert list(tmp_path.iterdir()) == []


def test_importing_modules_has_no_side_effects(tmp_path: Path) -> None:
    """Import every module in a fresh interpreter with sockets blocked and an empty cwd."""
    script = textwrap.dedent(
        """
        import importlib
        import pkgutil
        import socket

        def blocked(*args, **kwargs):
            raise RuntimeError("network access attempted")

        socket.socket.connect = blocked
        socket.create_connection = blocked

        import collector

        for info in pkgutil.walk_packages(collector.__path__, "collector."):
            importlib.import_module(info.name)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


# --- `collector run` (Phase 4): the crawl itself is replaced by a stub; nothing is fetched ----

SOURCES_HEADER = "platform,company_name,identifier,company_website\n"
EMPTY_LISTING = Extraction((), (), "listing", 0)  # a recognized listing with no jobs


@pytest.fixture
def crawl_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[Source]]:
    """Replace the crawl with a stub that records its sources (no crawl is ever real here)."""
    calls: list[list[Source]] = []

    def fake(sources: Any, **kwargs: Any) -> list[SourceResult]:
        configured = list(sources)
        calls.append(configured)
        return [SourceResult(source, (EMPTY_LISTING,), ()) for source in configured]

    monkeypatch.setattr("collector.cli.crawl_sources", fake)
    return calls


def write_sources(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_run_help_documents_the_sources_option() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--sources" in result.output


def test_run_requires_the_sources_option(crawl_calls: list[list[Source]]) -> None:
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert crawl_calls == []


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "lacks column"),
        (SOURCES_HEADER, "no sources configured"),
        (SOURCES_HEADER + "workday,Example Corp,board,\n", "platform must be one of"),
        (SOURCES_HEADER + "jsonld,Example Corp,http://jobs.example.org/a,\n", "https"),
        (SOURCES_HEADER + "lever,A,site,\nlever,B,Site,\n", "duplicate source"),
        (SOURCES_HEADER + "lever,A,site,,extra\n", "more fields"),
    ],
)
def test_run_rejects_an_unusable_sources_file_before_crawling(
    tmp_path: Path, crawl_calls: list[list[Source]], text: str, fragment: str
) -> None:
    result = runner.invoke(
        app,
        ["run", "--sources", str(write_sources(tmp_path, text)), "--db", str(tmp_path / "t.db")],
    )

    assert result.exit_code == 2
    assert result.output.startswith("error:")
    assert fragment in result.output
    assert crawl_calls == []


def test_run_reports_a_missing_sources_file(
    tmp_path: Path, crawl_calls: list[list[Source]]
) -> None:
    result = runner.invoke(app, ["run", "--sources", str(tmp_path / "missing.csv")])
    assert result.exit_code == 2
    assert result.output.startswith("error:")
    assert crawl_calls == []


def test_run_passes_the_configured_sources_in_order_and_succeeds_when_clean(
    tmp_path: Path, crawl_calls: list[list[Source]]
) -> None:
    path = write_sources(
        tmp_path, SOURCES_HEADER + "lever,Example Two,site-b,\ngreenhouse,Example One,board-a,\n"
    )

    result = runner.invoke(app, ["run", "--sources", str(path), "--db", str(tmp_path / "t.db")])

    assert result.exit_code == 0
    assert crawl_calls == [
        [Source("lever", "Example Two", "site-b"), Source("greenhouse", "Example One", "board-a")]
    ]
    assert "lever Example Two: 0 records" in result.output
    assert "TOTAL: 0 unique records" in result.output


def test_run_reports_blocked_sources_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = Source("lever", "Good Corp", "good-site")
    blocked = Source("greenhouse", "Blocked Corp", "blocked-board")
    url = "https://boards-api.greenhouse.io/v1/boards/x/jobs"
    failure = FetchFailure(url, "HTTP 403", True)
    monkeypatch.setattr(
        "collector.cli.crawl_sources",
        lambda sources, **kwargs: [
            SourceResult(blocked, (), (failure,)),
            SourceResult(good, (EMPTY_LISTING,), ()),
        ],
    )
    path = write_sources(tmp_path, SOURCES_HEADER + "greenhouse,Blocked Corp,blocked-board,\n")

    result = runner.invoke(app, ["run", "--sources", str(path), "--db", str(tmp_path / "t.db")])

    assert result.exit_code == 1
    assert "greenhouse Blocked Corp" in result.output and "(BLOCKED)" in result.output
    assert f"blocked: HTTP 403 ({url})" in result.output
    assert "lever Good Corp: 0 records" in result.output  # the other source is still reported
    assert "TOTAL:" in result.output


def test_run_exits_nonzero_for_a_plain_fetch_failure_without_calling_it_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Source("lever", "Example Corp", "site")
    monkeypatch.setattr(
        "collector.cli.crawl_sources",
        lambda sources, **kwargs: [
            SourceResult(source, (), (FetchFailure("https://api.lever.co/x", "HTTP 503"),))
        ],
    )
    path = write_sources(tmp_path, SOURCES_HEADER + "lever,Example Corp,site,\n")

    result = runner.invoke(app, ["run", "--sources", str(path), "--db", str(tmp_path / "t.db")])

    assert result.exit_code == 1
    assert "fetch error: HTTP 503" in result.output
    assert "BLOCKED" not in result.output


def test_run_output_reports_counts_not_job_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Source("greenhouse", "Example Corp", "example-board")
    payload = json.loads((FIXTURES / "greenhouse" / "list_complete.json").read_text("utf-8"))
    extraction = extract_greenhouse_jobs(
        payload,
        company_name="Example Corp",
        board_token="example-board",
        source_url="https://boards-api.greenhouse.io/v1/boards/example-board/jobs",
        collected_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "collector.cli.crawl_sources",
        lambda sources, **kwargs: [SourceResult(source, (extraction,), ())],
    )
    path = write_sources(tmp_path, SOURCES_HEADER + "greenhouse,Example Corp,example-board,\n")

    result = runner.invoke(app, ["run", "--sources", str(path), "--db", str(tmp_path / "t.db")])

    assert result.exit_code == 0
    assert "greenhouse Example Corp: 1 records, 0 failures" in result.output
    assert "TOTAL: 1 unique records" in result.output
    assert extraction.records[0].title not in result.output  # counts only; no job content
