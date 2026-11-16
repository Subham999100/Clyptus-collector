"""Ashby Job Board API extraction: decoded JSON in, ``JobRecord`` objects out.

Offline and pure. No network, files, database, logging, environment or clock access: the caller
fetches and decodes the response and supplies every other input, including the collection time.
The input payload is never mutated.

Supported shape (see ``docs/platform-field-mapping.md``): the response of
``GET /posting-api/job-board/{board}``, an object ``{"apiVersion": "1", "jobs": [...]}``. The
documentation shows no separate single-job shape, so anything without a ``jobs`` list is an
invalid response. ``apiVersion`` is not checked.

Each job is extracted independently. A malformed job becomes an ``ExtractionFailure`` and never
stops the others; nothing is dropped silently. A job with ``isListed: false`` is documented as
reachable only by direct link, so it is deliberately **skipped**, not collected, and reported in
``Extraction.skipped``. Optional values the source does not provide stay ``None`` and nothing is
guessed. Messages never echo source values or HTML.

Posting dates: ``publishedAt`` is documented as the "ISO DateTime when the job was **last**
published", so it is a verified publication timestamp (``posted_date_source="platform"``), with
the caveat that re-publishing a job moves it forward. Absent means unknown; ``collected_at`` is
never substituted. ``isRemote`` is deliberately not read: the documentation says it is true for
Hybrid as well as Remote, so ``workplaceType`` alone decides the work type.
"""

import uuid
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
    clean_text,
    collect,
    from_rejection,
    html_to_text,
    make_snippet,
    read_iso_date,
    read_text,
    rejected_response,
)
from collector.models import JobRecord, WorkType
from collector.pipelines.validate import FieldError, validate_job

__all__ = [
    "DESCRIPTION_SNIPPET_MAX",
    "AshbyExtraction",
    "ExtractionFailure",
    "ExtractionWarning",
    "FailureKind",
    "SkippedJob",
    "extract_ashby_jobs",
]

PLATFORM = "ashby"

AshbyExtraction = Extraction

# Documented values of ``workplaceType`` (docs: OnSite, Remote, Hybrid), case-insensitive.
# Unknown values mean "not provided"; the work type is never inferred from ``isRemote`` or text.
_WORK_TYPES: dict[str, WorkType] = {"onsite": "onsite", "remote": "remote", "hybrid": "hybrid"}


def extract_ashby_jobs(
    payload: Any,
    *,
    company_name: str,
    board_name: str,
    source_url: str,
    collected_at: datetime,
    company_website: str | None = None,
) -> AshbyExtraction:
    """Convert a decoded Ashby job-board response into ``JobRecord`` objects.

    Args:
        payload: the decoded JSON object (``{"jobs": [...]}``). Not modified.
        company_name: from trusted configuration; Ashby responses do not carry it and it is
            never taken from job text.
        board_name: the Ashby job-board name the response was requested from. Scopes job
            identity: ``external_id`` is ``"<board_name>:<id>"``.
        source_url: the public URL that was requested; becomes ``JobRecord.source_url``.
        collected_at: timezone-aware collection time (the adapter never reads the clock).
        company_website: optional configured website.

    Raises:
        ValueError: for invalid configuration (blank name, malformed board name, non-public URL,
            naive ``collected_at``). Problems in the *response* never raise; they are returned as
            failures (fatal), skips (unlisted jobs) or warnings (the job is kept).
    """
    config = check_config(
        company_name, board_name, "board_name", source_url, collected_at, company_website
    )
    if not isinstance(payload, dict):
        return rejected_response(f"response is {type(payload).__name__}, expected an object")
    jobs = payload.get("jobs")
    if jobs is None:
        return rejected_response("response has no 'jobs' list")
    if not isinstance(jobs, list):
        return rejected_response(f"'jobs' is {type(jobs).__name__}, expected a list")

    items: list[tuple[int | None, Any]] = []
    skipped: list[SkippedJob] = []
    for index, item in enumerate(jobs):
        if isinstance(item, dict) and item.get("isListed") is False:
            skipped.append(
                SkippedJob(index, _peek_id(item), "isListed is false: not on the public board")
            )
        else:
            items.append((index, item))
    result = collect(
        items, lambda item, index: _extract_job(item, index, config), "listing", len(jobs)
    )
    return replace(result, skipped=tuple(skipped))


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

    def warn(field: str, kind: FailureKind, message: str) -> None:
        warnings.append(ExtractionWarning(kind, index, job_id, field, message))

    listed = item.get("isListed")
    if listed is not None and not isinstance(listed, bool):
        fail(
            "isListed",
            FailureKind.INVALID_TYPE,
            f"isListed is {type(listed).__name__}, expected a boolean",
        )

    title = read_text(item, "title", fail, required=True)
    apply_url = _read_apply_url(item, fail, warn)
    location = _read_location(item, fail)
    employment_type = _read_employment_type(item, fail)
    work_type = _read_work_type(item.get("workplaceType"), fail)
    description = _read_description(item, fail)
    posted = read_iso_date(item.get("publishedAt"), "publishedAt", config.collected_at, warn)

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
            "posted_date": posted,
            "posted_date_source": "platform" if posted else "unknown",
            "external_id": f"{config.scope}:{job_id}" if job_id else None,
            "description_snippet": description,
        }
    )
    if isinstance(result, JobRecord):
        return result, tuple(warnings)
    return from_rejection(result, index, job_id), tuple(warnings)


