"""Keka Hire jobs extraction: decoded JSON in, ``JobRecord`` objects out.

Offline and pure. No network, files, database, logging, environment or clock access: the caller
fetches and decodes the response and supplies every other input, including the collection time.
The input payload is never mutated. This module does **not** call Keka: the documented source,
``GET https://{company}.keka.com/api/v1/hire/jobs``, is an authenticated API (OAuth 2.0 client
credentials plus an API key, scope ``kekaapi``), so it is used only with an authorized
integration's own credentials, outside this adapter.

Supported shape (official API reference, ``get_v1-hire-jobs``): a JSON array of ``JobDTO``
objects. The reference lists field names and types but no field descriptions, no status values
and no date formats; this adapter maps only what that contract makes unambiguous (see
``docs/platform-field-mapping.md``).

Visibility: the endpoint returns "all Published, Confidential and Archived jobs". A job is
collected only when ``canListOnCareersSite`` is ``true`` **and** its integer ``status`` is one of
``listed_statuses``, the status code(s) the integrator has verified mean "published" in their
Keka account (Keka does not document the codes, so there is no default). Other jobs are
reported in ``Extraction.skipped``, never collected; a missing or mistyped visibility field is a
failure, never a guess.

Posting dates: **none are extracted.** ``publishedOn`` has no documented format or meaning,
``createdOn`` is a creation time and ``targetHireDate`` a hiring target, so none of them is read
and ``collected_at`` is never substituted. Every record has ``posted_date=None`` /
``posted_date_source="unknown"`` (Phase 1: ``UNKNOWN_DATE``, kept and flagged).

Each job is extracted independently: a malformed job becomes an ``ExtractionFailure`` and never
stops the others. Messages never echo source values or HTML.
"""

from collections.abc import Collection
from dataclasses import replace
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
    SkippedJob,
    check_config,
    classify_url,
    collect,
    from_rejection,
    html_to_text,
    make_snippet,
    read_text,
    rejected_response,
)
from collector.models import JobRecord
from collector.pipelines.validate import FieldError, validate_job

__all__ = [
    "DESCRIPTION_SNIPPET_MAX",
    "ExtractionFailure",
    "ExtractionWarning",
    "FailureKind",
    "KekaExtraction",
    "SkippedJob",
    "extract_keka_jobs",
]

PLATFORM = "keka"

KekaExtraction = Extraction

_JOB_TYPE_MAX_LENGTH = 100
_LOCATION_PARTS = ("city", "state", "countryName")


def extract_keka_jobs(
    payload: Any,
    *,
    company_name: str,
    tenant: str,
    listed_statuses: Collection[int],
    source_url: str,
    collected_at: datetime,
    company_website: str | None = None,
) -> KekaExtraction:
    """Convert a decoded Keka ``GET /v1/hire/jobs`` response into ``JobRecord`` objects.

    Args:
        payload: the decoded JSON array of jobs. Not modified.
        company_name: from trusted configuration; never taken from job text.
        tenant: the Keka company (the ``{company}`` in the API host). Scopes job identity:
            ``external_id`` is ``"<tenant>:<id>"``.
        listed_statuses: the ``status`` code(s) verified to mean "published" for this account.
            Required and non-empty: Keka does not document its status values.
        source_url: the URL that was requested; becomes ``JobRecord.source_url``.
        collected_at: timezone-aware collection time (the adapter never reads the clock).
        company_website: optional configured website.

    Raises:
        ValueError: for invalid configuration (blank name, malformed tenant, empty or
            non-integer ``listed_statuses``, non-public URL, naive ``collected_at``). Problems in
            the *response* never raise; they are failures, skips or warnings.
    """
    config = check_config(company_name, tenant, "tenant", source_url, collected_at, company_website)
    statuses = frozenset(listed_statuses)
    if not statuses or not all(
        isinstance(code, int) and not isinstance(code, bool) for code in statuses
    ):
        raise ValueError("listed_statuses must be a non-empty collection of integer status codes")
    if not isinstance(payload, list):
        return rejected_response(f"response is {type(payload).__name__}, expected a list of jobs")

    items: list[tuple[int | None, Any]] = []
    skipped: list[SkippedJob] = []
    for index, item in enumerate(payload):
        reason = _skip_reason(item, statuses)
        if reason is None:
            items.append((index, item))
        else:
            skipped.append(SkippedJob(index, _peek_id(item), reason))
    result = collect(
        items,
        lambda item, index: _extract_job(item, index, config),
        "listing",
        len(payload),
    )
    return replace(result, skipped=tuple(skipped))


def _skip_reason(item: Any, statuses: frozenset[int]) -> str | None:
    """Why a well-typed job is deliberately not collected (mistyped fields are failures)."""
    if not isinstance(item, dict):
        return None
    if item.get("canListOnCareersSite") is False:
        return "canListOnCareersSite is false: not listed on the careers site"
    status = item.get("status")
    if isinstance(status, int) and not isinstance(status, bool) and status not in statuses:
        return "status is not a configured published status"
    return None


def _peek_id(item: Any) -> str | None:
    """Best-effort id of a skipped job, for reporting only."""
    value = item.get("id") if isinstance(item, dict) else None
    return value.strip() if isinstance(value, str) and value.strip().isprintable() else None


# --- one job -------------------------------------------------------------------------------


