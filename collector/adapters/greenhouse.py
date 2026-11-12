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
import ipaddress
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from collector.models import JobRecord, WorkType
from collector.pipelines.validate import FieldError, Rejection, validate_job
from collector.utils.dates import parse_posted_date
from collector.utils.urls import normalize_url

PLATFORM = "greenhouse"
DESCRIPTION_SNIPPET_MAX = 500  # Unicode characters

ResponseType = Literal["listing", "detail"]

_BOARD_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
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
# Content that is removed with everything inside it (never rendered, never loaded).
_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "object",
    "embed",
    "svg",
    "canvas",
    "head",
)
# Elements that separate words even though the source has no whitespace between them.
_BLOCK_TAGS = frozenset(
    "address article aside blockquote br dd details div dl dt fieldset figcaption figure footer "
    "form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section summary table tbody td tfoot "
    "th thead tr ul".split()
)
_SPACE = object()


class FailureKind(StrEnum):
    INVALID_RESPONSE = "invalid_response"  # payload is not a recognizable Greenhouse response
    MISSING_FIELD = "missing_field"  # a required value is absent or blank
    INVALID_URL = "invalid_url"  # present but not a usable public http(s) URL
    INVALID_DATE = "invalid_date"  # first_published is a string but not a valid ISO 8601 date
    INVALID_TYPE = "invalid_type"  # a field has an unexpected JSON type


@dataclass(frozen=True)
class ExtractionFailure:
    """One response or one job that could not be extracted.

    ``index`` is the position in ``jobs`` (``None`` for the whole response, or for a detail
    response); ``job_id`` is the Greenhouse id when it was usable. ``errors`` lists every problem
    found (field names are the *source* keys, e.g. ``absolute_url``, ``location.name``) and
    ``kind`` is the kind of the first one.
    """

    kind: FailureKind
    index: int | None
    job_id: str | None
    errors: tuple[FieldError, ...]


@dataclass(frozen=True)
class ExtractionWarning:
    """A non-fatal problem with one job: the job was still extracted, minus the bad value.

    Today only ``first_published`` is recoverable (``invalid_date`` or ``invalid_type``): the job
    is kept with an unknown posting date. ``index`` and ``job_id`` are as in ``ExtractionFailure``;
    ``field`` is the source key. The message never contains source values.
    """

    kind: FailureKind
    index: int | None
    job_id: str | None
    field: str
    message: str


@dataclass(frozen=True)
class GreenhouseExtraction:
    """Outcome of one response: records, failures and warnings, each in source order.

    ``response_type`` is ``None`` when the response was not recognized. ``job_count`` is the number
    of job objects in the response, so ``len(records) + len(failures)`` equals it for a recognized
    response. ``warnings`` usually refer to jobs in ``records``; if a job also failed for another
    reason, its warnings are still reported.
    """

    records: tuple[JobRecord, ...]
    failures: tuple[ExtractionFailure, ...]
    response_type: ResponseType | None
    job_count: int
    warnings: tuple[ExtractionWarning, ...] = ()


@dataclass(frozen=True)
class _Config:
    company_name: str
    board_token: str
    source_url: str
    collected_at: datetime
    company_website: str | None


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
    config = _check_config(company_name, board_token, source_url, collected_at, company_website)

    if not isinstance(payload, dict):
        return _rejected_response(f"response is {type(payload).__name__}, expected an object")

    if "jobs" in payload:
        jobs = payload["jobs"]
        if not isinstance(jobs, list):
            return _rejected_response(f"'jobs' is {type(jobs).__name__}, expected a list")
        items: list[tuple[int | None, Any]] = list(enumerate(jobs))
        response_type: ResponseType = "listing"
    elif {"id", "title", "absolute_url"} & payload.keys():
        items = [(None, payload)]
        response_type = "detail"
    else:
        return _rejected_response("response has neither a 'jobs' list nor job fields")

    records: list[JobRecord] = []
    failures: list[ExtractionFailure] = []
    warnings: list[ExtractionWarning] = []
    for index, item in items:
        outcome, job_warnings = _extract_job(item, index, config)
        warnings.extend(job_warnings)
        if isinstance(outcome, JobRecord):
            records.append(outcome)
        else:
            failures.append(outcome)
    return GreenhouseExtraction(
        tuple(records), tuple(failures), response_type, len(items), tuple(warnings)
    )


