from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from collector.utils.dates import parse_posted_date

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Today", date(2026, 10, 15)),
        ("today", date(2026, 10, 15)),
        ("Yesterday", date(2026, 10, 14)),
        ("3 days ago", date(2026, 10, 12)),
        ("1 day ago", date(2026, 10, 14)),
        ("2 weeks ago", date(2026, 10, 1)),
        ("Posted 3 days ago", date(2026, 10, 12)),
        ("3 hours ago", date(2026, 10, 15)),
        ("12 Oct 2026", date(2026, 10, 12)),
        ("12 October 2026", date(2026, 10, 12)),
        ("October 12, 2026", date(2026, 10, 12)),
        ("Posted on 12 Oct 2026", date(2026, 10, 12)),
        ("12/10/2026", date(2026, 10, 12)),  # day-first
        ("01/02/2026", date(2026, 2, 1)),  # ambiguous: day-first wins
        ("12-10-2026", date(2026, 10, 12)),
        ("12.10.2026", date(2026, 10, 12)),
        ("10/25/2026", date(2026, 10, 25)),  # only month-first is possible
        ("2026-10-12", date(2026, 10, 12)),
        ("  2026-10-12  ", date(2026, 10, 12)),
    ],
)
def test_parses(text: str, expected: date) -> None:
    assert parse_posted_date(text, NOW) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "soon",
        "recently",
        "new",
        "a few days ago",
        "30+ days ago",
        "2 months ago",
        "last week",
        "12345",
        "2026",
        "Req 2026",
        "Job ID 12/10",
        "12 Oct",
        "Oct 2026",
        "31/02/2026",
        "12/10/26",
        "2026-13-45",
        "2026-02-31",
        "12 foo 2026",
        "99999 days ago",
        "20261012x",
    ],
)
def test_vague_or_invalid_returns_none(text: str) -> None:
    assert parse_posted_date(text, NOW) is None


def test_relative_dates_use_the_timezone_of_now() -> None:
    # 2026-10-14 20:00 UTC is already 15 Oct in IST.
    instant = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)
    assert parse_posted_date("today", instant) == date(2026, 10, 14)
    assert parse_posted_date("today", instant.astimezone(ZoneInfo("Asia/Kolkata"))) == date(
        2026, 10, 15
    )


def test_hours_ago_can_cross_midnight() -> None:
    now = datetime(2026, 10, 15, 1, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert parse_posted_date("3 hours ago", now) == date(2026, 10, 14)


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-12T23:30:00-05:00",
        "2026-10-12T23:30:00-05:00".replace("T", " "),
        "2026-10-12T23:30:00Z",
    ],
)
def test_explicit_source_timezone_keeps_written_date(text: str) -> None:
    # Not converted to the business timezone (where 23:30 -05:00 would already be 13 Oct).
    assert parse_posted_date(text, NOW) == date(2026, 10, 12)


def test_textual_date_with_explicit_timezone_keeps_written_date() -> None:
    assert parse_posted_date("12 Oct 2026 11:30pm EST", NOW) == date(2026, 10, 12)


def test_now_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_posted_date("today", datetime(2026, 10, 15))


def test_is_deterministic() -> None:
    assert {parse_posted_date("3 days ago", NOW) for _ in range(5)} == {date(2026, 10, 12)}
