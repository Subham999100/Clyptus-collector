"""JSON-LD ``JobPosting`` extraction: already-fetched HTML in, ``JobRecord`` objects out.

Offline and pure. No network, files, database, logging, environment or clock access: the caller
fetches the page and supplies the HTML, its URL and the collection time. Nothing is executed:
``<script type="application/ld+json">`` blocks are read as text only, and no browser or crawler
is involved. The input is never mutated.

Supported shapes (see ``docs/platform-field-mapping.md``): each JSON-LD block may hold one
object, a top-level array, or an ``@graph`` list; ``JobPosting`` objects are taken from any of
those (nested ``@graph``/array combinations up to a fixed depth, so hostile nesting is bounded).
Objects of other schema.org types are ignored. Blocks are parsed one by one with strict JSON, so
one malformed block never hides the postings in the others.

Extraction units are counted in document order: each malformed (non-blank) JSON-LD block is one
``invalid_response`` failure, and each identified ``JobPosting`` is one record or failure, so
``job_count == len(records) + len(failures)``. A blank block carries no data and is ignored.

Posting dates: only a valid ISO 8601 ``datePosted`` (documented by Google as the original
publication date) is used, with ``posted_date_source="jsonld"``. ``validThrough`` (an expiry),
page modification times and ``collected_at`` are never substituted; a missing or unusable
``datePosted`` leaves the date unknown (Phase 1 classifies the job ``UNKNOWN_DATE``).

Company: a configured ``company_name`` always wins; otherwise ``hiringOrganization.name`` is
used, except ``"confidential"`` (Google's marker for anonymous hiring), which is not a usable
company name. ``JobPosting`` has no application-URL property (``directApply`` is only a boolean
hint), so ``url`` (the posting's page) is used and the page URL is the fallback; no URL is ever
invented.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from typing import Any

from selectolax.parser import HTMLParser

from collector.adapters.common import (
    DESCRIPTION_SNIPPET_MAX,
    Extraction,
    ExtractionFailure,
    ExtractionWarning,
    Fail,
    FailureKind,
    classify_url,
    from_rejection,
    html_to_text,
    make_snippet,
    public_url,
    read_iso_date,
    read_text,
    rejected_response,
    without_control_characters,
)
from collector.models import JobRecord, WorkType
from collector.pipelines.validate import FieldError, validate_job

__all__ = [
    "DESCRIPTION_SNIPPET_MAX",
    "ExtractionFailure",
    "ExtractionWarning",
    "FailureKind",
    "JsonLdExtraction",
    "extract_json_ld_jobs",
]

PLATFORM = "jsonld"

JsonLdExtraction = Extraction

_SCRIPT_TYPE = "application/ld+json"
_MAX_DEPTH = 6  # arrays and @graph lists are expanded at most this deep; nothing else is walked


@dataclass(frozen=True)
class _PageConfig:
    """Validated trusted configuration for one page."""

    company_name: str | None
    page_url: str
    collected_at: datetime
    company_website: str | None


def extract_json_ld_jobs(
    html: Any,
    *,
    page_url: str,
    collected_at: datetime,
    company_name: str | None = None,
    company_website: str | None = None,
) -> JsonLdExtraction:
    """Convert the JSON-LD ``JobPosting`` markup of one fetched page into ``JobRecord`` objects.

    Args:
        html: the already-fetched page markup. Never fetched here; nothing in it is executed.
        page_url: the public URL the page was fetched from; becomes ``JobRecord.source_url``
            and the apply-URL fallback.
        collected_at: timezone-aware collection time (the adapter never reads the clock).
        company_name: optional trusted configuration; when given it always wins over
            ``hiringOrganization.name``.
        company_website: optional trusted configuration; when given it always wins over
            ``hiringOrganization.sameAs``/``url``.

    Raises:
        ValueError: for invalid configuration (blank ``company_name``, non-public URLs, naive
            ``collected_at``). Problems in the *page* never raise; they are returned as failures
            (fatal) or warnings (the job is kept).
    """
    config = _check_page_config(company_name, page_url, collected_at, company_website)
    if not isinstance(html, str):
        return rejected_response(f"page is {type(html).__name__}, expected an HTML string")

    outcomes: list[tuple[int, JobRecord | ExtractionFailure]] = []
    warnings: list[ExtractionWarning] = []
    fallback_indexes: list[int] = []
    index = 0
    for block in _json_ld_blocks(html):
        if not block.strip():
            continue  # a blank block carries no data; nothing is lost
        try:
            data = json.loads(block)
        except ValueError:
            error = FieldError(
                "(script)", FailureKind.INVALID_RESPONSE.value, "JSON-LD block is not valid JSON"
            )
            outcomes.append(
                (index, ExtractionFailure(FailureKind.INVALID_RESPONSE, index, None, (error,)))
            )
            index += 1
            continue
        for posting in _job_postings(data):
            outcome, job_warnings = _extract_job(posting, index, config)
            warnings.extend(job_warnings)
            outcomes.append((index, outcome))
            job_url, _ = classify_url(posting.get("url"), "url")
            if (
                isinstance(outcome, JobRecord)
                and outcome.external_id is None
                and (job_url is None or job_url == config.page_url)
            ):
                fallback_indexes.append(index)
            index += 1

    ambiguous_indexes = set(fallback_indexes) if len(fallback_indexes) > 1 else set()
    records: list[JobRecord] = []
    failures: list[ExtractionFailure] = []
    for posting_index, outcome in outcomes:
        if isinstance(outcome, JobRecord) and posting_index in ambiguous_indexes:
            error = FieldError(
                "(identity)",
                FailureKind.INVALID_RESPONSE.value,
                "multiple JobPosting entries have no job-specific identifier or URL; "
                "identity is ambiguous",
            )
            failures.append(
                ExtractionFailure(FailureKind.INVALID_RESPONSE, posting_index, None, (error,))
            )
        elif isinstance(outcome, JobRecord):
            records.append(outcome)
        else:
            failures.append(outcome)
    return JsonLdExtraction(tuple(records), tuple(failures), "listing", index, tuple(warnings))


def _check_page_config(
    company_name: str | None,
    page_url: str,
    collected_at: datetime,
    company_website: str | None,
) -> _PageConfig:
    """Validate configuration; raise ``ValueError`` if it is unusable."""
    if company_name is not None:
        if not isinstance(company_name, str) or not company_name.strip():
            raise ValueError("company_name must be a non-blank string when provided")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    try:
        clean_page = public_url(page_url)
        clean_website = public_url(company_website) if company_website is not None else None
    except ValueError as exc:
        raise ValueError(f"configuration URL: {exc}") from exc
    return _PageConfig(
        company_name.strip() if company_name else None, clean_page, collected_at, clean_website
    )


# --- finding the postings ---------------------------------------------------------------------


def _json_ld_blocks(markup: str) -> list[str]:
    """The text of every ``<script type="application/ld+json">`` element, in document order.

    The parser only reads text content; scripts are never executed and nothing is loaded."""
    markup = without_control_characters(markup)  # the parser mishandles NUL bytes
    if not markup.strip():
        return []
    blocks: list[str] = []
    for node in HTMLParser(markup).css("script"):
        declared = node.attributes.get("type") or ""
        if declared.strip().casefold() == _SCRIPT_TYPE:
            blocks.append(node.text(deep=True) or "")
    return blocks


def _job_postings(data: Any) -> list[dict[str, Any]]:
    """Every ``JobPosting`` object in one decoded block, in source order.

    Iterative and depth-limited: only arrays and ``@graph`` lists are expanded, never other
    properties, so unbounded or hostile nesting cannot recurse deeply."""
    found: list[dict[str, Any]] = []
    stack: list[tuple[Any, int]] = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_DEPTH:
            continue
        if isinstance(node, list):
            stack.extend((entry, depth + 1) for entry in reversed(node))
        elif isinstance(node, dict):
            if _is_job_posting(node):
                found.append(node)
            graph = node.get("@graph")
            if isinstance(graph, list):
                stack.extend((entry, depth + 1) for entry in reversed(graph))
    return found


def _is_job_posting(node: dict[str, Any]) -> bool:
    """``@type`` names ``JobPosting``: alone or in a list, as the bare name, a schema.org IRI
    (``https://schema.org/JobPosting``) or a prefixed name (``schema:JobPosting``)."""
    declared = node.get("@type")
    for entry in declared if isinstance(declared, list) else [declared]:
        if isinstance(entry, str):
            name = entry.strip().rpartition("/")[2].rpartition(":")[2]
            if name.casefold() == "jobposting":
                return True
    return False


# --- one posting --------------------------------------------------------------------------------


def _extract_job(
    item: dict[str, Any], index: int | None, config: _PageConfig
) -> tuple[JobRecord | ExtractionFailure, tuple[ExtractionWarning, ...]]:
    errors: list[FieldError] = []
    warnings: list[ExtractionWarning] = []

    def fail(field: str, kind: FailureKind, message: str) -> None:
        errors.append(FieldError(field, kind.value, message))

    job_id = _read_identifier(item.get("identifier"), fail)

    def warn(field: str, kind: FailureKind, message: str) -> None:
        warnings.append(ExtractionWarning(kind, index, job_id, field, message))

    title = read_text(item, "title", fail, required=True)
    company = config.company_name or _read_company_name(item, fail)
    website = config.company_website or _read_company_website(item, warn)
    apply_url = _read_apply_url(item, config.page_url, warn)
    location = _read_locations(item.get("jobLocation"), fail)
    work_type = _read_work_type(item.get("jobLocationType"), fail)
    employment_type = _read_employment_type(item.get("employmentType"), fail)
    posted = read_iso_date(item.get("datePosted"), "datePosted", config.collected_at, warn)
    description = _read_description(item.get("description"), fail)

    if errors:
        failure = ExtractionFailure(FailureKind(errors[0].code), index, job_id, tuple(errors))
        return failure, tuple(warnings)

    result = validate_job(
        {
            "title": " ".join(title.split()) if title else title,
            "company_name": company,
            "apply_url": apply_url,
            "source_url": config.page_url,
            "platform": PLATFORM,
            "collected_at": config.collected_at,
            "company_website": website,
            "location": location,
            "work_type": work_type,
            "employment_type": employment_type,
            "posted_date": posted,
            "posted_date_source": "jsonld" if posted else "unknown",
            "external_id": job_id,
            "description_snippet": description,
        }
    )
    if isinstance(result, JobRecord):
        return result, tuple(warnings)
    return from_rejection(result, index, job_id), tuple(warnings)


def _read_identifier(value: Any, fail: Fail) -> str | None:
    """``identifier`` is a ``PropertyValue`` (its ``value``) or a plain string/number. Absent
    means "no stable id" (identity falls back to the URL); a present but unusable id is a
    failure rather than a silent downgrade. It is Google's "hiring organization's unique job
    ID", so it is stored unscoped (``job_key`` already scopes by platform and company)."""
    field = "identifier"
    if isinstance(value, dict):
        value = value.get("value")
        field = "identifier.value"
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        fail(field, FailureKind.INVALID_TYPE, f"{field} is {type(value).__name__}")
        return None
    text = " ".join(value.split())
    if not text or not text.isprintable():
        fail(field, FailureKind.INVALID_TYPE, f"{field} is blank or unprintable")
        return None
    return text


def _read_company_name(item: dict[str, Any], fail: Fail) -> str | None:
    """``hiringOrganization.name`` (or the organization given as a plain string). Google allows
    ``"confidential"`` for anonymous hiring; that is not a usable company name. Only consulted
    when no ``company_name`` is configured."""
    org = item.get("hiringOrganization")
    if org is None:
        fail(
            "hiringOrganization",
            FailureKind.MISSING_FIELD,
            "hiringOrganization is missing and no company_name is configured",
        )
        return None
    if isinstance(org, str):
        name = org
    elif isinstance(org, dict):
        value = org.get("name")
        if value is None:
            fail(
                "hiringOrganization.name",
                FailureKind.MISSING_FIELD,
                "hiringOrganization.name is missing",
            )
            return None
        if not isinstance(value, str):
            fail(
                "hiringOrganization.name",
                FailureKind.INVALID_TYPE,
                f"hiringOrganization.name is {type(value).__name__}, expected a string",
            )
            return None
        name = value
    else:
        fail(
            "hiringOrganization",
            FailureKind.INVALID_TYPE,
            f"hiringOrganization is {type(org).__name__}",
        )
        return None
    name = " ".join(name.split())
    if not name:
        fail(
            "hiringOrganization.name",
            FailureKind.MISSING_FIELD,
            "hiringOrganization.name is blank",
        )
        return None
    if name.casefold() == "confidential":
        fail(
            "hiringOrganization.name",
            FailureKind.MISSING_FIELD,
            "hiringOrganization.name marks anonymous hiring; company unknown",
        )
        return None
    return name


def _read_company_website(item: dict[str, Any], warn: Fail) -> str | None:
    """``hiringOrganization.sameAs``, else ``hiringOrganization.url``, when usable. The website
    is optional, so a present but unusable value is only a warning. Only consulted when no
    ``company_website`` is configured."""
    org = item.get("hiringOrganization")
    if not isinstance(org, dict):
        return None  # a plain-string or malformed organization carries no website
    same_as, same_problem = classify_url(org.get("sameAs"), "hiringOrganization.sameAs")
    if same_as:
        return same_as
    org_url, url_problem = classify_url(org.get("url"), "hiringOrganization.url")
    if org_url:
        if same_problem:
            kind, message = same_problem
            warn(
                "hiringOrganization.sameAs",
                kind,
                f"{message}; hiringOrganization.url used as the company website",
            )
        return org_url
    for field, problem in (
        ("hiringOrganization.sameAs", same_problem),
        ("hiringOrganization.url", url_problem),
    ):
        if problem:
            kind, message = problem
            warn(field, kind, f"{message}; company website unset")
    return None


def _read_apply_url(item: dict[str, Any], page_url: str, warn: Fail) -> str:
    """``url`` (the posting's own page) when usable; otherwise the page the markup came from.
    ``JobPosting`` has no application-URL property (``directApply`` is only a boolean hint), so
    no URL is ever invented; a present but unusable ``url`` is a warning because the page URL
    saves the job."""
    url, problem = classify_url(item.get("url"), "url")
    if url:
        return url
    if problem:
        kind, message = problem
        warn("url", kind, f"{message}; the page URL used as the apply URL")
    return page_url


def _read_locations(value: Any, fail: Fail) -> str | None:
    """``jobLocation``: one ``Place`` or a list of them, in source order, case-insensitive
    duplicates removed, joined with ``"; "``. Each place contributes its postal address
    (``addressLocality, addressRegion, addressCountry``) or its plain-text address or name.
    ``applicantLocationRequirements`` (an eligibility area, not a workplace) is never read."""
    if value is None:
        return None
    names: list[str] = []
    for place in value if isinstance(value, list) else [value]:
        text = _place_text(place, fail)
        if text:
            names.append(text)
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        cleaned = " ".join(name.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            unique.append(cleaned)
    return "; ".join(unique) or None


def _place_text(place: Any, fail: Fail) -> str | None:
    if isinstance(place, str):
        return place
    if not isinstance(place, dict):
        fail(
            "jobLocation",
            FailureKind.INVALID_TYPE,
            f"jobLocation has a {type(place).__name__} entry",
        )
        return None
    address = place.get("address")
    if address is None:
        name = place.get("name")
        if name is None:
            return None
        if not isinstance(name, str):
            fail(
                "jobLocation.name",
                FailureKind.INVALID_TYPE,
                f"jobLocation.name is {type(name).__name__}",
            )
            return None
        return name
    if isinstance(address, str):
        return address
    if not isinstance(address, dict):
        fail(
            "jobLocation.address",
            FailureKind.INVALID_TYPE,
            f"jobLocation.address is {type(address).__name__}",
        )
        return None
    parts: list[str] = []
    for key in ("addressLocality", "addressRegion", "addressCountry"):
        part = address.get(key)
        if part is None:
            continue
        if isinstance(part, dict):  # addressCountry may be a Country object with a name
            part = part.get("name")
            if part is None:
                continue
        if not isinstance(part, str):
            fail(
                f"jobLocation.address.{key}",
                FailureKind.INVALID_TYPE,
                f"jobLocation.address.{key} is {type(part).__name__}",
            )
            continue
        cleaned = " ".join(part.split())
        if cleaned:
            parts.append(cleaned)
    return ", ".join(parts) or None


def _read_work_type(value: Any, fail: Fail) -> WorkType | None:
    """``jobLocationType == "TELECOMMUTE"`` (Google: 100 % remote) maps to ``remote``. The
    vocabulary has no hybrid or on-site value and absence does not mean on-site, so nothing else
    is ever inferred."""
    if value is None:
        return None
    remote = False
    for entry in value if isinstance(value, list) else [value]:
        if not isinstance(entry, str):
            fail(
                "jobLocationType",
                FailureKind.INVALID_TYPE,
                f"jobLocationType is or contains a {type(entry).__name__}",
            )
            return None
        if entry.strip().casefold() == "telecommute":
            remote = True
    return "remote" if remote else None


def _read_employment_type(value: Any, fail: Fail) -> str | None:
    """``employmentType`` verbatim (whitespace collapsed); an array is joined with ``", "`` in
    source order, case-insensitive duplicates removed. Values are not mapped onto a
    vocabulary."""
    if value is None:
        return None
    seen: set[str] = set()
    unique: list[str] = []
    for entry in value if isinstance(value, list) else [value]:
        if not isinstance(entry, str):
            fail(
                "employmentType",
                FailureKind.INVALID_TYPE,
                f"employmentType is or contains a {type(entry).__name__}",
            )
            return None
        cleaned = " ".join(entry.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            unique.append(cleaned)
    return ", ".join(unique) or None


def _read_description(value: Any, fail: Fail) -> str | None:
    """``description`` is HTML per Google, though pages also embed it entity-encoded or as plain
    text: content without literal tags is entity-decoded once, then converted to text exactly as
    for the other adapters (nothing is executed or fetched)."""
    if value is None:
        return None
    if not isinstance(value, str):
        fail("description", FailureKind.INVALID_TYPE, f"description is {type(value).__name__}")
        return None
    markup = value if "<" in value else unescape(value)
    return make_snippet(html_to_text(markup))
