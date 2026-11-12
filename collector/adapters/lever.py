"""Lever Postings API extraction: decoded JSON in, ``JobRecord`` objects out.

Offline and pure. No network, files, database, logging, environment or clock access: the caller
fetches and decodes the response and supplies every other input, including the collection time.
The input payload is never mutated.

Supported shape (see ``docs/platform-field-mapping.md``): the list response of
``GET /v0/postings/{site}?mode=json``, a JSON array with one object per posting. Lever has no
separate single-posting shape in our fixtures, and the documentation does not show a response
wrapper, so anything other than an array is an invalid response.

Each posting is extracted independently. A malformed posting becomes an ``ExtractionFailure`` and
never stops the others; nothing is dropped silently. Optional values the source does not provide
stay ``None`` and nothing is guessed. Messages never echo source values or HTML.

Posting dates: **none are extracted.** Every record has ``posted_date=None`` and
``posted_date_source="unknown"``. Lever's public README documents no publication-date field;
``createdAt`` is not documented at all (its unit and meaning are third-party claims only), so it
is deliberately not read, parsed or validated here, and ``collected_at`` is never substituted.
Phase 1 therefore classes Lever jobs as ``UNKNOWN_DATE`` (kept, flagged for review). Revisit only
after permitted live validation (see the "Dates" notes in the mapping doc).
"""

import re
import uuid
from datetime import datetime
from typing import Any

from collector.adapters.common import (
    DESCRIPTION_SNIPPET_MAX,
    Config,
    Extraction,
    ExtractionFailure,
    ExtractionWarning,
    Fail,
    FailureKind,
    check_config,
    classify_url,
    clean_text,
    collect,
    from_rejection,
    html_to_text,
    make_snippet,
    read_text,
    rejected_response,
)
from collector.models import JobRecord, WorkType
from collector.pipelines.validate import FieldError, validate_job

__all__ = [
    "DESCRIPTION_SNIPPET_MAX",
    "ExtractionFailure",
    "ExtractionWarning",
    "FailureKind",
    "LeverExtraction",
    "extract_lever_jobs",
]

PLATFORM = "lever"

LeverExtraction = Extraction

# Documented values of the top-level ``workplaceType`` (README: unspecified, on-site, remote,
# hybrid). "unspecified" and anything unknown mean "not provided".
_WORK_TYPES: dict[str, WorkType | None] = {
    "remote": "remote",
    "hybrid": "hybrid",
    "on-site": "onsite",
    "unspecified": None,
}
# ``categories.commitment`` is free text; more than one value in it is ambiguous.
_MULTIPLE_VALUES = re.compile(r"[,;/|&]|\b(?:or|and)\b", re.IGNORECASE)
_COMMITMENT_MAX_LENGTH = 100


def extract_lever_jobs(
    payload: Any,
    *,
    company_name: str,
    site_slug: str,
    source_url: str,
    collected_at: datetime,
    company_website: str | None = None,
) -> LeverExtraction:
    """Convert a decoded Lever Postings API list response into ``JobRecord`` objects.

    Args:
        payload: the decoded JSON array of postings. Not modified.
        company_name: from trusted configuration; Lever responses do not carry it and it is
            never taken from posting text.
        site_slug: the Lever site the response was requested from. Scopes job identity:
            ``external_id`` is ``"<site_slug>:<id>"``.
        source_url: the public URL that was requested; becomes ``JobRecord.source_url``.
        collected_at: timezone-aware collection time (the adapter never reads the clock).
        company_website: optional configured website.

    Raises:
        ValueError: for invalid configuration (blank name, malformed site slug, non-public URL,
            naive ``collected_at``). Problems in the *response* never raise; they are returned as
            failures (fatal) or warnings (the job is kept).
    """
    config = check_config(
        company_name, site_slug, "site_slug", source_url, collected_at, company_website
    )
    if not isinstance(payload, list):
        return rejected_response(
            f"response is {type(payload).__name__}, expected a list of postings"
        )
    items = list(enumerate(payload))
    return collect(
        items, lambda item, index: _extract_job(item, index, config), "listing", len(items)
    )


# --- one posting ---------------------------------------------------------------------------


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

    def warn(field: str, kind: FailureKind, message: str) -> None:
        warnings.append(ExtractionWarning(kind, index, job_id, field, message))

    title = read_text(item, "text", fail, required=True)
    apply_url = _read_apply_url(item, fail, warn)
    categories = _read_categories(item.get("categories"), fail)
    location = _read_locations(categories, fail)
    employment_type = _read_commitment(categories, fail)
    work_type = _read_work_type(item.get("workplaceType"), fail)
    description = _read_description(item, fail)

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
            "work_type": work_type,
            "employment_type": employment_type,
            # No posted_date: Lever documents no publication-date field (see module docstring).
            "external_id": f"{config.scope}:{job_id}" if job_id else None,
            "description_snippet": description,
        }
    )
    if isinstance(result, JobRecord):
        return result, tuple(warnings)
    return from_rejection(result, index, job_id), tuple(warnings)


