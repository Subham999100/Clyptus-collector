"""Protected page fetching for source discovery: the Phase 4 crawler, one page at a time.

``PageSpider`` is the existing ``_SourceSpider`` with a single HTTPS page as its "source", so
everything that guards collection guards discovery too: ``SourceOnlyMiddleware`` (HTTPS to
exactly the requested host and port, no credentials or proxies), ``PublicOnlyResolver``
(connection-time public-address check), verified TLS, robots.txt, the download delay, retry and
size limits, and HTTP 403/429 and challenge-page detection. Nothing is relaxed. A redirect to
another host is refused before any request, as in collection; its target is handed to discovery
as ``Redirected`` so it can be *judged* (e.g. a careers page redirecting to a job board) without
ever being fetched. A same-domain redirect target (``example.com`` -> ``www.example.com``)
is requested by discovery as a new page, with all checks again.

``crawl_discovery`` runs the discovery steps of many companies in one Scrapy run (the Twisted
reactor starts once per process), one fetch at a time with the download delay between fetches,
and reports each company's result as soon as it is known. Importing this module does nothing.
"""

from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from scrapy import signals
from scrapy.crawler import CrawlerProcess
from scrapy.http import Response
from twisted.internet import defer, task
from twisted.python.failure import Failure

from collector.adapters.common import Extraction, rejected_response
from collector.companies.discovery import Discovery, FetchError, Page, Redirected, Steps
from collector.settings import Settings
from collector.spiders.platforms import _SourceSpider
from collector.spiders.run import UnsafeDestination, scrapy_settings
from collector.spiders.sources import Source

Job = tuple[str, Steps]  # a key and its discovery steps
OnDone = Callable[[str, "Discovery | Exception"], None]


class PageSpider(_SourceSpider):
    """Fetch one public HTTPS page; keep its text, or the redirect target it was refused."""

    name = "discovery-page"
    platform = "jsonld"  # the base class validates the URL exactly as for a JSON-LD page source

    def __init__(self, url: str, **kwargs: Any) -> None:
        super().__init__(Source("jsonld", "source discovery", url), **kwargs)
        self.page: Page | None = None
        self.redirect: str | None = None

    @classmethod
    def from_crawler(cls, crawler: Any, *args: Any, **kwargs: Any) -> "PageSpider":
        spider = super().from_crawler(crawler, *args, **kwargs)
        # The spider schedules only its one request, so any other scheduled request is a
        # redirect hop. Newer Scrapy drops a hop to a host outside ``allowed_domains`` right
        # here, silently (no errback), so the target is recorded when it is scheduled.
        crawler.signals.connect(spider._scheduled, signal=signals.request_scheduled)
        return spider

    def _scheduled(self, request: Any, spider: Any = None) -> None:
        if urlsplit(request.url).hostname != urlsplit(self.listing_url()).hostname:
            self.redirect = request.url

    def listing_url(self) -> str:
        return self.source.identifier

    def extract(self, response: Response) -> Extraction:
        text = self.body_text(response)
        if text is None:
            try:
                text = response.body.decode("utf-8")
            except UnicodeDecodeError:
                return rejected_response("response body is not text")
        self.page = Page(str(response.url), text)
        # No jobs here: the base class then also checks the body for a challenge page.
        return Extraction((), (), "listing", 0)

    def on_error(self, failure: Failure) -> None:
        request = getattr(failure, "request", None)
        if (
            failure.check(UnsafeDestination)
            and request is not None
            and request.url != self.listing_url()
        ):
            self.redirect = request.url  # refused before any request was made
        super().on_error(failure)


def outcome(spider: PageSpider | None) -> Page | FetchError:
    """What one fetch produced, as discovery expects it. Reasons never contain URLs."""
    if spider is None:
        return FetchError("fetch did not start")
    if spider.page is not None and not spider.fetch_failures:
        return spider.page
    if spider.redirect is not None:
        return Redirected(spider.redirect)
    if spider.fetch_failures:
        failure = spider.fetch_failures[0]
        reason = "refused by robots.txt" if failure.reason == "IgnoreRequest" else failure.reason
        return FetchError(f"blocked: {reason}" if failure.blocked else reason)
    if spider.page is None:
        return FetchError("no response received")
    return spider.page


def crawl_discovery(
    jobs: Sequence[Job], on_done: OnDone, *, settings: Settings | None = None
) -> None:
    """Drive each job's discovery steps to the end with protected fetches, calling
    ``on_done(key, result)`` per job (``result`` is the exception if the steps failed).

    Makes network requests and starts the Twisted reactor (once per process). An exception
    raised by ``on_done`` stops the run and is re-raised here.
    """
    pending: list[tuple[str, Steps, str]] = []
    for key, steps in jobs:
        try:
            pending.append((key, steps, next(steps)))
        except StopIteration as stop:  # decided without fetching anything
            on_done(key, stop.value)
        except Exception as exc:  # noqa: BLE001 - recorded as this company's failure
            on_done(key, exc)
    if not pending:
        return
    config = scrapy_settings(settings)
    process = CrawlerProcess(config)
    delay = float(config["DOWNLOAD_DELAY"])  # type: ignore[arg-type]
    errors: list[BaseException] = []

    @defer.inlineCallbacks
    def run_in_sequence():  # noqa: ANN202
        fetched = 0
        try:
            for key, steps, first in pending:
                url = first
                result: Discovery | Exception
                try:
                    while True:
                        if fetched:
                            # Imported only now (see crawl_sources): the first crawl installed
                            # the reactor Scrapy is configured for.
                            from twisted.internet import reactor

                            yield task.deferLater(reactor, delay, lambda: None)
                        fetched += 1
                        crawler = process.create_crawler(PageSpider)
                        try:
                            yield process.crawl(crawler, url=url)
                        except Exception:  # noqa: BLE001 - reported through outcome()
                            pass
                        fetched_page = outcome(crawler.spider)
                        if isinstance(fetched_page, FetchError):
                            url = steps.throw(fetched_page)
                        else:
                            url = steps.send(fetched_page)
                except StopIteration as stop:
                    result = stop.value
                except Exception as exc:  # noqa: BLE001 - one company must not stop the rest
                    result = exc
                on_done(key, result)
        except BaseException as exc:  # noqa: BLE001 - re-raised after the reactor stops
            errors.append(exc)
        finally:
            from twisted.internet import reactor

            if reactor.running:
                reactor.stop()
            else:
                reactor.callWhenRunning(reactor.stop)

    run_in_sequence()
    process.start(stop_after_crawl=False)  # blocks until run_in_sequence stops the reactor
    if errors:
        raise errors[0]
