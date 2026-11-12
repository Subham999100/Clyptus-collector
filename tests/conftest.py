import socket
from datetime import UTC, datetime
from typing import Any

import pytest

from collector.models import JobRecord
from collector.pipelines.validate import Rejection, validate_job

COLLECTED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def make_raw(**overrides: Any) -> dict[str, Any]:
    """Raw extracted-job dict with synthetic test values (not real job data)."""
    raw: dict[str, Any] = {
        "title": "Test Engineer",
        "company_name": "Example Corp",
        "apply_url": "https://jobs.example.com/apply/123",
        "source_url": "https://jobs.example.com/careers",
        "platform": "testboard",
        "collected_at": COLLECTED_AT,
    }
    raw.update(overrides)
    return raw


def make_record(**overrides: Any) -> JobRecord:
    result = validate_job(make_raw(**overrides))
    assert not isinstance(result, Rejection), result
    return result


@pytest.fixture
def raw_job() -> dict[str, Any]:
    return make_raw()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
