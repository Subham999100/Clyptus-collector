"""Parse posted-date text (e.g. "3 days ago", "12 Oct 2026") into a ``date``.

Returns ``None`` whenever the text is vague, ambiguous or not a date; a date is never guessed.
No clock is read: relative expressions resolve against the ``now`` you pass in.
"""

import re
from datetime import date, datetime, timedelta

_PREFIX = re.compile(r"^posted(?:\s+on)?\s+")
_RELATIVE = re.compile(r"(\d{1,4})\s+(minute|hour|day|week)s?\s+ago")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}(?:[t ]\S.*)?")
_NUMERIC = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_MONTH_NAME = re.compile(r"[a-z]{3,}")
_YEAR = re.compile(r"\b\d{4}\b")
_UNITS = {"minute": "minutes", "hour": "hours", "day": "days", "week": "weeks"}


def parse_posted_date(text: str, now: datetime) -> date | None:
    """Parse ``text`` into a calendar date, or return ``None`` if it cannot be done reliably.

    Supported: ``today``, ``yesterday``, ``N minutes/hours/days/weeks ago``, ISO dates
    (``2026-10-12``, optionally with a time and offset), numeric ``dd/mm/yyyy`` (also ``-`` and
    ``.``), and textual dates with a day, month name and 4-digit year (``12 Oct 2026``,
    ``October 12, 2026``). A leading ``Posted`` / ``Posted on`` is ignored.

    - Ambiguous numeric dates are read day-first (``12/10/2026`` is 12 October). The month-first
      reading is used only when day-first is impossible (``10/25/2026``).
    - Relative expressions use the calendar date of ``now`` in its own timezone, so pass ``now``
      converted to the business timezone. ``months``/``years ago``, ``30+ days ago`` and
      similar vague phrases return ``None``.
    - If the text carries an explicit timezone/offset, the date is the one written in the
      source (``2026-10-12T23:30:00-05:00`` is 12 October); it is not converted.

    Raises:
        ValueError: if ``now`` is not timezone-aware.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cleaned = _PREFIX.sub("", " ".join(text.lower().split()))
    if not cleaned:
        return None

    if cleaned == "today":
        return now.date()
    if cleaned == "yesterday":
        return now.date() - timedelta(days=1)

    if match := _RELATIVE.fullmatch(cleaned):
        try:
            return (now - timedelta(**{_UNITS[match[2]]: int(match[1])})).date()
        except OverflowError:
            return None

    if _ISO.fullmatch(cleaned):
        try:
            return datetime.fromisoformat(cleaned.upper()).date()  # 'T' and 'Z' need upper case
        except ValueError:
            return None

    if match := _NUMERIC.fullmatch(cleaned):
        first, second, year = (int(g) for g in match.groups())
        for day, month in ((first, second), (second, first)):  # day-first preferred
            try:
                return date(year, month, day)
            except ValueError:
                continue
        return None

    if _MONTH_NAME.search(cleaned) and _YEAR.search(cleaned):
        return _parse_textual(cleaned)
    return None


def _parse_textual(text: str) -> date | None:
    import dateparser  # imported lazily: it is slow to import

    parsed = dateparser.parse(
        text, languages=["en"], settings={"DATE_ORDER": "DMY", "STRICT_PARSING": True}
    )
    return parsed.date() if parsed else None
