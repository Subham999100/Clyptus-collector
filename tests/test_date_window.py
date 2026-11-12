from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from collector.pipelines.date_window import DateOutcome, DateWarning, check_date_window
from collector.settings import Settings
from tests.conftest import make_record

IST = ZoneInfo("Asia/Kolkata")
# 2026-03-31 12:00 UTC == 17:30 IST: the same calendar date in both zones.
NOON = datetime(2026, 3, 31, 12, 0, tzinfo=UTC)
# 2026-03-31 19:00 UTC == 2026-04-01 00:30 IST: UTC and IST dates differ.
IST_PAST_MIDNIGHT = datetime(2026, 3, 31, 19, 0, tzinfo=UTC)


def check(posted: str | None, reference: datetime = NOON, **kwargs: object):
    extra = {"posted_date": posted, "posted_date_source": "platform"} if posted else {}
    return check_date_window(make_record(**extra), reference, **kwargs)


@pytest.fixture(autouse=True)
def default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLLECTOR_DATE_WINDOW_DAYS", raising=False)
    monkeypatch.delenv("COLLECTOR_TIMEZONE", raising=False)


def test_unknown_date_retained_and_flagged() -> None:
    result = check(None, window_days=30)
    assert result.outcome is DateOutcome.UNKNOWN_DATE
    assert result.needs_review
    assert not result.eligible


def test_job_within_window_is_eligible() -> None:
    result = check("2026-03-10", window_days=30)
    assert result.outcome is DateOutcome.ELIGIBLE
    assert result.eligible
    assert not result.needs_review
    assert result.warning is None


def test_job_older_than_window_excluded() -> None:
    result = check("2026-02-01", window_days=30)
    assert result.outcome is DateOutcome.OLD_DATE
    assert not result.eligible
    assert not result.needs_review


def test_window_boundaries() -> None:
    assert check("2026-03-01").outcome is DateOutcome.ELIGIBLE  # exactly 30 days old
    assert check("2026-02-28").outcome is DateOutcome.OLD_DATE  # 31 days old
    assert check("2026-03-31").outcome is DateOutcome.ELIGIBLE  # today


def test_custom_window() -> None:
    assert check("2026-03-20", window_days=7).outcome is DateOutcome.OLD_DATE
    assert check("2026-03-20", window_days=11).outcome is DateOutcome.ELIGIBLE


def test_default_window_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert check("2026-02-28").outcome is DateOutcome.OLD_DATE
    monkeypatch.setenv("COLLECTOR_DATE_WINDOW_DAYS", "40")
    assert check("2026-02-28").outcome is DateOutcome.ELIGIBLE


def test_one_day_future_tolerated_with_warning() -> None:
    result = check("2026-04-01")  # tomorrow
    assert result.outcome is DateOutcome.ELIGIBLE
    assert result.eligible
    assert result.warning is DateWarning.FUTURE_WITHIN_TOLERANCE


def test_future_beyond_tolerance_is_flagged() -> None:
    result = check("2026-04-02")  # two days ahead
    assert result.outcome is DateOutcome.FUTURE_DATE
    assert result.needs_review
    assert not result.eligible


def test_collected_at_does_not_affect_outcome_and_posted_date_untouched() -> None:
    record = make_record(
        posted_date="2026-01-01",
        posted_date_source="jsonld",
        collected_at=datetime(2026, 3, 31, tzinfo=UTC),  # collected today, posted long ago
    )
    assert check_date_window(record, NOON, 30).outcome is DateOutcome.OLD_DATE
    assert record.posted_date == date(2026, 1, 1)


def test_default_timezone_is_ist_so_utc_late_evening_is_already_tomorrow() -> None:
    # Reference is 31 Mar in UTC but 1 Apr in IST. 1 Apr is "today" in IST (no warning)...
    assert check("2026-04-01", IST_PAST_MIDNIGHT).warning is None
    # ...but one day ahead when evaluated in UTC.
    result = check("2026-04-01", IST_PAST_MIDNIGHT, tz=UTC)
    assert result.warning is DateWarning.FUTURE_WITHIN_TOLERANCE


def test_midnight_boundary_shifts_window_edges() -> None:
    # In IST today is 1 Apr: 2 Mar is 30 days old (eligible), 1 Mar is 31 (old).
    assert check("2026-03-02", IST_PAST_MIDNIGHT, tz=IST).outcome is DateOutcome.ELIGIBLE
    assert check("2026-03-01", IST_PAST_MIDNIGHT, tz=IST).outcome is DateOutcome.OLD_DATE
    # In UTC today is 31 Mar: 1 Mar is 30 days old (eligible).
    assert check("2026-03-01", IST_PAST_MIDNIGHT, tz=UTC).outcome is DateOutcome.ELIGIBLE


def test_future_tolerance_is_relative_to_business_timezone_date() -> None:
    # IST today = 1 Apr, so 3 Apr is two days ahead (flagged); in UTC (31 Mar) 2 Apr already is.
    assert check("2026-04-03", IST_PAST_MIDNIGHT, tz=IST).outcome is DateOutcome.FUTURE_DATE
    assert check("2026-04-02", IST_PAST_MIDNIGHT, tz=IST).warning is not None
    assert check("2026-04-02", IST_PAST_MIDNIGHT, tz=UTC).outcome is DateOutcome.FUTURE_DATE


def test_timezone_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().timezone == "Asia/Kolkata"
    monkeypatch.setenv("COLLECTOR_TIMEZONE", "UTC")
    assert Settings().timezone == "UTC"
    # With the UTC override the 1 Apr posting is one day ahead of "today" (31 Mar).
    assert check("2026-04-01", IST_PAST_MIDNIGHT).warning is DateWarning.FUTURE_WITHIN_TOLERANCE


def test_invalid_timezone_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLLECTOR_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValidationError):
        Settings()


def test_reference_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        check_date_window(make_record(), datetime(2026, 3, 31, 12, 0), 30)


def test_reference_defaults_to_now() -> None:
    record = make_record(posted_date=datetime.now(UTC).date().isoformat())
    assert check_date_window(record, window_days=30).outcome is DateOutcome.ELIGIBLE