# --- configuration -------------------------------------------------------------------------


def _check_config(
    company_name: str,
    board_token: str,
    source_url: str,
    collected_at: datetime,
    company_website: str | None,
) -> _Config:
    if not isinstance(company_name, str) or not company_name.strip():
        raise ValueError("company_name must be a non-blank string")
    if not isinstance(board_token, str) or not _BOARD_TOKEN.fullmatch(board_token.strip()):
        raise ValueError("board_token must match [A-Za-z0-9][A-Za-z0-9._-]*")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    try:
        clean_source = _public_url(source_url)
        clean_website = _public_url(company_website) if company_website is not None else None
    except ValueError as exc:
        raise ValueError(f"configuration URL: {exc}") from exc
    return _Config(
        company_name=company_name.strip(),
        board_token=board_token.strip(),
        source_url=clean_source,
        collected_at=collected_at,
        company_website=clean_website,
    )


def _public_url(value: Any) -> str:
    """Return the normalized http(s) URL, or raise ``ValueError`` (without echoing the value).

    Rejects non-http(s) schemes, relative URLs, embedded credentials, single-label hosts,
    ``localhost`` and non-global IP addresses. Nothing is fetched.
    """
    if not isinstance(value, str):
        raise ValueError("not a string")
    text = value.strip()
    if not text or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in text):
        raise ValueError("blank or contains whitespace/control characters")
    parts = urlsplit(text)
    host = parts.hostname
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ValueError("not an absolute http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("contains credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host == "localhost" or host.endswith(".localhost"):
            raise ValueError("host is not public") from None
    else:
        if not address.is_global:
            raise ValueError("host is not public")
    return normalize_url(text)  # raises ValueError on a malformed port


# --- one job -------------------------------------------------------------------------------


def _extract_job(
    item: Any, index: int | None, config: _Config
) -> tuple[JobRecord | ExtractionFailure, tuple[ExtractionWarning, ...]]:
    if not isinstance(item, dict):
        error = FieldError("(job)", FailureKind.INVALID_TYPE.value, f"job is {type(item).__name__}")
        return ExtractionFailure(FailureKind.INVALID_TYPE, index, None, (error,)), ()

    errors: list[FieldError] = []
    warnings: list[ExtractionWarning] = []

    def fail(field: str, kind: FailureKind, message: str) -> None:
        errors.append(FieldError(field, kind.value, message))

    job_id = _read_id(item.get("id"), fail)
    title = _read_text(item, "title", fail, required=True)
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
            "external_id": f"{config.board_token}:{job_id}" if job_id else None,
            "description_snippet": description,
        }
    )
    if isinstance(result, JobRecord):
        return result, tuple(warnings)
    return _from_rejection(result, index, job_id), tuple(warnings)


def _from_rejection(
    rejection: Rejection, index: int | None, job_id: str | None
) -> ExtractionFailure:
    """Backstop: Phase 1 validation refused a record our own checks accepted (e.g. a URL longer
    than the model allows). Only the validator's field/code/message are kept, never the input."""
    errors = tuple(FieldError(e.field, _kind_for(e).value, e.message) for e in rejection.errors)
    return ExtractionFailure(FailureKind(errors[0].code), index, job_id, errors)


def _kind_for(error: FieldError) -> FailureKind:
    if error.code in ("missing", "string_too_short"):
        return FailureKind.MISSING_FIELD
    if error.code.startswith("url"):
        return FailureKind.INVALID_URL
    if "date" in error.code:
        return FailureKind.INVALID_DATE
    return FailureKind.INVALID_TYPE


Fail = Callable[[str, FailureKind, str], None]  # also the shape of the ``warn`` callback


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


