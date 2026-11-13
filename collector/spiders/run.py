"""Run one crawl over explicitly configured sources, with Scrapy's built-in safety features.

Everything here uses stock Scrapy (no custom HTTP client, retry framework or rate limiter)
plus one tiny downloader middleware, ``SourceOnlyMiddleware``. Safety comes from settings —
built-in robots.txt compliance, a descriptive User-Agent, conservative concurrency, a download
delay, request timeouts, limited retries (never for HTTP 429), a response size cap and bounded
redirects — and from that middleware, which refuses, **before any network traffic**, every
request (including each redirect hop) that is not HTTPS to exactly the configured source host
and port, or that carries credentials or targets localhost / a non-global address.
``allowed_domains`` on each spider stays as a second, coarser filter.

Sources are crawled **one after another**, with the download delay between them: every Scrapy
crawler has its own downloader, so crawling sources in parallel would ignore the per-domain
limits for sources that share a host (e.g. all Greenhouse boards share one API host).

Importing this module does nothing; only ``crawl_sources`` makes network requests (to the
configured sources and their hosts' ``robots.txt``). It starts the Twisted reactor, which can
run only once per process, so it is meant for the CLI. Tests run it in a subprocess.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import IgnoreRequest
from twisted.internet import defer, task

from collector.adapters.common import Extraction, public_url
from collector.models import JobRecord
from collector.settings import Settings, get_settings
from collector.spiders.platforms import SPIDERS, FetchFailure
from collector.spiders.sources import Source

# Generic, descriptive client name: says what is crawling and that it behaves politely.
USER_AGENT = "clyptus-collector/0.1 (public job-postings collector; obeys robots.txt)"


class UnsafeDestination(IgnoreRequest):
    """A request to somewhere other than the configured source. Its name is the reported reason;
    its message never contains the URL (it could carry a query string or credentials)."""


class SourceOnlyMiddleware:
    """Downloader middleware: drop any request that is not HTTPS to exactly the configured
    source's host and port. Runs before the network, so a redirect to an unsafe destination is
    never fetched (the spider's errback then reports the source as failed). Credentials in the
    URL, ``localhost`` and non-global IP addresses are refused as well. It judges names and
    literal IPs only: it does not resolve DNS (see the report on remaining limitations)."""

    def __init__(self, crawler: Any) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Any) -> "SourceOnlyMiddleware":
        return cls(crawler)

    def process_request(self, request: Any, spider: Any = None) -> None:
        source = urlsplit(self.crawler.spider.listing_url())
        try:
            target = urlsplit(request.url)
            public_url(request.url)  # http(s) only, no credentials, public host, global IP
            allowed = (
                target.scheme == "https"
                and target.hostname == source.hostname
                and (target.port or 443) == (source.port or 443)
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise UnsafeDestination("request is not an HTTPS request to the configured source")


def scrapy_settings(settings: Settings | None = None) -> dict[str, object]:
    """Scrapy settings built from the project configuration (``COLLECTOR_*`` variables)."""
    settings = settings if settings is not None else get_settings()
    return {
        "ROBOTSTXT_OBEY": settings.robots_txt_obey,
        "USER_AGENT": USER_AGENT,
        "CONCURRENT_REQUESTS": settings.concurrent_requests,
        "CONCURRENT_REQUESTS_PER_DOMAIN": settings.concurrent_requests_per_domain,
        "DOWNLOAD_DELAY": settings.download_delay_seconds,
        "DOWNLOAD_TIMEOUT": settings.request_timeout_seconds,
        "RETRY_TIMES": settings.retry_times,
        "AUTOTHROTTLE_ENABLED": settings.autothrottle_enabled,
        "COOKIES_ENABLED": False,  # the public endpoints need no session state
        "TELNETCONSOLE_ENABLED": False,  # no listening console ...
        "REMOTE_CONTROL_ENABLED": False,  # ... and no code-executing HTTP server (Scrapy 2.20+)
        "DEPTH_LIMIT": 1,  # spiders issue one request each and follow nothing
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408],  # not 429: back off, don't retry
        "REDIRECT_MAX_TIMES": 3,
        "DOWNLOADER_MIDDLEWARES": {f"{__name__}.SourceOnlyMiddleware": 10},  # before robots etc.
        "DOWNLOAD_MAXSIZE": 20 * 1024 * 1024,  # job listings are small; refuse anything huge
        "LOG_LEVEL": settings.log_level,
    }


@dataclass(frozen=True)
class SourceResult:
    """Everything one configured source produced in one run: the adapter's extractions
    (records, failures, warnings, skipped — untouched) and the transport failures."""

    source: Source
    extractions: tuple[Extraction, ...]
    fetch_failures: tuple[FetchFailure, ...]

    @property
    def records(self) -> tuple[JobRecord, ...]:
        return tuple(record for extraction in self.extractions for record in extraction.records)

    @property
    def blocked(self) -> bool:
        """Whether the source refused access (HTTP 403/429 or a challenge page)."""
        return any(failure.blocked for failure in self.fetch_failures)


def crawl_sources(
    sources: Iterable[Source],
    *,
    settings: Settings | None = None,
    collected_at: datetime | None = None,
) -> list[SourceResult]:
    """Fetch every configured source once and return its results, in configuration order.

    Makes network requests and starts the Twisted reactor (once per process). ``collected_at``
    is normally left ``None`` so each response is stamped when it arrives; passing a fixed
    timezone-aware time is for controlled runs only.
    """
    configured = list(sources)
    if not configured:
        return []
    config = scrapy_settings(settings)
    process = CrawlerProcess(config)
    crawlers = [(source, process.create_crawler(SPIDERS[source.platform])) for source in configured]
    delay = float(config["DOWNLOAD_DELAY"])  # type: ignore[arg-type]

    @defer.inlineCallbacks
    def crawl_in_sequence():  # noqa: ANN202
        try:
            for position, (source, crawler) in enumerate(crawlers):
                if position:
                    # Imported only now: the first crawl installed the reactor Scrapy is
                    # configured for, and importing the default reactor earlier would replace it.
                    from twisted.internet import reactor

                    yield task.deferLater(reactor, delay, lambda: None)
                try:
                    yield process.crawl(crawler, source=source, collected_at=collected_at)
                except Exception:  # noqa: BLE001 - one failed source must not stop the others
                    pass  # reported below: the spider produced neither results nor failures
        finally:
            from twisted.internet import reactor

            reactor.stop()

    crawl_in_sequence()
    process.start(stop_after_crawl=False)  # blocks until crawl_in_sequence stops the reactor

    results: list[SourceResult] = []
    for source, crawler in crawlers:
        spider = crawler.spider
        if spider is None:  # the spider never opened; report it as a fetch failure
            failure = FetchFailure(url="", reason="spider did not start")
            results.append(SourceResult(source, (), (failure,)))
        elif not spider.extractions and not spider.fetch_failures:
            # No response and no error: the request was dropped (e.g. a filtered redirect).
            failure = FetchFailure(url=spider.listing_url(), reason="no response received")
            results.append(SourceResult(source, (), (failure,)))
        else:
            results.append(
                SourceResult(source, tuple(spider.extractions), tuple(spider.fetch_failures))
            )
    return results
