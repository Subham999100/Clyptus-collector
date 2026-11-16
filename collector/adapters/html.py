"""Generic HTML job extraction: already-fetched career pages in, ``JobRecord`` objects out.

Offline and pure. No network, files, database, logging, environment or clock access: the caller
(``collector.spiders.platforms.HtmlSpider``) fetches every page through the protected crawler
and supplies the markup, its URL and the collection time. Nothing in a page is executed and
nothing is loaded; scripts are never run.

This is **not** a universal scraper. Every source has its own reviewed selector configuration
(``HtmlConfig``): CSS selectors, in Scrapy/parsel syntax, that say where a page keeps each value.
Values come only from what those selectors match; nothing is guessed, and an optional field
whose selector is absent or matches nothing stays ``None``.

Supported structures:

- a listing page whose job cards (``job``) sit inside a listing container (``listing``), each
  card with a title and a link to the job's own page (``url``);
- optionally, each job's detail page (``detail``), read only for the fields it is configured
  for, when the card alone does not carry them;
- optionally, simple server-rendered pagination through a next-page link (``next_page``),
  bounded by ``max_pages``.

Every link (job pages and next pages) must resolve to an ``https`` URL on exactly the host and
port of the configured listing URL, under ``path_prefix``; anything else is reported and never
followed. A link that only works with JavaScript (``javascript:``, ``#``, a button) is reported
as unsupported. A page whose listing container is missing is an unusable response, never an
empty listing, unless the configured ``no_jobs`` marker is present.

Identity: a configured ``external_id`` (unscoped, as for JSON-LD: ``job_key`` already scopes by
platform and company), otherwise the job page URL, which is the apply URL. Card positions are
used only to report problems, never as identity. Posting dates come only from the configured
``posted_date`` text, parsed by the shared ``parse_posted_date`` (relative phrases such as
"3 days ago" resolve against ``collected_at`` in its own timezone, so pass it in the business
timezone); unreadable text leaves the date unknown, with a warning. ``collected_at`` is never
used as a posting date.

Counting: a listing ``Extraction`` settles the cards it can (records, failures, duplicates
skipped) and returns the others as ``pending`` jobs; each pending job is settled by exactly one
``detail`` extraction (``job_count`` 1). So ``records + failures + skipped == job_count`` holds
for every extraction, and the job counts of all extractions add up to the cards found.
"""

import json
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from parsel import Selector
from selectolax.parser import HTMLParser

from collector.adapters.common import (
    DESCRIPTION_SNIPPET_MAX,
    Extraction,
    ExtractionFailure,
    ExtractionWarning,
    FailureKind,
    SkippedJob,
    clean_text,
    from_rejection,
    html_to_text,
    make_snippet,
    public_url,
    rejected_response,
    without_control_characters,
)
from collector.models import JobRecord, WorkType
from collector.pipelines.validate import FieldError, validate_job
from collector.utils.dates import parse_posted_date
from collector.utils.urls import normalize_url

__all__ = [
    "DESCRIPTION_SNIPPET_MAX",
    "MAX_PAGES",
    "FieldSelectors",
    "HtmlConfig",
    "ListingPage",
    "PendingJob",
    "detail_unavailable",
    "extract_html_detail",
    "extract_html_listing",
    "loads_html_config",
    "parse_html_config",
]

PLATFORM = "html"

MAX_PAGES = 10  # hard upper bound for max_pages (listing pages per source and run)
DEFAULT_MAX_PAGES = 3
MAX_SELECTOR_LENGTH = 300
MAX_TITLE_LENGTH = 300
MAX_LOCATION_LENGTH = 300
MAX_EMPLOYMENT_TYPE_LENGTH = 100
MAX_EXTERNAL_ID_LENGTH = 100
_SCRIPT_PAGE_TEXT = 200  # a page with scripts and less visible text than this needs JavaScript

_WORK_TYPES: dict[str, WorkType] = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on-site": "onsite",
    "on site": "onsite",
}
_FIELD_KEYS = ("location", "posted_date", "employment_type", "work_type", "description")
_CARD_KEYS = frozenset(
    {"listing", "job", "title", "url", "external_id", "no_jobs", "next_page", "max_pages"}
    | {"path_prefix", "detail"}
    | set(_FIELD_KEYS)
)


