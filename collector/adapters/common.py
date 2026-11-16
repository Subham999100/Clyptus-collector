"""Pieces shared by the platform extraction adapters (Greenhouse, Lever, Ashby, JSON-LD).

Offline and pure: no network, files, database, logging, environment or clock access. Only what
more than one adapter needs lives here: the result types, configuration and URL checks, optional
ISO posting dates, and HTML/text to snippet conversion. Platform-specific field mapping stays in
each adapter.
"""

import ipaddress
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from collector.models import JobRecord
from collector.pipelines.validate import FieldError, Rejection
from collector.utils.dates import parse_posted_date
from collector.utils.urls import normalize_url

DESCRIPTION_SNIPPET_MAX = 500  # Unicode characters

ResponseType = Literal["listing", "detail"]

_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
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
    INVALID_RESPONSE = "invalid_response"  # payload is not a recognizable platform response
    MISSING_FIELD = "missing_field"  # a required value is absent or blank
    INVALID_URL = "invalid_url"  # present but not a usable public http(s) URL
    INVALID_DATE = "invalid_date"  # a date value is present but unusable
    INVALID_TYPE = "invalid_type"  # a field has an unexpected JSON type


@dataclass(frozen=True)
class ExtractionFailure:
    """One response or one job that could not be extracted.

    ``index`` is the position of the job in the response (``None`` for the whole response, or for
    a single-job response); ``job_id`` is the platform id when it was usable. ``errors`` lists
    every problem found (field names are the *source* keys) and ``kind`` is the kind of the
    first one.
    """

    kind: FailureKind
    index: int | None
    job_id: str | None
    errors: tuple[FieldError, ...]


@dataclass(frozen=True)
class ExtractionWarning:
    """A non-fatal problem with one job: the job was still extracted, minus the bad value.

    ``index`` and ``job_id`` are as in ``ExtractionFailure``; ``field`` is the source key. The
    message never contains source values.
    """

    kind: FailureKind
    index: int | None
    job_id: str | None
    field: str
    message: str


@dataclass(frozen=True)
class SkippedJob:
    """One job deliberately not collected (e.g. Ashby ``isListed: false``): a rule, not a data
    problem. ``index`` and ``job_id`` are as in ``ExtractionFailure``; ``reason`` is a safe
    message that never contains source values."""

    index: int | None
    job_id: str | None
    reason: str


@dataclass(frozen=True)
class Extraction:
    """Outcome of one response: records, failures, skips and warnings, each in source order.

    ``response_type`` is ``None`` when the response was not recognized. ``job_count`` is the number
    of job objects in the response, so ``len(records) + len(failures) + len(skipped)`` equals it
    for a recognized response. ``skipped`` lists jobs deliberately not collected; it stays empty
    for platforms without such a rule. ``warnings`` usually refer to jobs in ``records``; if a job
    also failed for another reason, its warnings are still reported.
    """

    records: tuple[JobRecord, ...]
    failures: tuple[ExtractionFailure, ...]
    response_type: ResponseType | None
    job_count: int
    warnings: tuple[ExtractionWarning, ...] = ()
    skipped: tuple[SkippedJob, ...] = ()


@dataclass(frozen=True)
class Config:
    """Validated trusted configuration. ``scope`` is the platform's board token / site slug."""

    company_name: str
    scope: str
    source_url: str
    collected_at: datetime
    company_website: str | None


# Reports a problem: (source field, kind, safe message). Used for both failures and warnings.
Fail = Callable[[str, FailureKind, str], None]
ExtractJob = Callable[
    [Any, int | None], tuple[JobRecord | ExtractionFailure, tuple[ExtractionWarning, ...]]
]


def check_config(
    company_name: str,
    scope: str,
    scope_label: str,
    source_url: str,
    collected_at: datetime,
    company_website: str | None,
) -> Config:
    """Validate configuration; raise ``ValueError`` (naming ``scope_label``) if it is unusable."""
    if not isinstance(company_name, str) or not company_name.strip():
        raise ValueError("company_name must be a non-blank string")
    if not isinstance(scope, str) or not _SCOPE.fullmatch(scope.strip()):
        raise ValueError(f"{scope_label} must match [A-Za-z0-9][A-Za-z0-9._-]*")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    try:
        clean_source = public_url(source_url)
        clean_website = public_url(company_website) if company_website is not None else None
    except ValueError as exc:
        raise ValueError(f"configuration URL: {exc}") from exc
    return Config(
        company_name=company_name.strip(),
        scope=scope.strip(),
        source_url=clean_source,
        collected_at=collected_at,
        company_website=clean_website,
    )