def _peek_id(item: dict[str, Any]) -> str | None:
    """Best-effort id of a skipped job, for reporting only; problems are not recorded."""
    return _read_id(item.get("id"), lambda field, kind, message: None)


def _read_id(value: Any, fail: Fail) -> str | None:
    """Ashby's official field table documents no job ``id``; observed responses carry a UUID
    string (**ASSUMPTION**, see the mapping doc). Absent means "no stable id" (identity falls
    back to the URL); a present but unusable id is a failure rather than a silent downgrade.
    UUIDs are canonicalized (lower case, hyphenated) so one id has one form."""
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
    """``applyUrl`` (Ashby's hosted application page) is preferred; ``jobUrl`` (the posting
    page, which links to the form) is the fallback. An ``applyUrl`` that is present but unusable
    is reported as a warning when the fallback saves the job. No URL is ever constructed."""
    apply_url, apply_problem = classify_url(item.get("applyUrl"), "applyUrl")
    if apply_url:
        return apply_url
    job_url, job_problem = classify_url(item.get("jobUrl"), "jobUrl")
    if job_url:
        if apply_problem:
            kind, message = apply_problem
            warn("applyUrl", kind, f"{message}; jobUrl used as the apply URL")
        return job_url
    for field, problem in (("applyUrl", apply_problem), ("jobUrl", job_problem)):
        if problem:
            fail(field, *problem)
    if not (apply_problem or job_problem):
        fail("applyUrl", FailureKind.MISSING_FIELD, "applyUrl and jobUrl are both missing")
    return None


def _read_location(item: dict[str, Any], fail: Fail) -> str | None:
    """The primary ``location`` first, then each ``secondaryLocations[].location`` in source
    order, duplicates (case-insensitive) removed, joined with ``"; "``. The structured
    ``address`` objects are not read: the display names already name the places."""
    names: list[str] = []
    primary = item.get("location")
    if isinstance(primary, str):
        names.append(primary)
    elif primary is not None:
        fail("location", FailureKind.INVALID_TYPE, f"location is {type(primary).__name__}")
    secondary = item.get("secondaryLocations")
    if isinstance(secondary, list):
        for entry in secondary:
            if not isinstance(entry, dict):
                fail(
                    "secondaryLocations",
                    FailureKind.INVALID_TYPE,
                    f"secondaryLocations has a {type(entry).__name__} entry",
                )
                break
            name = entry.get("location")
            if name is None:
                continue
            if not isinstance(name, str):
                fail(
                    "secondaryLocations.location",
                    FailureKind.INVALID_TYPE,
                    f"secondaryLocations.location is {type(name).__name__}",
                )
                break
            names.append(name)
    elif secondary is not None:
        fail(
            "secondaryLocations",
            FailureKind.INVALID_TYPE,
            f"secondaryLocations is {type(secondary).__name__}, expected a list",
        )
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        cleaned = " ".join(name.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            unique.append(cleaned)
    return "; ".join(unique) or None


def _read_employment_type(item: dict[str, Any], fail: Fail) -> str | None:
    """``employmentType`` verbatim (whitespace collapsed). The documentation lists ``FullTime``,
    ``PartTime``, ``Intern``, ``Contract`` and ``Temporary``; the value is stored as sent and
    not mapped onto a vocabulary, so an undocumented value is kept rather than guessed away."""
    value = read_text(item, "employmentType", fail, required=False)
    if value is None:
        return None
    return " ".join(value.split()) or None


def _read_work_type(value: Any, fail: Fail) -> WorkType | None:
    """``workplaceType`` only when it is one of the documented values. Never inferred from
    ``isRemote`` (true for Hybrid too), locations or descriptions."""
    if value is None:
        return None
    if not isinstance(value, str):
        fail("workplaceType", FailureKind.INVALID_TYPE, f"workplaceType is {type(value).__name__}")
        return None
    return _WORK_TYPES.get(value.strip().casefold())


def _read_description(item: dict[str, Any], fail: Fail) -> str | None:
    """``descriptionPlain`` when it has content (it is text, never parsed as HTML); otherwise
    ``descriptionHtml`` (HTML, converted to text without executing or loading anything)."""
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
    markup = item.get("descriptionHtml")
    if markup is None:
        return None
    if not isinstance(markup, str):
        fail(
            "descriptionHtml",
            FailureKind.INVALID_TYPE,
            f"descriptionHtml is {type(markup).__name__}",
        )
        return None
    return make_snippet(html_to_text(markup))
