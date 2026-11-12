"""Posted-date window check. Uses ``posted_date`` only, never ``collected_at``."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum

from collector.models import JobRecord
from collector.settings import get_settings

FUTURE_TOLERANCE_DAYS = 1


class DateOutcome(StrEnum):
    ELIGIBLE = "ELIGIBLE"  # posted within the window (inclusive of the boundary day)
    UNKNOWN_DATE = "UNKNOWN_DATE"  # no posted date: kept, needs review
    OLD_DATE = "OLD_DATE"  # older than the window: excluded from the eligible set
    FUTURE_DATE = "FUTURE_DATE"  # beyond the future tolerance: kept, flagged as suspicious


class DateWarning(StrEnum):
    FUTURE_WITHIN_TOLERANCE = "FUTURE_WITHIN_TOLERANCE"


@dataclass(frozen=True)
class DateWindowResult:
    outcome: DateOutcome
    warning: DateWarning | None = None

    @property
    def eligible(self) -> bool:
        """True only for jobs that belong in the eligible export set."""
        return self.outcome is DateOutcome.ELIGIBLE

    @property
    def needs_review(self) -> bool:
        return self.outcome in (DateOutcome.UNKNOWN_DATE, DateOutcome.FUTURE_DATE)


def check_date_window(
    record: JobRecord,
    reference: datetime | None = None,
    window_days: int | None = None,
    tz: tzinfo | None = None,
) -> DateWindowResult:
    """Classify ``record.posted_date`` against a window ending at ``reference``.

    ``reference`` (default: now) must be timezone-aware; its calendar date *in the business
    timezone* ``tz`` (default: ``Settings.timezone``, Asia/Kolkata) is "today". With
    ``age = today - posted_date``:

    - ``0 <= age <= window_days``: ELIGIBLE (``window_days`` defaults to ``date_window_days``)
    - ``age > window_days``: OLD_DATE
    - ``-FUTURE_TOLERANCE_DAYS <= age < 0``: ELIGIBLE with a FUTURE_WITHIN_TOLERANCE warning
      (a source in a timezone ahead of ours can legitimately be one calendar day ahead)
    - further in the future: FUTURE_DATE
    - no posted date: UNKNOWN_DATE (never replaced by the collection date)

    The record is never modified.
    """
    if window_days is None:
        window_days = get_settings().date_window_days
    if tz is None:
        tz = get_settings().tz
    if reference is None:
        reference = datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("reference must be timezone-aware")

    posted = record.posted_date
    if posted is None:
        return DateWindowResult(DateOutcome.UNKNOWN_DATE)

    today = reference.astimezone(tz).date()
    if posted > today + timedelta(days=FUTURE_TOLERANCE_DAYS):
        return DateWindowResult(DateOutcome.FUTURE_DATE)
    if posted > today:
        return DateWindowResult(DateOutcome.ELIGIBLE, DateWarning.FUTURE_WITHIN_TOLERANCE)
    if posted < today - timedelta(days=window_days):
        return DateWindowResult(DateOutcome.OLD_DATE)
    return DateWindowResult(DateOutcome.ELIGIBLE)