def public_url(value: Any) -> str:
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


def classify_url(value: Any, name: str) -> tuple[str | None, tuple[FailureKind, str] | None]:
    """Judge one URL field named ``name``.

    Returns ``(url, None)`` when usable (normalized), ``(None, None)`` when absent (missing, null
    or blank) and ``(None, (kind, message))`` when present but unusable. The message never
    contains the value.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if not isinstance(value, str):
        return None, (FailureKind.INVALID_TYPE, f"{name} is {type(value).__name__}")
    try:
        return public_url(value), None
    except ValueError as exc:
        return None, (FailureKind.INVALID_URL, f"{name} is not a public URL: {exc}")


# ISO 8601 date, or date-time with optional offset. Relative or free-text dates are not accepted.
_ISO_DATE_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:[Zz]|[+-]\d{2}(?::?\d{2})?)?)?"
)


def read_iso_date(value: Any, field: str, now: datetime, warn: Fail) -> date | None:
    """An optional ISO 8601 posting date named ``field``. Absent, null or blank means unknown,
    silently. A value that is present but unusable also means unknown, but is reported through
    ``warn`` so the job is kept. The date is the one written in the source, offset not applied
    (same rule as ``parse_posted_date``); relative or free-text dates are never accepted."""
    if value is None:
        return None
    if not isinstance(value, str):
        warn(
            field,
            FailureKind.INVALID_TYPE,
            f"{field} is {type(value).__name__}, expected a string; posting date unknown",
        )
        return None
    text = value.strip()
    if not text:
        return None
    parsed = None
    if _ISO_DATE_TEXT.fullmatch(text):
        parsed = parse_posted_date(text, now)  # ISO text never consults ``now``
    if parsed is None:
        warn(
            field,
            FailureKind.INVALID_DATE,
            f"{field} is not a valid ISO 8601 date; posting date unknown",
        )
    return parsed


def read_text(source: dict[str, Any], key: str, fail: Fail, *, required: bool) -> str | None:
    """A string field. Absent/blank is ``None`` (a failure if ``required``); other types fail."""
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


def collect(
    items: Iterable[tuple[int | None, Any]],
    extract_job: ExtractJob,
    response_type: ResponseType,
    job_count: int,
) -> Extraction:
    """Run ``extract_job`` over every item; one failing job never affects the others."""
    records: list[JobRecord] = []
    failures: list[ExtractionFailure] = []
    warnings: list[ExtractionWarning] = []
    for index, item in items:
        outcome, job_warnings = extract_job(item, index)
        warnings.extend(job_warnings)
        if isinstance(outcome, JobRecord):
            records.append(outcome)
        else:
            failures.append(outcome)
    return Extraction(tuple(records), tuple(failures), response_type, job_count, tuple(warnings))


def rejected_response(message: str) -> Extraction:
    error = FieldError("(response)", FailureKind.INVALID_RESPONSE.value, message)
    failure = ExtractionFailure(FailureKind.INVALID_RESPONSE, None, None, (error,))
    return Extraction((), (failure,), None, 0)


def from_rejection(
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


# --- text ----------------------------------------------------------------------------------


def clean_text(text: str) -> str:
    """Single-line text: control characters removed, NFC-normalized, whitespace collapsed."""
    text = unicodedata.normalize("NFC", without_control_characters(text))
    return " ".join(text.split())


def without_control_characters(text: str) -> str:
    """Drop control characters (NUL, escapes, ...); whitespace controls are kept."""
    return "".join(ch for ch in text if ch.isspace() or unicodedata.category(ch) != "Cc")


def html_to_text(markup: str) -> str:
    """Readable single-line text from HTML. Nothing is executed or fetched; ``script``, ``style``
    and similar elements are dropped with their content; block elements separate words."""
    markup = without_control_characters(markup)  # the parser mishandles NUL bytes
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
    return clean_text("".join(parts))


def make_snippet(text: str) -> str | None:
    """At most ``DESCRIPTION_SNIPPET_MAX`` characters, cut at a word boundary when one exists
    (a single longer word is cut hard). No ellipsis is added. ``text`` must already be clean."""
    if not text:
        return None
    if len(text) <= DESCRIPTION_SNIPPET_MAX:
        return text
    cut = text[:DESCRIPTION_SNIPPET_MAX]
    if text[DESCRIPTION_SNIPPET_MAX].isspace():
        return cut.rstrip()
    head = cut.rpartition(" ")[0]
    return head.rstrip() if head else cut