def _extract_job(
    item: Any, index: int | None, config: Config
) -> tuple[JobRecord | ExtractionFailure, tuple[ExtractionWarning, ...]]:
    if not isinstance(item, dict):
        error = FieldError("(job)", FailureKind.INVALID_TYPE.value, f"job is {type(item).__name__}")
        return ExtractionFailure(FailureKind.INVALID_TYPE, index, None, (error,)), ()

    errors: list[FieldError] = []

    def fail(field: str, kind: FailureKind, message: str) -> None:
        errors.append(FieldError(field, kind.value, message))

    job_id = _read_id(item.get("id"), fail)
    _check_visibility(item, fail)
    title = read_text(item, "title", fail, required=True)
    apply_url = _read_apply_url(item.get("careerPortalUrl"), fail)
    location = _read_locations(item.get("jobLocations"), fail)
    employment_type = _read_job_type(item.get("jobType"), fail)
    description = _read_description(item.get("description"), fail)

    if errors:
        return ExtractionFailure(FailureKind(errors[0].code), index, job_id, tuple(errors)), ()

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
            "employment_type": employment_type,
            # No posted_date: no Keka date field has a documented meaning and format.
            "external_id": f"{config.scope}:{job_id}" if job_id else None,
            "description_snippet": description,
        }
    )
    if isinstance(result, JobRecord):
        return result, ()
    return from_rejection(result, index, job_id), ()


def _read_id(value: Any, fail: Fail) -> str | None:
    """``id`` (a nullable string). Absent means no stable id (identity falls back to the apply
    URL); present but unusable is a failure rather than a silent downgrade."""
    if value is None:
        return None
    if not isinstance(value, str):
        fail("id", FailureKind.INVALID_TYPE, f"id is {type(value).__name__}, expected a string")
        return None
    text = value.strip()
    if not text or not text.isprintable() or " " in text:
        fail("id", FailureKind.INVALID_TYPE, "id is blank or contains whitespace")
        return None
    return text


def _check_visibility(item: dict[str, Any], fail: Fail) -> None:
    """Both visibility fields are non-nullable in the contract. Jobs reach this point only when
    they were not skipped, so a value here that is not ``true`` / an integer is a data problem."""
    listed = item.get("canListOnCareersSite")
    if listed is None:
        fail("canListOnCareersSite", FailureKind.MISSING_FIELD, "canListOnCareersSite is missing")
    elif listed is not True:
        fail(
            "canListOnCareersSite",
            FailureKind.INVALID_TYPE,
            f"canListOnCareersSite is {type(listed).__name__}, expected a boolean",
        )
    status = item.get("status")
    if status is None:
        fail("status", FailureKind.MISSING_FIELD, "status is missing")
    elif not isinstance(status, int) or isinstance(status, bool):
        fail("status", FailureKind.INVALID_TYPE, f"status is {type(status).__name__}")


def _read_apply_url(value: Any, fail: Fail) -> str | None:
    """``careerPortalUrl``, the job's careers-portal link, is the only URL in the contract; it is
    required (never constructed) and must be a public http(s) URL."""
    url, problem = classify_url(value, "careerPortalUrl")
    if problem:
        fail("careerPortalUrl", *problem)
    elif url is None:
        fail("careerPortalUrl", FailureKind.MISSING_FIELD, "careerPortalUrl is missing")
    return url


def _read_locations(value: Any, fail: Fail) -> str | None:
    """Each ``jobLocations`` entry as "city, state, countryName" (the parts present), or its
    ``name`` when it has none of them; entries in source order, case-insensitive duplicates
    removed, joined with ``"; "``. Nothing is inferred from other fields."""
    if value is None:
        return None
    if not isinstance(value, list):
        fail("jobLocations", FailureKind.INVALID_TYPE, f"jobLocations is {type(value).__name__}")
        return None
    names: list[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            fail(
                "jobLocations",
                FailureKind.INVALID_TYPE,
                f"jobLocations has a {type(entry).__name__} entry",
            )
            return None
        for key in (*_LOCATION_PARTS, "name"):
            part = entry.get(key)
            if part is not None and not isinstance(part, str):
                fail(
                    f"jobLocations.{key}",
                    FailureKind.INVALID_TYPE,
                    f"jobLocations.{key} is {type(part).__name__}",
                )
                return None
        parts = [
            " ".join(entry[k].split()) for k in _LOCATION_PARTS if (entry.get(k) or "").strip()
        ]
        if parts:
            names.append(", ".join(parts))
        elif (entry.get("name") or "").strip():
            names.append(" ".join(entry["name"].split()))
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name.casefold() not in seen:
            seen.add(name.casefold())
            unique.append(name)
    return "; ".join(unique) or None


def _read_job_type(value: Any, fail: Fail) -> str | None:
    """``jobType`` verbatim (whitespace collapsed); it has no documented vocabulary, so it is not
    mapped. Overlong text is treated as not provided."""
    if value is None:
        return None
    if not isinstance(value, str):
        fail("jobType", FailureKind.INVALID_TYPE, f"jobType is {type(value).__name__}")
        return None
    text = " ".join(value.split())
    return text if text and len(text) <= _JOB_TYPE_MAX_LENGTH else None


def _read_description(value: Any, fail: Fail) -> str | None:
    """``description`` converted from HTML to text (plain text passes through unchanged); no
    script runs and nothing is loaded."""
    if value is None:
        return None
    if not isinstance(value, str):
        fail("description", FailureKind.INVALID_TYPE, f"description is {type(value).__name__}")
        return None
    return make_snippet(html_to_text(value))
