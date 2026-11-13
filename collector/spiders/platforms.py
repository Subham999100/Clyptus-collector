"""Scrapy spiders: fetch one configured source each and hand responses to the existing adapters.

Responsibilities stay split exactly as in the earlier phases: these spiders only **fetch**; the
Phase 3 adapters extract and normalize; Phase 1 validates, filters and de-duplicates. No parsing
logic is duplicated here.

Each spider issues **one listing request** for its configured source and follows nothing: no
link discovery, no pagination, no JavaScript, no browser. ``allowed_domains`` is pinned to the
source's host, so Scrapy's built-in offsite filtering also stops redirects that leave it.

Results are collected on the spider instance, using the existing result structures:

- ``extractions`` — the adapters' ``Extraction`` objects (records, failures, warnings, skipped),
  untouched. A body that cannot even be decoded becomes one ``invalid_response`` failure.
- ``fetch_failures`` — transport problems (HTTP error status, DNS, timeout, a robots.txt denial)
  and access blocks, reported **separately** from extraction failures. The reason names the
  status or failure type only; response content is never echoed or logged. HTTP 403, HTTP 429
  and challenge/CAPTCHA pages are flagged ``blocked``: they are recorded, never retried or
  worked around, and never reported as an empty job listing.

The collection time is read from the clock once per response (adapters never read the clock);
tests inject ``collected_at`` instead.
"""

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

import scrapy
from scrapy.http import Response
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.python.failure import Failure

from collector.adapters.ashby import extract_ashby_jobs
from collector.adapters.common import Extraction, rejected_response
from collector.adapters.greenhouse import extract_greenhouse_jobs
from collector.adapters.json_ld import extract_json_ld_jobs
from collector.adapters.lever import extract_lever_jobs
from collector.spiders.sources import Source

__all__ = [
    "SPIDERS",
    "AshbySpider",
    "FetchFailure",
    "GreenhouseSpider",
    "JsonLdSpider",
    "LeverSpider",
]


# Markers of an access-challenge or CAPTCHA page, matched case-insensitively in the first
# ``_CHALLENGE_SCAN_CHARS`` of a body that produced no job. Detection only: nothing is solved.
_CHALLENGE_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "/cdn-cgi/challenge-platform",
    "cf-chl-",
    "g-recaptcha",
    "h-captcha",
    "hcaptcha.com",
    "px-captcha",
    "captcha-delivery.com",
    "verify you are human",
    "are you a robot",
)
_CHALLENGE_SCAN_CHARS = 100_000
_BLOCKING_STATUSES = frozenset({403, 429})


@dataclass(frozen=True)
class FetchFailure:
    """One request that produced no usable response. ``reason`` names the HTTP status or the
    failure type only — never response content, headers or credentials. ``blocked`` marks an
    access block (HTTP 403/429 or a challenge page); ``url`` carries no query string."""

    url: str
    reason: str
    blocked: bool = False


def _safe_url(url: object) -> str:
    """The URL without credentials, query string and fragment, which could hold secrets."""
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _is_challenge_header(response: Response) -> bool:
    mitigated = response.headers.get("cf-mitigated")
    return mitigated is not None and mitigated.lower() == b"challenge"