def _read_text(source: dict[str, Any], key: str, fail: Fail, *, required: bool) -> str | None:
    value = source.get(key)
    if value is None:
        if required:
            fail(key, FailureKind.MISSING_FIELD, f"{key} is missing")
        return None
    if not isinstance(value, str):
        fail(key, FailureKind.INVALID_TYPE, f"{key} is {type(value).__name__}, expected a string")
        return None
    if not value.strip():
        if required:
            fail(key, FailureKind.MISSING_FIELD, f"{key} is blank")
        return None
    return value


def _read_url(value: Any, fail: Fail) -> str | None:
    """``absolute_url`` is the only documented job link, and the board hosts the application
    form behind it, so it is the apply URL. No URL is ever built from other fields."""
    if value is None or (isinstance(value, str) and not value.strip()):
        fail("absolute_url", FailureKind.MISSING_FIELD, "absolute_url is missing")
        return None
    if not isinstance(value, str):
        fail("absolute_url", FailureKind.INVALID_TYPE, f"absolute_url is {type(value).__name__}")
        return None
    try:
        return _public_url(value)
    except ValueError as exc:
        fail("absolute_url", FailureKind.INVALID_URL, f"absolute_url is not a public URL: {exc}")
        return None


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


# --- description ---------------------------------------------------------------------------


def _read_description(value: Any, fail: Fail) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        fail("content", FailureKind.INVALID_TYPE, f"content is {type(value).__name__}")
        return None
    return _snippet(_html_to_text(_decode_content(value)))


def _decode_content(content: str) -> str:
    """Greenhouse ``content`` is HTML that has been entity-encoded (``&lt;p&gt;``): decode it
    once to get markup. Content that already contains literal tags is left as it is, so its own
    entities are decoded exactly once, by the HTML parser."""
    return content if "<" in content else html.unescape(content)


def _html_to_text(markup: str) -> str:
    """Readable single-line text from HTML. Nothing is executed or fetched; ``script``, ``style``
    and similar elements are dropped with their content; block elements separate words."""
    markup = _without_control_characters(markup)  # the parser mishandles NUL bytes
    if not markup.strip():
        return ""
    tree = HTMLParser(markup)
    tree.strip_tags(list(_DROP_TAGS))
    root = tree.body or tree.root
    if root is None:
        return ""
    parts: list[str] = []
    stack: list[Any] = [root]  # iterative: hostile nesting depth cannot overflow the stack
    while stack:
        node = stack.pop()
        if node is _SPACE:
            parts.append(" ")
            continue
        tag = node.tag
        if tag == "-text":
            parts.append(node.text_content or "")
        elif not tag.startswith(("-", "_")):  # skip comments, doctype, processing instructions
            if tag in _BLOCK_TAGS:
                parts.append(" ")
                stack.append(_SPACE)
            stack.extend(reversed(list(node.iter(include_text=True))))
    text = unicodedata.normalize("NFC", _without_control_characters("".join(parts)))
    return " ".join(text.split())


def _without_control_characters(text: str) -> str:
    """Drop control characters (NUL, escapes, ...); whitespace controls are kept."""
    return "".join(ch for ch in text if ch.isspace() or unicodedata.category(ch) != "Cc")


def _snippet(text: str) -> str | None:
    """At most ``DESCRIPTION_SNIPPET_MAX`` characters, cut at a word boundary when one exists
    (a single longer word is cut hard). No ellipsis is added."""
    if not text:
        return None
    if len(text) <= DESCRIPTION_SNIPPET_MAX:
        return text
    cut = text[:DESCRIPTION_SNIPPET_MAX]
    if text[DESCRIPTION_SNIPPET_MAX].isspace():
        return cut.rstrip()
    head = cut.rpartition(" ")[0]
    return head.rstrip() if head else cut


def _rejected_response(message: str) -> GreenhouseExtraction:
    error = FieldError("(response)", FailureKind.INVALID_RESPONSE.value, message)
    failure = ExtractionFailure(FailureKind.INVALID_RESPONSE, None, None, (error,))
    return GreenhouseExtraction((), (failure,), None, 0)
