"""Run one crawl over explicitly configured sources, with Scrapy's built-in safety features.

Everything here uses stock Scrapy (no custom HTTP client, retry framework or rate limiter)
plus two small pieces of our own. Safety comes from settings — built-in robots.txt compliance,
a descriptive User-Agent, conservative concurrency, a download delay, request timeouts, limited
retries (never for HTTP 429), a response size cap, bounded redirects, verified TLS certificates
and no proxies — and from:

- ``SourceOnlyMiddleware``, which refuses, **before any network traffic**, every request
  (including each redirect hop) that is not HTTPS to exactly the configured source host and
  port, or that carries credentials or a proxy, or targets localhost / a non-global IP literal.
- ``PublicOnlyResolver``, Twisted's name resolver for the crawl. Every address the downloader
  can connect to (listings, redirects, retries, ``robots.txt``) is validated at the moment of
  the connection, because the downloader connects to exactly the addresses it delivers.

``allowed_domains`` on each spider stays as a second, coarser filter.

Sources are crawled **one after another**, with the download delay between them: every Scrapy
crawler has its own downloader, so crawling sources in parallel would ignore the per-domain
limits for sources that share a host (e.g. all Greenhouse boards share one API host).

Importing this module does nothing; only ``crawl_sources`` makes network requests (to the
configured sources and their hosts' ``robots.txt``). It starts the Twisted reactor, which can
run only once per process, so it is meant for the CLI. Tests run it in a subprocess.
"""

import ipaddress
import logging
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import IgnoreRequest
from scrapy.settings import default_settings
from twisted.internet import defer, task
from twisted.internet.address import IPv4Address, IPv6Address
from twisted.internet.interfaces import IHostnameResolver, IHostResolution
from twisted.internet.threads import deferToThreadPool
from zope.interface import implementer

from collector.adapters.common import Extraction, public_url
from collector.models import JobRecord
from collector.settings import Settings, get_settings
from collector.spiders.platforms import SPIDERS, FetchFailure
from collector.spiders.sources import Source

# Generic, descriptive client name: says what is crawling and that it behaves politely.
USER_AGENT = "clyptus-collector/0.1 (public job-postings collector; obeys robots.txt)"


logger = logging.getLogger(__name__)

# IPv6 ranges that embed an IPv4 address (NAT64, 6to4, Teredo). ``is_global`` accepts them, but
# they can reach private IPv4 hosts, so they are refused outright.
_TRANSLATED_IPV6 = tuple(
    ipaddress.ip_network(net)
    for net in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)