# --- configuration ------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSelectors:
    """Selectors for the optional job fields, on a card or on a detail page."""

    location: str | None = None
    posted_date: str | None = None
    employment_type: str | None = None
    work_type: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class HtmlConfig:
    """One source's reviewed selector configuration (see ``parse_html_config``)."""

    listing: str
    job: str
    title: str
    url: str
    fields: FieldSelectors = FieldSelectors()
    external_id: str | None = None
    no_jobs: str | None = None
    next_page: str | None = None
    max_pages: int = 1
    path_prefix: str = "/"
    detail: FieldSelectors | None = None


def parse_html_config(data: Any) -> HtmlConfig:
    """Validate a decoded selector configuration; raise ``ValueError`` naming the bad key.

    Keys (all selectors are CSS in Scrapy/parsel syntax; ``::attr(name)`` reads an attribute):

    - required: ``listing`` (the container of the job cards), ``job`` (one card, inside the
      container), ``title`` (inside a card) and ``url`` (inside a card: the link to the job's own
      page; an element match uses its ``href``);
    - optional, inside a card: ``external_id``, ``location``, ``posted_date``,
      ``employment_type``, ``work_type``, ``description``;
    - optional, on the page: ``no_jobs`` (a marker shown when there are no openings) and
      ``next_page`` (the next-page link), with ``max_pages`` (1 to ``MAX_PAGES``, default
      ``DEFAULT_MAX_PAGES``; only with ``next_page``);
    - optional: ``path_prefix`` (job and next-page links must stay under it; default ``/``) and
      ``detail``, an object with selectors for any of ``location``, ``posted_date``,
      ``employment_type``, ``work_type`` and ``description`` on each job's detail page (at
      least one). Detail pages are fetched only when ``detail`` is given.

    Unknown keys are refused, so a typo cannot silently disable a field.
    """
    if not isinstance(data, dict):
        raise ValueError("selector configuration must be a JSON object")
    unknown = sorted(set(data) - _CARD_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s): {', '.join(unknown)}")
    listing = _selector(data, "listing", required=True, element=True)
    job = _selector(data, "job", required=True, element=True)
    title = _selector(data, "title", required=True)
    url = _selector(data, "url", required=True)
    assert listing and job and title and url  # required ones are never None
    next_page = _selector(data, "next_page")
    max_pages = data.get("max_pages")
    if max_pages is None:
        max_pages = DEFAULT_MAX_PAGES if next_page else 1
    elif not next_page:
        raise ValueError("max_pages needs next_page")
    elif not isinstance(max_pages, int) or isinstance(max_pages, bool):
        raise ValueError("max_pages must be an integer")
    elif not 1 <= max_pages <= MAX_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_PAGES}")
    detail = None
    if data.get("detail") is not None:
        raw = data["detail"]
        if not isinstance(raw, dict):
            raise ValueError("detail must be an object")
        extra = sorted(set(raw) - set(_FIELD_KEYS))
        if extra:
            raise ValueError(f"unknown detail key(s): {', '.join(extra)}")
        detail = _field_selectors(raw, "detail.")
        if detail == FieldSelectors():
            raise ValueError("detail needs at least one selector")
    return HtmlConfig(
        listing=listing,
        job=job,
        title=title,
        url=url,
        fields=_field_selectors(data, ""),
        external_id=_selector(data, "external_id"),
        no_jobs=_selector(data, "no_jobs", element=True),
        next_page=next_page,
        max_pages=max_pages,
        path_prefix=_path_prefix(data.get("path_prefix", "/")),
        detail=detail,
    )


def _field_selectors(data: dict[str, Any], prefix: str) -> FieldSelectors:
    values = {key: _selector(data, key, label=prefix + key) for key in _FIELD_KEYS}
    return FieldSelectors(**values)


