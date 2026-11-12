"""Greenhouse Job Board API extraction: decoded JSON in, ``JobRecord`` objects out.

Offline and pure. No network, files, database, logging, environment or clock access: the caller
fetches and decodes the response and supplies every other input, including the collection time.
The input payload is never mutated.

Supported shapes (see ``docs/platform-field-mapping.md``):

- listing: ``{"jobs": [...], "meta": {...}}`` (``GET /v1/boards/{token}/jobs``)
- detail: one job object (``GET /v1/boards/{token}/jobs/{id}``)

Each job is extracted independently. A malformed job becomes an ``ExtractionFailure`` and never
stops the others; nothing is dropped silently. Optional values the source does not provide stay
``None`` and nothing is guessed. Failure messages never echo source values or HTML.
"""

import html
import re
from datetime import date, datetime
from typing import Any

from collector.adapters.common import (
    DESCRIPTION_SNIPPET_MAX,
    Config,
    Extraction,
    ExtractionFailure,
    ExtractionWarning,
    Fail,
    FailureKind,
    ResponseType,
    check_config,
    classify_url,
    collect,
    from_rejection,
    html_to_text,
    make_snippet,
    read_text,
    rejected_response,
)
from collector.models import JobRecord, WorkType
from collector.pipelines.validate import FieldError, validate_job
from collector.utils.dates import parse_posted_date

__all__ = [
    "DESCRIPTION_SNIPPET_MAX",
    "ExtractionFailure",
    "ExtractionWarning",
    "FailureKind",
    "GreenhouseExtraction",
    "extract_greenhouse_jobs",
]

PLATFORM = "greenhouse"

GreenhouseExtraction = Extraction

_ID_TEXT = re.compile(r"\d+")
# ISO 8601 date, or date-time with optional offset. Relative or free-text dates are not accepted.
_ISO_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:[Zz]|[+-]\d{2}(?::?\d{2})?)?)?"
)
# A location is "explicitly remote" only if every ';'-separated part starts with the word
# "remote" as a whole: "Remote", "Remote - India", "Remote (EMEA)", "Remote, Spain".
_REMOTE_PART = re.compile(r"remote(?:\s+[-–—]\s+\S.*|\s*[,(:]\s*\S.*)?", re.IGNORECASE)
_NOT_REMOTE_WORDS = re.compile(r"hybrid|on-?site|in-office", re.IGNORECASE)


def extract_greenhouse_jobs(
    payload: Any,
    *,
    company_name: str,
    board_token: str,
    source_url: str,
    collected_at: datetime,
    company_website: str | None = None,
) -> GreenhouseExtraction:
    """Convert a decoded Greenhouse Job Board response into ``JobRecord`` objects.

    Args:
        payload: the decoded JSON (listing or single-job detail). Not modified.
        company_name: from trusted configuration. The response's own ``company_name`` (detail
            only) is deliberately ignored.
        board_token: the Greenhouse board token the response was requested from. Scopes job
            identity: ``external_id`` is ``"<board_token>:<id>"``.
        source_url: the public URL that was requested; becomes ``JobRecord.source_url``.
        collected_at: timezone-aware collection time (the adapter never reads the clock).
        company_website: optional configured website.

    Raises:
        ValueError: for invalid configuration (blank name, malformed board token, non-public
            URL, naive ``collected_at``). Problems in the *response* never raise; they are
            returned as failures.
    """
    config = check_config(
        company_name, board_token, "board_token", source_url, collected_at, company_website
    )

    if not isinstance(payload, dict):
        return rejected_response(f"response is {type(payload).__name__}, expected an object")

    if "jobs" in payload:
        jobs = payload["jobs"]
        if not isinstance(jobs, list):
            return rejected_response(f"'jobs' is {type(jobs).__name__}, expected a list")
        items: list[tuple[int | None, Any]] = list(enumerate(jobs))
        response_type: ResponseType = "listing"
    elif {"id", "title", "absolute_url"} & payload.keys():
        items = [(None, payload)]
        response_type = "detail"
    else:
        return rejected_response("response has neither a 'jobs' list nor job fields")

    return collect(
        items, lambda item, index: _extract_job(item, index, config), response_type, len(items)
    )


# --- one job -------------------------------------------------------------------------------