def is_public_address(value: str) -> bool:
    """Whether ``value`` is a globally routable public unicast IP address; anything else, and
    anything that does not parse, is refused (fail closed). An IPv4-mapped IPv6 address is
    judged as the IPv4 address it maps to."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif any(address in net for net in _TRANSLATED_IPV6) or address.is_site_local:
            return False
    return address.is_global and not (
        address.is_multicast  # Python's is_global accepts multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_private
    )


def _system_lookup(host: str) -> list[str]:
    """Every address (IPv4 and IPv6) the system resolver returns for ``host``. Blocking."""
    found = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in found))


@implementer(IHostResolution)
class _Resolution:
    def __init__(self, name: str) -> None:
        self.name = name

    def cancel(self) -> None:
        return None


@implementer(IHostnameResolver)
class PublicOnlyResolver:
    """Twisted name resolver that only ever hands the downloader public addresses.

    Twisted's ``HostnameEndpoint`` (used by Scrapy's HTTP/1.1 handler for every request:
    listings, redirects, retries and ``robots.txt``) connects to exactly the addresses this
    resolver delivers; it does not resolve again. Validating here therefore binds the check to
    the connection itself, which closes the check-then-connect (DNS rebinding) gap: a name that
    later resolves to an internal address is simply refused at that moment. The hostname is not
    touched, so TLS SNI and certificate verification still use it. Nothing is cached.

    If *any* address in an answer is not public, or the lookup fails, no address is delivered
    (the endpoint fails with ``DNSLookupError``, which Scrapy's retry middleware retries through
    this same check). Addresses are never logged or put in error messages.
    """

    def __init__(self, reactor: Any, timeout: float = 60.0) -> None:
        self.reactor = reactor
        self.timeout = timeout

    @classmethod
    def from_crawler(cls, crawler: Any, reactor: Any) -> "PublicOnlyResolver":
        return cls(reactor, crawler.settings.getfloat("DNS_TIMEOUT"))

    def install_on_reactor(self) -> None:
        self.reactor.installNameResolver(self)

    def _lookup(self, host: str) -> "defer.Deferred[list[str]]":
        pool = self.reactor.getThreadPool()
        return deferToThreadPool(self.reactor, pool, _system_lookup, host)

    def resolveHostName(
        self,
        resolutionReceiver: Any,
        hostName: str,
        portNumber: int = 0,
        addressTypes: Sequence[type] | None = None,
        transportSemantics: str = "TCP",
    ) -> Any:
        resolution = _Resolution(hostName)
        resolutionReceiver.resolutionBegan(resolution)

        def deliver(answers: list[str]) -> None:
            if answers and all(is_public_address(answer) for answer in answers):
                for answer in answers:
                    kind = IPv6Address if ":" in answer else IPv4Address
                    if addressTypes is None or kind in addressTypes:
                        resolutionReceiver.addressResolved(kind("TCP", answer, portNumber))
            else:
                logger.warning("refusing to connect to %s: no usable public address", hostName)
            resolutionReceiver.resolutionComplete()

        def failed(_failure: Any) -> None:
            resolutionReceiver.resolutionComplete()  # nothing delivered: the connect fails

        lookup = self._lookup(hostName)
        lookup.addTimeout(self.timeout, self.reactor)
        lookup.addCallbacks(deliver, failed)
        return resolution


class UnsafeDestination(IgnoreRequest):
    """A request to somewhere other than the configured source. Its name is the reported reason;
    its message never contains the URL (it could carry a query string or credentials)."""


class SourceOnlyMiddleware:
    """Downloader middleware: drop any request that is not HTTPS to exactly the configured
    source's host and port. Runs before the network, so a redirect to an unsafe destination is
    never fetched (the spider's errback then reports the source as failed). Credentials in the
    URL, ``localhost`` and non-global IP addresses are refused as well, and so is any request
    with a proxy set. It judges names and literal IPs only; what a name *resolves to* is checked
    where the connection is made, by ``PublicOnlyResolver``."""

    def __init__(self, crawler: Any) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Any) -> "SourceOnlyMiddleware":
        return cls(crawler)

    def process_request(self, request: Any, spider: Any = None) -> None:
        source = urlsplit(self.crawler.spider.listing_url())
        if getattr(request, "meta", {}).get("proxy"):
            raise UnsafeDestination("requests must not use a proxy")
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


def _network_security_settings() -> dict[str, object]:
    """Settings that vary by Scrapy version: the resolver that validates every connection
    address, and TLS certificate verification (Scrapy's default does not verify certificates)."""
    resolver = f"{__name__}.PublicOnlyResolver"
    config: dict[str, object] = {"DNSCACHE_ENABLED": False}  # every connection is re-validated
    if hasattr(default_settings, "TWISTED_DNS_RESOLVER"):  # newer Scrapy
        config["TWISTED_DNS_RESOLVER"] = resolver
    else:
        config["DNS_RESOLVER"] = resolver
    if hasattr(default_settings, "DOWNLOAD_VERIFY_CERTIFICATES"):
        config["DOWNLOAD_VERIFY_CERTIFICATES"] = True
    else:  # older Scrapy: the browser-like factory verifies the certificate and hostname
        config["DOWNLOADER_CLIENTCONTEXTFACTORY"] = (
            "scrapy.core.downloader.contextfactory.BrowserLikeContextFactory"
        )
    return config


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
        "DOWNLOADER_MIDDLEWARES": {
            f"{__name__}.SourceOnlyMiddleware": 10,  # before robots etc.
            # Never route through a proxy: this middleware reads HTTP(S)_PROXY from the
            # environment, which would bypass the destination checks.
            "scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware": None,
        },
        **_network_security_settings(),
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