def _selector(
    data: dict[str, Any],
    key: str,
    *,
    required: bool = False,
    element: bool = False,
    label: str | None = None,
) -> str | None:
    """A CSS selector that compiles; ``element`` ones may not select attributes or text."""
    name = label or key
    value = data.get(key)
    if value is None:
        if required:
            raise ValueError(f"{name} selector is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank selector string")
    text = value.strip()
    if len(text) > MAX_SELECTOR_LENGTH or not text.isprintable():
        raise ValueError(f"{name} selector is too long or contains control characters")
    if element and "::" in text:
        raise ValueError(f"{name} must select elements, not attributes or text")
    try:
        Selector(text="<html></html>", type="html").css(text)
    except Exception as exc:  # noqa: BLE001 - cssselect raises several error types
        raise ValueError(f"{name} is not a valid CSS selector ({type(exc).__name__})") from None
    return text


def _path_prefix(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("path_prefix must be a string starting with '/'")
    if not value.isprintable() or any(ch.isspace() for ch in value):
        raise ValueError("path_prefix must not contain whitespace or control characters")
    if "?" in value or "#" in value or "/../" in f"{value}/" or "/./" in f"{value}/":
        raise ValueError("path_prefix must be a plain path")
    return value


# --- results ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Values:
    """Optional field values read from a card or a detail page."""

    location: str | None = None
    posted_date: date | None = None
    employment_type: str | None = None
    work_type: WorkType | None = None
    description: str | None = None


@dataclass(frozen=True)
class PendingJob:
    """A listing card whose job is completed from its detail page (``url``)."""

    index: int
    url: str
    title: str
    external_id: str | None
    values: _Values


@dataclass(frozen=True)
class ListingPage:
    """One listing page: what it settled, the jobs awaiting their detail page, the next page
    to fetch (already validated) and the job URLs it accepted (to skip repeats later)."""

    extraction: Extraction
    pending: tuple[PendingJob, ...]
    next_page: str | None
    job_urls: tuple[str, ...]


@dataclass(frozen=True)
class _Trusted:
    company_name: str
    listing_url: str
    collected_at: datetime
    company_website: str | None


def _trusted(
    company_name: str, listing_url: str, collected_at: datetime, company_website: str | None
) -> _Trusted:
    if not isinstance(company_name, str) or not company_name.strip():
        raise ValueError("company_name must be a non-blank string")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    try:
        listing = public_url(listing_url)
        website = public_url(company_website) if company_website is not None else None
    except ValueError as exc:
        raise ValueError(f"configuration URL: {exc}") from exc
    if urlsplit(listing).scheme != "https":
        raise ValueError("listing URL must use https")
    return _Trusted(company_name.strip(), listing, collected_at, website)


Report = Callable[[str, FailureKind, str], None]


# --- listing pages ------------------------------------------------------------------------------


def extract_html_listing(
    html: Any,
    *,
    config: HtmlConfig,
    page_url: str,
    listing_url: str,
    company_name: str,
    collected_at: datetime,
    company_website: str | None = None,
    page_number: int = 1,
    visited_pages: Collection[str] = (),
    seen_jobs: Collection[str] = (),
) -> ListingPage:
    """Read one fetched listing page.

    Args:
        html: the page markup (never fetched here, nothing in it is executed).
        config: the source's validated selector configuration.
        page_url: the URL this page was fetched from; relative links resolve against it.
        listing_url: the configured listing URL; its host and port bound every link.
        company_name, company_website: trusted configuration, never read from the page.
        collected_at: timezone-aware collection time (see the module docstring for dates).
        page_number: 1 for the configured listing URL, 2 for the next page, and so on.
        visited_pages: normalized URLs of the listing pages already fetched in this run.
        seen_jobs: normalized job URLs already found in this run (repeats are skipped).

    Raises:
        ValueError: for invalid configuration. Problems in the page never raise.
    """
    trusted = _trusted(company_name, listing_url, collected_at, company_website)
    if not isinstance(html, str):
        return _page(rejected_response(f"page is {type(html).__name__}, expected HTML text"))
    tree = _parse(html)
    containers = tree.css(config.listing)
    if not containers:
        if config.no_jobs and tree.css(config.no_jobs):
            return _page(Extraction((), (), "listing", 0))
        return _page(rejected_response(_missing_container(tree)))

    records: list[JobRecord] = []
    failures: list[ExtractionFailure] = []
    warnings: list[ExtractionWarning] = []
    skipped: list[SkippedJob] = []
    pending: list[PendingJob] = []
    accepted: list[str] = []
    seen = set(seen_jobs)
    cards = [card for container in containers for card in container.css(config.job)]
    for index, card in enumerate(cards):
        outcome = _read_card(card, index, config, page_url, trusted, warnings)
        if isinstance(outcome, ExtractionFailure):
            failures.append(outcome)
            continue
        job_url = outcome.url
        if job_url in seen:
            skipped.append(
                SkippedJob(index, outcome.external_id, "duplicate job link: already found")
            )
            continue
        seen.add(job_url)
        accepted.append(job_url)
        if config.detail is not None:
            pending.append(outcome)
            continue
        built = _build(outcome, outcome.values, page_url, trusted)
        if isinstance(built, JobRecord):
            records.append(built)
        else:
            failures.append(built)

    next_page = _next_page(tree, config, page_url, trusted, page_number, visited_pages, warnings)
    extraction = Extraction(
        tuple(records),
        tuple(failures),
        "listing",
        len(records) + len(failures) + len(skipped),
        tuple(warnings),
        tuple(skipped),
    )
    return ListingPage(extraction, tuple(pending), next_page, tuple(accepted))


def _page(extraction: Extraction) -> ListingPage:
    return ListingPage(extraction, (), None, ())


def _parse(html: str) -> Selector:
    """The page as a selector tree. The markup is first parsed by the HTML5 parser the other
    adapters use (selectolax) and re-serialized, so malformed markup (unclosed tags, stray end
    tags) gets the same structure a browser would show; parsel's own lxml parser would, for
    example, nest an unclosed job card inside the previous one."""
    markup = without_control_characters(html)
    normalized = HTMLParser(markup).html if markup.strip() else None
    return Selector(text=normalized or "<html></html>", type="html")


def _missing_container(tree: Selector) -> str:
    """Why a page without the listing container is unusable (never an empty listing)."""
    has_scripts = bool(tree.css("script") or tree.css("noscript"))
    visible = html_to_text(tree.css("body").get() or "")  # script and noscript text dropped
    if has_scripts and len(visible) < _SCRIPT_PAGE_TEXT:
        return (
            "listing container not found; the page appears to build its content with "
            "JavaScript, which is not supported"
        )
    return "listing container not found (the page layout may have changed)"


def _read_card(
    card: Selector,
    index: int,
    config: HtmlConfig,
    page_url: str,
    trusted: _Trusted,
    warnings: list[ExtractionWarning],
) -> PendingJob | ExtractionFailure:
    errors: list[FieldError] = []

    def fail(field: str, kind: FailureKind, message: str) -> None:
        errors.append(FieldError(field, kind.value, message))

    external_id = _read_external_id(card, config.external_id, fail)

    def warn(field: str, kind: FailureKind, message: str) -> None:
        warnings.append(ExtractionWarning(kind, index, external_id, field, message))

    title = _first(card, config.title)
    if title is None:
        fail("title", FailureKind.MISSING_FIELD, "title is missing")
    elif len(title) > MAX_TITLE_LENGTH:
        fail("title", FailureKind.INVALID_TYPE, f"title is longer than {MAX_TITLE_LENGTH} chars")
    found, raw = _link(card, config.url)
    job_url: str | None = None
    if not found or not raw:
        fail("url", FailureKind.MISSING_FIELD, "job link is missing")
    else:
        job_url, problem = _check_link(raw, page_url, trusted.listing_url, config.path_prefix)
        if problem:
            fail("url", FailureKind.INVALID_URL, f"job link {problem}")
    values = _read_values(card, config.fields, trusted.collected_at, warn)
    if errors:
        return ExtractionFailure(FailureKind(errors[0].code), index, external_id, tuple(errors))
    assert title is not None and job_url is not None
    return PendingJob(index, job_url, title, external_id, values)


def _next_page(
    tree: Selector,
    config: HtmlConfig,
    page_url: str,
    trusted: _Trusted,
    page_number: int,
    visited_pages: Collection[str],
    warnings: list[ExtractionWarning],
) -> str | None:
    """The validated next listing page, or ``None`` (with a warning when one is not followed)."""
    if not config.next_page:
        return None
    found, raw = _link(tree, config.next_page)
    if not found:
        return None  # the last page

    def warn(kind: FailureKind, message: str) -> None:
        warnings.append(ExtractionWarning(kind, None, None, "next_page", message))

    if not raw or not raw.strip():
        warn(
            FailureKind.INVALID_RESPONSE,
            "next-page control is not a link (JavaScript pagination is not supported); "
            "later pages not collected",
        )
        return None
    url, problem = _check_link(raw, page_url, trusted.listing_url, config.path_prefix)
    if url is None:
        warn(FailureKind.INVALID_URL, f"next-page link {problem}; later pages not collected")
        return None
    current = {normalize_url(page_url), *visited_pages}
    if url == normalize_url(page_url):
        return None  # a last page that links to itself
    if url in current:
        warn(FailureKind.INVALID_RESPONSE, "pagination loop: next page was already fetched")
        return None
    if page_number >= config.max_pages:
        warn(
            FailureKind.INVALID_RESPONSE,
            f"page limit ({config.max_pages}) reached; later pages not collected",
        )
        return None
    return url


# --- detail pages -------------------------------------------------------------------------------


def extract_html_detail(
    html: Any,
    *,
    job: PendingJob,
    config: HtmlConfig,
    page_url: str,
    listing_url: str,
    company_name: str,
    collected_at: datetime,
    company_website: str | None = None,
) -> Extraction:
    """Complete one pending job from its fetched detail page (``job_count`` 1).

    The configured ``detail`` selectors fill in the optional fields; a value found here wins over
    the card's. A page that has none of the configured detail fields is not this job's page
    (e.g. a "job no longer available" page) and fails the job.
    """
    trusted = _trusted(company_name, listing_url, collected_at, company_website)
    if config.detail is None:
        raise ValueError("the configuration has no detail selectors")
    if not isinstance(html, str):
        return _job_failure(job, "detail page is not HTML text")
    tree = _parse(html)
    if not any(
        tree.css(selector) for selector in vars(config.detail).values() if selector is not None
    ):
        return _job_failure(job, "detail page has none of the configured detail fields")
    warnings: list[ExtractionWarning] = []

    def warn(field: str, kind: FailureKind, message: str) -> None:
        warnings.append(
            ExtractionWarning(kind, job.index, job.external_id, f"detail.{field}", message)
        )

    found = _read_values(tree, config.detail, trusted.collected_at, warn)
    merged = _Values(
        **{
            key: value if (value := getattr(found, key)) is not None else getattr(job.values, key)
            for key in _FIELD_KEYS
        }
    )
    built = _build(job, merged, page_url, trusted)
    if isinstance(built, JobRecord):
        return Extraction((built,), (), "detail", 1, tuple(warnings))
    return Extraction((), (built,), "detail", 1, tuple(warnings))


def detail_unavailable(job: PendingJob, reason: str) -> Extraction:
    """The job's detail page could not be fetched (e.g. HTTP 404): the job fails alone."""
    return _job_failure(job, f"detail page unavailable ({reason})")


def _job_failure(job: PendingJob, message: str) -> Extraction:
    error = FieldError("(detail page)", FailureKind.INVALID_RESPONSE.value, message)
    failure = ExtractionFailure(FailureKind.INVALID_RESPONSE, job.index, job.external_id, (error,))
    return Extraction((), (failure,), "detail", 1)


# --- one job ------------------------------------------------------------------------------------


def _build(
    job: PendingJob, values: _Values, page_url: str, trusted: _Trusted
) -> JobRecord | ExtractionFailure:
    result = validate_job(
        {
            "title": job.title,
            "company_name": trusted.company_name,
            "apply_url": job.url,
            "source_url": page_url,
            "platform": PLATFORM,
            "collected_at": trusted.collected_at,
            "company_website": trusted.company_website,
            "location": values.location,
            "work_type": values.work_type,
            "employment_type": values.employment_type,
            "posted_date": values.posted_date,
            "posted_date_source": "page_text" if values.posted_date else "unknown",
            "external_id": job.external_id,
            "description_snippet": values.description,
        }
    )
    if isinstance(result, JobRecord):
        return result
    return from_rejection(result, job.index, job.external_id)


def _read_values(
    scope: Selector, selectors: FieldSelectors, now: datetime, warn: Report
) -> _Values:
    location = None
    if selectors.location:
        seen: set[str] = set()
        unique: list[str] = []
        for name in _texts(scope, selectors.location):
            if name.casefold() not in seen:
                seen.add(name.casefold())
                unique.append(name)
        location = "; ".join(unique) or None
        if location and len(location) > MAX_LOCATION_LENGTH:
            warn("location", FailureKind.INVALID_TYPE, "location text is too long; location unset")
            location = None
    posted = None
    if selectors.posted_date:
        text = _first(scope, selectors.posted_date)
        if text:
            posted = parse_posted_date(text, now)
            if posted is None:
                warn(
                    "posted_date",
                    FailureKind.INVALID_DATE,
                    "posted_date text is not a recognizable date; posting date unknown",
                )
    employment = None
    if selectors.employment_type:
        employment = _first(scope, selectors.employment_type)
        if employment and len(employment) > MAX_EMPLOYMENT_TYPE_LENGTH:
            warn(
                "employment_type",
                FailureKind.INVALID_TYPE,
                "employment_type text is too long; employment type unset",
            )
            employment = None
    work_type: WorkType | None = None
    if selectors.work_type:
        text = _first(scope, selectors.work_type)
        if text:
            work_type = _WORK_TYPES.get(text.casefold())
            if work_type is None:
                warn(
                    "work_type",
                    FailureKind.INVALID_TYPE,
                    "work_type text is not remote, hybrid or on-site; work type unknown",
                )
    description = None
    if selectors.description:
        description = make_snippet(" ".join(_texts(scope, selectors.description)))
    return _Values(location, posted, employment, work_type, description)


def _read_external_id(card: Selector, selector: str | None, fail: Report) -> str | None:
    """A configured job id is required on every card: falling back to the URL for some cards
    would give the same job a different identity from one run to the next."""
    if selector is None:
        return None
    value = _first(card, selector)
    if value is None:
        fail("external_id", FailureKind.MISSING_FIELD, "external_id is missing")
        return None
    if len(value) > MAX_EXTERNAL_ID_LENGTH:
        fail("external_id", FailureKind.INVALID_TYPE, "external_id is too long")
        return None
    return value


# --- reading the markup -------------------------------------------------------------------------


def _texts(scope: Selector, selector: str) -> list[str]:
    """Clean text of each match: an attribute or text match as it is, an element as readable
    text (scripts, styles and similar content dropped). Blank matches are left out."""
    texts: list[str] = []
    for match in scope.css(selector):
        root = match.root
        text = clean_text(root) if isinstance(root, str) else html_to_text(match.get())
        if text:
            texts.append(text)
    return texts


def _first(scope: Selector, selector: str) -> str | None:
    texts = _texts(scope, selector)
    return texts[0] if texts else None


def _link(scope: Selector, selector: str) -> tuple[bool, str | None]:
    """(whether the selector matched, the first match's link): an attribute match as it is,
    an element's ``href``."""
    for match in scope.css(selector):
        root = match.root
        return True, (root if isinstance(root, str) else match.attrib.get("href"))
    return False, None


def _check_link(
    raw: str, page_url: str, listing_url: str, path_prefix: str
) -> tuple[str | None, str | None]:
    """``(normalized URL, None)`` for a usable link, else ``(None, problem)``. The problem text
    never repeats the link."""
    text = raw.strip()
    if text.startswith("#") or text.lower().startswith("javascript:"):
        return None, "needs JavaScript or is an in-page anchor (not supported)"
    try:
        url = public_url(urljoin(page_url, text))
    except ValueError as exc:
        return None, f"is not a public URL ({exc})"
    target, source = urlsplit(url), urlsplit(listing_url)
    if target.scheme != "https":
        return None, "is not an https URL"
    if (target.hostname, target.port or 443) != (source.hostname, source.port or 443):
        return None, "points to another host than the source"
    if not target.path.startswith(path_prefix):
        return None, "is outside the configured path_prefix"
    return url, None


def loads_html_config(text: str) -> HtmlConfig:
    """``parse_html_config`` over JSON text; ``ValueError`` for malformed JSON."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"selector configuration is not valid JSON ({exc.msg})") from None
    return parse_html_config(data)