def _extract_job(
    item: Any, index: int | None, config: Config
) -> tuple[JobRecord | ExtractionFailure, tuple[ExtractionWarning, ...]]:
    if not isinstance(item, dict):
        error = FieldError("(job)", FailureKind.INVALID_TYPE.value, f"job is {type(item).__name__}")
        return ExtractionFailure(FailureKind.INVALID_TYPE, index, None, (error,)), ()

    errors: list[FieldError] = []
    warnings: list[ExtractionWarning] = []

    def fail(field: str, kind: FailureKind, message: str) -> None:
        errors.append(FieldError(field, kind.value, message))

    job_id = _read_id(item.get("id"), fail)
    title = read_text(item, "title", fail, required=True)
    apply_url = _read_url(item.get("absolute_url"), fail)
    location = _read_location(item.get("location"), fail)
    description = _read_description(item.get("content"), fail)

    def warn(field: str, kind: FailureKind, message: str) -> None:
        warnings.append(ExtractionWarning(kind, index, job_id, field, message))

    posted = _read_date(item.get("first_published"), config.collected_at, warn)

    if errors:
        failure = ExtractionFailure(FailureKind(errors[0].code), index, job_id, tuple(errors))
        return failure, tuple(warnings)

    result = validate_job(
        {
            "title": " ".join(title.split()) if title else title,
            "company_name": config.company_name,
            "apply_url": apply_url,
            "source_url": config.source_url,
            "platform": PLATFORM,
            "collected_at": config.collected_at,
            "company_website": config.company_website,
            "location": location,
            "work_type": _explicit_work_type(location),
            "posted_date": posted,
            "posted_date_source": "platform" if posted else "unknown",
            "external_id": f"{config.scope}:{job_id}" if job_id else None,
            "description_snippet": description,
        }
    )
    if isinstance(result, JobRecord):
        return result, tuple(warnings)
    return from_rejection(result, index, job_id), tuple(warnings)


def _read_id(value: Any, fail: Fail) -> str | None:
    """Greenhouse ``id`` is an integer. Absent means "no stable id" (identity falls back to the
    URL); a present but unusable id is a failure rather than a silent downgrade."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    if isinstance(value, str) and _ID_TEXT.fullmatch(value.strip()):
        return str(int(value.strip()))
    fail("id", FailureKind.INVALID_TYPE, f"id is {type(value).__name__}, expected an integer")
    return None


def _read_url(value: Any, fail: Fail) -> str | None:
    """``absolute_url`` is the only documented job link, and the board hosts the application
    form behind it, so it is the apply URL. No URL is ever built from other fields."""
    url, problem = classify_url(value, "absolute_url")
    if url is None:
        kind, message = problem or (FailureKind.MISSING_FIELD, "absolute_url is missing")
        fail("absolute_url", kind, message)
    return url


def _read_location(value: Any, fail: Fail) -> str | None:
    """``location.name`` verbatim (whitespace collapsed). It is free text and may already list
    several places (``"A; B"``); it is stored unsplit, in source order. ``offices`` is unused."""
    if value is None:
        return None
    if not isinstance(value, dict):
        fail("location", FailureKind.INVALID_TYPE, f"location is {type(value).__name__}")
        return None
    name = value.get("name")
    if name is None:
        return None
    if not isinstance(name, str):
        fail("location.name", FailureKind.INVALID_TYPE, f"location.name is {type(name).__name__}")
        return None
    return " ".join(name.split()) or None


def _explicit_work_type(location: str | None) -> WorkType | None:
    """Greenhouse has no workplace-type field. ``remote`` is set only when the location text
    itself is an explicit remote marker; hybrid and on-site are never inferred."""
    if not location or _NOT_REMOTE_WORDS.search(location):
        return None
    parts = [part.strip() for part in location.split(";")]
    return "remote" if all(_REMOTE_PART.fullmatch(part) for part in parts) else None


def _read_date(value: Any, now: datetime, warn: Fail) -> date | None:
    """Only ``first_published`` is a posting date; ``updated_at`` (last modified) and the
    collection time are never substituted. Absent, null or blank means unknown, silently. A value
    that is present but unusable also means unknown, but is reported through ``warn`` so the job
    is kept. The date is the one written in the source, offset not applied (same rule as
    ``parse_posted_date``)."""
    if value is None:
        return None
    if not isinstance(value, str):
        warn(
            "first_published",
            FailureKind.INVALID_TYPE,
            f"first_published is {type(value).__name__}, expected a string; posting date unknown",
        )
        return None
    text = value.strip()
    if not text:
        return None
    parsed = None
    if _ISO_DATE.fullmatch(text):
        parsed = parse_posted_date(text, now)  # ISO text never consults ``now``
    if parsed is None:
        warn(
            "first_published",
            FailureKind.INVALID_DATE,
            "first_published is not a valid ISO 8601 date; posting date unknown",
        )
    return parsed


def _read_description(value: Any, fail: Fail) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        fail("content", FailureKind.INVALID_TYPE, f"content is {type(value).__name__}")
        return None
    return make_snippet(html_to_text(_decode_content(value)))


def _decode_content(content: str) -> str:
    """Greenhouse ``content`` is HTML that has been entity-encoded (``&lt;p&gt;``): decode it
    once to get markup. Content that already contains literal tags is left as it is, so its own
    entities are decoded exactly once, by the HTML parser."""
    return content if "<" in content else html.unescape(content)