class _SourceSpider(scrapy.Spider):
    """Base: one configured ``Source`` in, results accumulated on the instance."""

    platform: ClassVar[str]

    def __init__(self, source: Source, collected_at: datetime | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if source.platform != self.platform:
            raise ValueError(f"source platform {source.platform!r} is not {self.platform!r}")
        if collected_at is not None and (
            collected_at.tzinfo is None or collected_at.utcoffset() is None
        ):
            raise ValueError("collected_at must be timezone-aware")
        self.source = source
        self.allowed_domains = [self._host()]
        self._collected_at = collected_at
        self.extractions: list[Extraction] = []
        self.fetch_failures: list[FetchFailure] = []

    def _host(self) -> str:
        host = urlsplit(self.listing_url()).hostname
        assert host is not None  # listing URLs are built from validated sources
        return host

    def listing_url(self) -> str:
        raise NotImplementedError

    def extract(self, response: Response) -> Extraction:
        raise NotImplementedError

    # Scrapy < 2.13 calls start_requests(); Scrapy >= 2.13 calls start(), which replaces it
    # (the default start() is the only thing that would consult start_requests()). Both yield
    # the same single request, so exactly one initial request exists on every supported version.
    def start_requests(self) -> Iterator[scrapy.Request]:
        yield scrapy.Request(self.listing_url(), callback=self.parse, errback=self.on_error)

    async def start(self) -> AsyncIterator[scrapy.Request]:
        for request in self.start_requests():
            yield request

    def parse(self, response: Response) -> None:
        if urlsplit(str(response.url)).scheme != "https":
            # Every configured source is HTTPS, so this can only be a redirect downgrade: the
            # content could have been tampered with, so it is not extracted.
            self.fetch_failures.append(
                FetchFailure(_safe_url(response.url), "response from a non-HTTPS URL")
            )
            return
        if _is_challenge_header(response):
            self._record_block(response.url, "access challenge page")
            return
        try:
            extraction = self.extract(response)
        except Exception as exc:  # noqa: BLE001 - one bad response must not end the crawl
            # Only the exception type is kept: its message could contain response content.
            extraction = rejected_response(f"extraction error: {type(exc).__name__}")
        no_jobs = extraction.response_type is None or not extraction.job_count
        if not extraction.records and no_jobs and self.looks_like_challenge(response):
            self._record_block(response.url, "access challenge page")
            return
        self.extractions.append(extraction)

    def on_error(self, failure: Failure) -> None:
        """Record a transport failure (never raises, never retries itself — Scrapy's built-in
        retry middleware has already given up by the time this runs)."""
        if failure.check(HttpError):
            response = failure.value.response
            status = response.status
            if status in _BLOCKING_STATUSES or _is_challenge_header(response):
                self._record_block(response.url, f"HTTP {status}")
            else:
                self.fetch_failures.append(FetchFailure(_safe_url(response.url), f"HTTP {status}"))
            return
        request = getattr(failure, "request", None)
        url = request.url if request is not None else self.listing_url()
        self.fetch_failures.append(FetchFailure(_safe_url(url), failure.type.__name__))

    def _record_block(self, url: object, reason: str) -> None:
        self.fetch_failures.append(FetchFailure(_safe_url(url), reason, blocked=True))

    def looks_like_challenge(self, response: Response) -> bool:
        """Whether a response that produced no jobs is a challenge/CAPTCHA page."""
        text = self.body_text(response)
        if text is None:
            return False
        head = text[:_CHALLENGE_SCAN_CHARS].lower()
        return any(marker in head for marker in _CHALLENGE_MARKERS)

    def collected_at(self) -> datetime:
        return self._collected_at if self._collected_at is not None else datetime.now(UTC)

    def body_text(self, response: Response) -> str | None:
        """The response body as text, or ``None`` for a non-text body."""
        try:
            return response.text
        except AttributeError:  # a plain (non-text) Response has no .text
            return None

    def decoded_json(self, response: Response) -> tuple[Any, Extraction | None]:
        """The decoded JSON body, or an ``invalid_response`` extraction when it is unusable."""
        text = self.body_text(response)
        if text is None:
            return None, rejected_response("response body is not text")
        try:
            return json.loads(text), None
        except ValueError:
            return None, rejected_response("response body is not valid JSON")


class GreenhouseSpider(_SourceSpider):
    """Greenhouse Job Board API listing (``?content=true`` for descriptions)."""

    name = "greenhouse"
    platform = "greenhouse"

    def listing_url(self) -> str:
        board = self.source.identifier
        return f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"

    def extract(self, response: Response) -> Extraction:
        payload, problem = self.decoded_json(response)
        if problem is not None:
            return problem
        return extract_greenhouse_jobs(
            payload,
            company_name=self.source.company_name,
            board_token=self.source.identifier,
            source_url=response.url,
            collected_at=self.collected_at(),
            company_website=self.source.company_website,
        )


class LeverSpider(_SourceSpider):
    """Lever Postings API listing. The adapter keeps ignoring ``createdAt`` completely: every
    Lever record stays ``posted_date=None`` / ``posted_date_source="unknown"``."""

    name = "lever"
    platform = "lever"

    def listing_url(self) -> str:
        return f"https://api.lever.co/v0/postings/{self.source.identifier}?mode=json"

    def extract(self, response: Response) -> Extraction:
        payload, problem = self.decoded_json(response)
        if problem is not None:
            return problem
        return extract_lever_jobs(
            payload,
            company_name=self.source.company_name,
            site_slug=self.source.identifier,
            source_url=response.url,
            collected_at=self.collected_at(),
            company_website=self.source.company_website,
        )


class AshbySpider(_SourceSpider):
    """Ashby job-board API listing. The adapter's skip rule (``isListed: false``) and
    ``publishedAt`` handling pass through unchanged in ``Extraction.skipped``/``records``."""

    name = "ashby"
    platform = "ashby"

    def listing_url(self) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{self.source.identifier}"

    def extract(self, response: Response) -> Extraction:
        payload, problem = self.decoded_json(response)
        if problem is not None:
            return problem
        return extract_ashby_jobs(
            payload,
            company_name=self.source.company_name,
            board_name=self.source.identifier,
            source_url=response.url,
            collected_at=self.collected_at(),
            company_website=self.source.company_website,
        )


class JsonLdSpider(_SourceSpider):
    """One explicitly configured public HTTPS job page. The HTML is handed to the JSON-LD
    adapter as text; nothing is executed and no other page is ever requested."""

    name = "jsonld"
    platform = "jsonld"

    def listing_url(self) -> str:
        return self.source.identifier  # the validated HTTPS page URL itself

    def extract(self, response: Response) -> Extraction:
        text = self.body_text(response)
        if text is None:
            return rejected_response("response body is not text")
        return extract_json_ld_jobs(
            text,
            page_url=response.url,
            collected_at=self.collected_at(),
            company_name=self.source.company_name,
            company_website=self.source.company_website,
        )


SPIDERS: dict[str, type[_SourceSpider]] = {
    spider.platform: spider for spider in (GreenhouseSpider, LeverSpider, AshbySpider, JsonLdSpider)
}