def _read_id(value: Any, fail: Fail) -> str | None:
    """Lever ``id`` is a UUID string. Absent means "no stable id" (identity falls back to the
    URL); a present but unusable id is a failure rather than a silent downgrade. UUIDs are
    canonicalized (lower case, hyphenated) so one id has one form; other strings are kept."""
    if value is None:
        return None
    if not isinstance(value, str):
        fail("id", FailureKind.INVALID_TYPE, f"id is {type(value).__name__}, expected a string")
        return None
    text = value.strip()
    if not text or not text.isprintable() or " " in text:
        fail("id", FailureKind.INVALID_TYPE, "id is blank or contains whitespace")
        return None
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text


def _read_apply_url(item: dict[str, Any], fail: Fail, warn: Fail) -> str | None:
    """``applyUrl`` (Lever's hosted application form) is preferred; ``hostedUrl`` (the posting
    page, which links to the form) is the fallback. An ``applyUrl`` that is present but unusable
    is reported as a warning when the fallback saves the job. No URL is ever constructed."""
    apply_url, apply_problem = classify_url(item.get("applyUrl"), "applyUrl")
    if apply_url:
        return apply_url
    hosted_url, hosted_problem = classify_url(item.get("hostedUrl"), "hostedUrl")
    if hosted_url:
        if apply_problem:
            kind, message = apply_problem
            warn("applyUrl", kind, f"{message}; hostedUrl used as the apply URL")
        return hosted_url
    for field, problem in (("applyUrl", apply_problem), ("hostedUrl", hosted_problem)):
        if problem:
            fail(field, *problem)
    if not (apply_problem or hosted_problem):
        fail("applyUrl", FailureKind.MISSING_FIELD, "applyUrl and hostedUrl are both missing")
    return None


def _read_categories(value: Any, fail: Fail) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        fail("categories", FailureKind.INVALID_TYPE, f"categories is {type(value).__name__}")
        return {}
    return value


def _read_locations(categories: dict[str, Any], fail: Fail) -> str | None:
    """The primary ``categories.location`` first, then the other ``categories.allLocations``
    entries in source order, duplicates (case-insensitive) removed, joined with ``"; "``. When
    only ``allLocations`` exists, it alone is used. Nothing is added from any other field."""
    names: list[str] = []
    primary = categories.get("location")
    if isinstance(primary, str):
        names.append(primary)
    elif primary is not None:
        fail(
            "categories.location",
            FailureKind.INVALID_TYPE,
            f"categories.location is {type(primary).__name__}",
        )
    every = categories.get("allLocations")
    if isinstance(every, list):
        for entry in every:
            if not isinstance(entry, str):
                fail(
                    "categories.allLocations",
                    FailureKind.INVALID_TYPE,
                    f"categories.allLocations has a {type(entry).__name__} entry",
                )
                break
            names.append(entry)
    elif every is not None:
        fail(
            "categories.allLocations",
            FailureKind.INVALID_TYPE,
            f"categories.allLocations is {type(every).__name__}, expected a list",
        )
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        cleaned = " ".join(name.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            unique.append(cleaned)
    return "; ".join(unique) or None


def _read_commitment(categories: dict[str, Any], fail: Fail) -> str | None:
    """``categories.commitment`` verbatim (whitespace collapsed), only when it names a single
    commitment: text that lists several (``"Full-time / Contract"``) or is long is ambiguous
    and becomes ``None``. It is not mapped onto a fixed vocabulary."""
    value = categories.get("commitment")
    if value is None:
        return None
    if not isinstance(value, str):
        fail(
            "categories.commitment",
            FailureKind.INVALID_TYPE,
            f"categories.commitment is {type(value).__name__}",
        )
        return None
    text = " ".join(value.split())
    if not text or len(text) > _COMMITMENT_MAX_LENGTH or _MULTIPLE_VALUES.search(text):
        return None
    return text


def _read_work_type(value: Any, fail: Fail) -> WorkType | None:
    """Top-level ``workplaceType`` only when it is one of the documented values. Never inferred
    from locations or descriptions."""
    if value is None:
        return None
    if not isinstance(value, str):
        fail("workplaceType", FailureKind.INVALID_TYPE, f"workplaceType is {type(value).__name__}")
        return None
    return _WORK_TYPES.get(value.strip().casefold())


def _read_description(item: dict[str, Any], fail: Fail) -> str | None:
    """``descriptionPlain`` when it has content (it is text, never parsed as HTML); otherwise
    ``description`` (HTML, converted to text without executing or loading anything)."""
    plain = item.get("descriptionPlain")
    if plain is not None and not isinstance(plain, str):
        fail(
            "descriptionPlain",
            FailureKind.INVALID_TYPE,
            f"descriptionPlain is {type(plain).__name__}",
        )
        return None
    if isinstance(plain, str) and plain.strip():
        return make_snippet(clean_text(plain))
    markup = item.get("description")
    if markup is None:
        return None
    if not isinstance(markup, str):
        fail("description", FailureKind.INVALID_TYPE, f"description is {type(markup).__name__}")
        return None
    return make_snippet(html_to_text(markup))
