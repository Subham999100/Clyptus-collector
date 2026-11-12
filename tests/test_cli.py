import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from collector.cli import app
from collector.settings import Settings, get_settings

runner = CliRunner()

COMMANDS = ["run", "export", "report"]


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


@pytest.mark.parametrize("command", COMMANDS)
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
