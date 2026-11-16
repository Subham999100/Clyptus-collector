"""Spider unit tests. Offline: synthetic Scrapy responses built from the existing fixtures; no
reactor, no crawl, no sockets (``no_network`` blocks any attempt)."""

import logging
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import scrapy
from packaging.requirements import Requirement
from packaging.version import Version
from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse, Request, Response, TextResponse
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.internet.error import DNSLookupError, TimeoutError
from twisted.python.failure import Failure

from collector.adapters.common import Extraction
from collector.adapters.greenhouse import extract_greenhouse_jobs
from collector.adapters.json_ld import extract_json_ld_jobs
from collector.settings import Settings
from collector.spiders import platforms
from collector.spiders.platforms import (
    SPIDERS,
    AshbySpider,
    GreenhouseSpider,
    HtmlSpider,
    JsonLdSpider,
    LeverSpider,
)
from collector.spiders.run import (
    USER_AGENT,
    SourceOnlyMiddleware,
    UnsafeDestination,
    scrapy_settings,
)
from collector.spiders.sources import PLATFORMS, SOURCE_PLATFORMS, Source

FIXTURES = Path(__file__).parent / "fixtures"
COLLECTED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
PAGE_URL = "https://jobs.example.org/careers/engineer"

SOURCES = {
    "greenhouse": Source("greenhouse", "Example Corp", "example-board"),
    "lever": Source("lever", "Example Corp", "example-site"),
    "ashby": Source("ashby", "Example Corp", "example-ashby"),
    "jsonld": Source("jsonld", "Example Corp", PAGE_URL),
}
LISTING_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/example-board/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/example-site?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/example-ashby",
    "jsonld": PAGE_URL,
}
GOOD_FIXTURES = {  # platform -> (fixture, expected job count)
    "greenhouse": ("greenhouse/list_complete.json", 1),
    "lever": ("lever/postings_complete.json", 1),
    "ashby": ("ashby/board_complete.json", 1),
    "jsonld": ("jsonld/multiple_job_postings.html", 2),
}
CHALLENGE_PAGE = (
    "<html><head><title>Just a moment...</title></head>"
    '<body><div id="cf-chl-widget"></div>SECRET-PAYLOAD</body></html>'
)


@pytest.fixture(autouse=True)
def _offline(no_network: None) -> None:
    """Every test in this module runs with sockets blocked."""


def make_spider(platform: str) -> Any:
    return SPIDERS[platform](SOURCES[platform], collected_at=COLLECTED_AT)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def respond(
    spider: Any,
    body: str | bytes,
    *,
    status: int = 200,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
) -> TextResponse:
    url = url or spider.listing_url()
    if content_type is None:
        content_type = (
            "text/html; charset=utf-8" if spider.platform == "jsonld" else "application/json"
        )
    data = body.encode("utf-8") if isinstance(body, str) else body
    cls = HtmlResponse if "html" in content_type else TextResponse
    return cls(
        url=url,
        status=status,
        headers={"Content-Type": content_type, **(headers or {})},
        body=data,
        request=Request(url),
        encoding="utf-8",
    )


def http_error(spider: Any, status: int, **kwargs: Any) -> Failure:
    return Failure(HttpError(respond(spider, "", status=status, **kwargs)))


# --- request generation and platform routing --------------------------------------------------


def test_spiders_cover_exactly_the_supported_platforms() -> None:
    # Phase 8 adds "html" (generic HTML, hand-configured only; see tests/test_html_sources.py).
    assert set(SPIDERS) == set(SOURCE_PLATFORMS) == {*PLATFORMS, "html"}
    assert SPIDERS["greenhouse"] is GreenhouseSpider
    assert SPIDERS["lever"] is LeverSpider
    assert SPIDERS["ashby"] is AshbySpider
    assert SPIDERS["jsonld"] is JsonLdSpider
    assert SPIDERS["html"] is HtmlSpider
    for platform, spider_class in SPIDERS.items():
        assert spider_class.platform == platform


@pytest.mark.parametrize("platform", PLATFORMS)
def test_each_source_produces_exactly_its_one_listing_request(platform: str) -> None:
    spider = make_spider(platform)

    requests = list(spider.start_requests())

    assert [request.url for request in requests] == [LISTING_URLS[platform]]
    assert requests[0].method == "GET"
    assert requests[0].callback == spider.parse
    assert requests[0].errback == spider.on_error
    assert spider.allowed_domains == [requests[0].url.split("/")[2]]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_spider_rejects_a_source_of_another_platform(platform: str) -> None:
    other = next(p for p in PLATFORMS if p != platform)
    with pytest.raises(ValueError, match="is not"):
        SPIDERS[platform](SOURCES[other])


def test_spider_requires_an_aware_collection_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        GreenhouseSpider(SOURCES["greenhouse"], collected_at=datetime(2026, 1, 1))


@pytest.mark.parametrize("platform", PLATFORMS)
def test_responses_are_routed_to_the_matching_adapter_only(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    sentinel = Extraction((), (), "listing", 0)
    for name in ("greenhouse", "lever", "ashby", "json_ld"):
        adapter = f"extract_{name}_jobs"
        monkeypatch.setattr(
            platforms, adapter, lambda *a, _n=name, **k: calls.append(_n) or sentinel
        )
    spider = make_spider(platform)

    spider.parse(respond(spider, "[]" if platform == "lever" else '{"jobs": []}'))

    assert calls == ["json_ld" if platform == "jsonld" else platform]
    assert spider.extractions == [sentinel]


# --- Scrapy version compatibility --------------------------------------------------------------


def _collect(async_iterator: Any) -> list[Any]:
    """Drain an async generator that never really awaits (no event loop, so no sockets)."""
    items: list[Any] = []
    while True:
        try:
            async_iterator.__anext__().send(None)
        except StopIteration as yielded:  # the generator yielded one item
            items.append(yielded.value)
        except StopAsyncIteration:
            return items


@pytest.mark.parametrize("platform", PLATFORMS)
def test_start_and_start_requests_yield_the_same_single_request(platform: str) -> None:
    """Scrapy < 2.13 reads start_requests(), Scrapy >= 2.13 reads start() instead of it; each
    must produce the one request, so no version sends zero or two initial requests."""
    spider = make_spider(platform)

    legacy = [(r.url, r.callback, r.errback) for r in spider.start_requests()]
    modern = [(r.url, r.callback, r.errback) for r in _collect(spider.start())]

    assert len(legacy) == len(modern) == 1
    assert legacy == modern


def test_installed_scrapy_satisfies_the_declared_requirement() -> None:
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))
    requirement = next(
        Requirement(text)
        for text in pyproject["project"]["dependencies"]
        if Requirement(text).name == "scrapy"
    )
    assert requirement.specifier.contains(scrapy.__version__)


def test_declared_scrapy_floor_filters_offsite_redirects() -> None:
    """Scrapy 2.11.0/2.11.1 filter offsite requests in a spider middleware that never sees
    redirects; 2.11.2 moved it to a downloader middleware. The floor must not go below it."""
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))
    requirement = next(
        Requirement(text)
        for text in pyproject["project"]["dependencies"]
        if Requirement(text).name == "scrapy"
    )
    floors = [Version(s.version) for s in requirement.specifier if s.operator == ">="]
    assert floors
    assert max(floors) >= Version("2.11.2")


# --- successful extraction ---------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_fixture_response_is_extracted_by_the_platform_adapter(platform: str) -> None:
    name, count = GOOD_FIXTURES[platform]
    spider = make_spider(platform)

    spider.parse(respond(spider, fixture_text(name)))

    (extraction,) = spider.extractions
    assert len(extraction.records) == count
    assert extraction.failures == ()
    assert all(record.platform == platform for record in extraction.records)
    assert spider.fetch_failures == []


def test_spider_output_equals_the_adapter_output_untouched() -> None:
    spider = make_spider("greenhouse")
    text = fixture_text("greenhouse/list_complete.json")

    spider.parse(respond(spider, text))

    import json

    expected = extract_greenhouse_jobs(
        json.loads(text),
        company_name="Example Corp",
        board_token="example-board",
        source_url=LISTING_URLS["greenhouse"],
        collected_at=COLLECTED_AT,
    )
    assert spider.extractions == [expected]

    jsonld = make_spider("jsonld")
    page = fixture_text("jsonld/single_job_posting.html")
    jsonld.parse(respond(jsonld, page))
    assert jsonld.extractions == [
        extract_json_ld_jobs(
            page, page_url=PAGE_URL, collected_at=COLLECTED_AT, company_name="Example Corp"
        )
    ]


# --- empty and malformed responses -------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "fixture"),
    [
        ("greenhouse", "greenhouse/list_empty.json"),
        ("lever", "lever/postings_empty.json"),
        ("ashby", "ashby/board_empty.json"),
        ("jsonld", "generic/listing_empty.html"),
        ("jsonld", "generic/listing_js_rendered_shell.html"),
    ],
)
def test_a_genuinely_empty_listing_is_a_successful_empty_extraction(
    platform: str, fixture: str
) -> None:
    spider = make_spider(platform)

    spider.parse(respond(spider, fixture_text(fixture)))

    (extraction,) = spider.extractions
    assert extraction.records == ()
    assert extraction.failures == ()
    assert spider.fetch_failures == []  # empty is not "blocked"


@pytest.mark.parametrize("platform", ["greenhouse", "lever", "ashby"])
@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "{not json",
        "<html><body>Welcome</body></html>",
        "null",
        '"text"',
        "42",
        b"\xff\xfe",
    ],
)
def test_unusable_json_bodies_become_invalid_response_failures(platform: str, body: Any) -> None:
    spider = make_spider(platform)

    spider.parse(respond(spider, body))  # must not raise

    (extraction,) = spider.extractions
    assert extraction.records == ()
    assert [f.kind.value for f in extraction.failures] == ["invalid_response"]
    assert spider.fetch_failures == []


def test_a_non_text_response_is_an_invalid_response_failure() -> None:
    for platform in PLATFORMS:
        spider = make_spider(platform)
        url = spider.listing_url()
        response = Response(url=url, body=b"\x00\x01", headers={"Content-Type": "image/png"})
        spider.parse(response)
        (extraction,) = spider.extractions
        assert [f.kind.value for f in extraction.failures] == ["invalid_response"]


def test_extraction_failures_and_warnings_are_kept_per_source() -> None:
    spider = make_spider("greenhouse")
    spider.parse(respond(spider, fixture_text("greenhouse/list_invalid_incomplete.json")))
    (extraction,) = spider.extractions
    assert extraction.failures  # reported, not dropped


# --- failure isolation -------------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_an_adapter_exception_is_reported_not_raised_and_leaks_nothing(
    platform: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    name = "json_ld" if platform == "jsonld" else platform

    def explode(*args: Any, **kwargs: Any) -> None:
        raise ValueError("SECRET-PAYLOAD")

    monkeypatch.setattr(platforms, f"extract_{name}_jobs", explode)
    spider = make_spider(platform)
    caplog.set_level(logging.DEBUG)

    spider.parse(respond(spider, '{"jobs": []}' if platform != "jsonld" else "<html></html>"))

    (extraction,) = spider.extractions
    assert [f.kind.value for f in extraction.failures] == ["invalid_response"]
    assert "ValueError" in repr(extraction.failures)
    assert "SECRET-PAYLOAD" not in repr(extraction)
    assert "SECRET-PAYLOAD" not in caplog.text


def test_one_failing_spider_does_not_affect_another() -> None:
    bad, good = make_spider("greenhouse"), make_spider("lever")

    bad.parse(respond(bad, "{broken"))
    good.parse(respond(good, fixture_text("lever/postings_complete.json")))

    assert not bad.extractions[0].records
    assert len(good.extractions[0].records) == 1
    assert good.fetch_failures == [] and bad.fetch_failures == []


# --- access-challenge detection ----------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("status", [403, 429])
def test_http_403_and_429_are_recorded_as_blocked(platform: str, status: int) -> None:
    spider = make_spider(platform)

    spider.on_error(http_error(spider, status))

    (failure,) = spider.fetch_failures
    assert failure.blocked is True
    assert failure.reason == f"HTTP {status}"
    assert spider.extractions == []  # never reported as an (empty) listing


@pytest.mark.parametrize("status", [404, 500, 503])
def test_other_http_errors_are_failures_but_not_blocks(status: int) -> None:
    spider = make_spider("lever")

    spider.on_error(http_error(spider, status))

    (failure,) = spider.fetch_failures
    assert failure.blocked is False
    assert failure.reason == f"HTTP {status}"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_challenge_page_with_status_200_is_blocked_not_an_empty_listing(platform: str) -> None:
    spider = make_spider(platform)

    spider.parse(respond(spider, CHALLENGE_PAGE, content_type="text/html"))

    assert spider.extractions == []
    (failure,) = spider.fetch_failures
    assert failure.blocked is True
    assert failure.reason == "access challenge page"


@pytest.mark.parametrize(
    "marker",
    [
        "<title>Attention Required! | Cloudflare</title>",
        '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>',
        '<div class="g-recaptcha" data-sitekey="x"></div>',
        '<div class="h-captcha"></div>',
        '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>',
        '<div id="px-captcha"></div>',
        "<p>Please verify you are human to continue</p>",
    ],
)
def test_known_challenge_markers_are_detected(marker: str) -> None:
    spider = make_spider("jsonld")
    spider.parse(respond(spider, f"<html><body>{marker}</body></html>"))
    assert spider.extractions == []
    assert spider.fetch_failures[0].blocked is True


@pytest.mark.parametrize("platform", PLATFORMS)
def test_cloudflare_challenge_header_blocks_even_a_parseable_body(platform: str) -> None:
    name, _ = GOOD_FIXTURES[platform]
    spider = make_spider(platform)

    spider.parse(respond(spider, fixture_text(name), headers={"cf-mitigated": "challenge"}))

    assert spider.extractions == []
    assert spider.fetch_failures[0].blocked is True


def test_a_page_with_real_postings_and_a_captcha_widget_is_not_blocked() -> None:
    spider = make_spider("jsonld")
    page = fixture_text("jsonld/single_job_posting.html") + '<div class="g-recaptcha"></div>'

    spider.parse(respond(spider, page))

    assert len(spider.extractions[0].records) == 1
    assert spider.fetch_failures == []


def test_a_plain_page_without_postings_or_markers_is_not_blocked() -> None:
    spider = make_spider("jsonld")
    spider.parse(respond(spider, "<html><body><h1>No openings right now</h1></body></html>"))
    assert spider.fetch_failures == []
    assert spider.extractions[0].records == ()


# --- redirects and transport failures ----------------------------------------------------------


@pytest.mark.parametrize("platform", ["lever", "jsonld"])
def test_a_response_from_a_non_https_url_is_not_extracted(platform: str) -> None:
    spider = make_spider(platform)
    insecure = spider.listing_url().replace("https://", "http://")
    name, _ = GOOD_FIXTURES[platform]

    spider.parse(respond(spider, fixture_text(name), url=insecure))

    assert spider.extractions == []
    (failure,) = spider.fetch_failures
    assert failure.blocked is False
    assert "non-HTTPS" in failure.reason


@pytest.mark.parametrize(
    "error", [TimeoutError(), DNSLookupError("nope"), IgnoreRequest("Forbidden by robots.txt")]
)
def test_transport_failures_are_recorded_by_type_only(error: Exception) -> None:
    spider = make_spider("ashby")
    failure = Failure(error)
    failure.request = Request(spider.listing_url() + "?token=SECRET-QUERY")  # type: ignore[attr-defined]

    spider.on_error(failure)

    (recorded,) = spider.fetch_failures
    assert recorded.reason == type(error).__name__
    assert recorded.blocked is False
    assert "SECRET" not in repr(recorded)  # neither the message nor the query string
    assert recorded.url == LISTING_URLS["ashby"]


def test_failure_report_has_no_response_content_or_query_string() -> None:
    spider = make_spider("jsonld")
    secret_url = PAGE_URL + "?access_token=SECRET-QUERY"

    spider.on_error(
        Failure(HttpError(respond(spider, "SECRET-PAYLOAD", status=403, url=secret_url)))
    )
    spider.parse(respond(spider, CHALLENGE_PAGE, url=secret_url))

    text = repr(spider.fetch_failures)
    assert "SECRET" not in text
    assert [f.url for f in spider.fetch_failures] == [PAGE_URL, PAGE_URL]


def test_processing_responses_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    spider = make_spider("greenhouse")

    spider.parse(respond(spider, "SECRET-PAYLOAD {not json"))
    spider.parse(respond(spider, CHALLENGE_PAGE, content_type="text/html"))
    spider.on_error(http_error(spider, 429))

    assert "SECRET" not in caplog.text
    assert [r for r in caplog.records if r.name.startswith("collector")] == []


# --- crawling-safety settings ------------------------------------------------------------------


@pytest.fixture
def default_scrapy_settings(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import os

    for name in list(os.environ):
        if name.startswith("COLLECTOR_"):
            monkeypatch.delenv(name)
    return scrapy_settings(Settings())


def test_robots_txt_is_obeyed_with_a_descriptive_user_agent(
    default_scrapy_settings: dict[str, Any],
) -> None:
    assert default_scrapy_settings["ROBOTSTXT_OBEY"] is True
    user_agent = default_scrapy_settings["USER_AGENT"]
    assert user_agent == USER_AGENT
    assert "clyptus-collector" in user_agent and "robots.txt" in user_agent


def test_concurrency_and_delays_are_conservative(default_scrapy_settings: dict[str, Any]) -> None:
    config = default_scrapy_settings
    assert config["CONCURRENT_REQUESTS_PER_DOMAIN"] == 1
    assert config["CONCURRENT_REQUESTS"] <= 4
    assert config["DOWNLOAD_DELAY"] >= 1.0
    assert config["AUTOTHROTTLE_ENABLED"] is True


def test_timeout_retries_redirects_and_size_are_bounded(
    default_scrapy_settings: dict[str, Any],
) -> None:
    config = default_scrapy_settings
    assert 0 < config["DOWNLOAD_TIMEOUT"] <= 30
    assert 0 < config["RETRY_TIMES"] <= 2
    assert 429 not in config["RETRY_HTTP_CODES"]  # a rate limit is respected, not hammered
    assert 403 not in config["RETRY_HTTP_CODES"]
    assert {500, 503}.issubset(config["RETRY_HTTP_CODES"])
    assert 0 < config["REDIRECT_MAX_TIMES"] <= 3
    assert config["DEPTH_LIMIT"] == 1  # nothing is ever followed
    assert 0 < config["DOWNLOAD_MAXSIZE"] <= 32 * 1024 * 1024


def test_no_state_or_listening_services_are_enabled(
    default_scrapy_settings: dict[str, Any],
) -> None:
    config = default_scrapy_settings
    assert config["COOKIES_ENABLED"] is False
    assert config["TELNETCONSOLE_ENABLED"] is False
    assert config["REMOTE_CONTROL_ENABLED"] is False  # Scrapy 2.20+ opens a code-running server


def test_collector_environment_overrides_reach_scrapy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLLECTOR_ROBOTS_TXT_OBEY", "false")
    monkeypatch.setenv("COLLECTOR_DOWNLOAD_DELAY_SECONDS", "2.5")
    monkeypatch.setenv("COLLECTOR_REQUEST_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("COLLECTOR_RETRY_TIMES", "1")
    monkeypatch.setenv("COLLECTOR_CONCURRENT_REQUESTS_PER_DOMAIN", "2")
    monkeypatch.setenv("COLLECTOR_LOG_LEVEL", "WARNING")

    config = scrapy_settings()

    assert config["ROBOTSTXT_OBEY"] is False
    assert config["DOWNLOAD_DELAY"] == 2.5
    assert config["DOWNLOAD_TIMEOUT"] == 7
    assert config["RETRY_TIMES"] == 1
    assert config["CONCURRENT_REQUESTS_PER_DOMAIN"] == 2
    assert config["LOG_LEVEL"] == "WARNING"


# --- redirect destination safety (the middleware that runs before any network request) ----------


class _Crawler:
    def __init__(self, spider: Any) -> None:
        self.spider = spider


@pytest.mark.parametrize(
    ("platform", "url", "allowed"),
    [
        ("lever", "https://api.lever.co/v0/postings/other?mode=json", True),  # same host
        ("lever", "https://API.LEVER.CO/anything", True),  # host case is irrelevant
        ("lever", "https://api.lever.co:443/anything", True),  # explicit default port
        ("lever", "http://api.lever.co/v0/postings/example-site", False),  # downgrade
        ("lever", "https://sub.api.lever.co/x", False),  # subdomain of the source
        ("lever", "https://lever.co/x", False),  # parent domain
        ("lever", "https://api.lever.co.evil.example/x", False),  # look-alike
        ("lever", "https://evil.example.net/x", False),
        ("lever", "https://api.lever.co:8443/x", False),  # other port
        ("lever", "https://user:pw@api.lever.co/x", False),  # credentials
        ("lever", "https://user@api.lever.co/x", False),
        ("lever", "https://localhost/x", False),
        ("lever", "https://127.0.0.1/x", False),
        ("lever", "https://[::1]/x", False),
        ("lever", "https://10.1.2.3/x", False),
        ("lever", "https://192.168.0.1/x", False),
        ("lever", "https://169.254.169.254/latest", False),
        ("lever", "ftp://api.lever.co/x", False),
        ("lever", "file:///etc/passwd", False),
        ("lever", "https://api.lever.co:notaport/x", False),  # malformed
        ("jsonld", PAGE_URL, True),
        ("jsonld", "https://jobs.example.org/other/page", True),
        ("jsonld", "https://careers.example.org/x", False),
        ("jsonld", "http://jobs.example.org/careers/engineer", False),
    ],
)
def test_only_https_to_the_configured_host_and_port_may_be_requested(
    platform: str, url: str, allowed: bool
) -> None:
    middleware = SourceOnlyMiddleware(_Crawler(make_spider(platform)))
    request = SimpleNamespace(url=url)  # only the URL is judged (Request rejects bad ports)

    if allowed:
        assert middleware.process_request(request) is None  # type: ignore[arg-type]
    else:
        with pytest.raises(UnsafeDestination) as refused:
            middleware.process_request(request)  # type: ignore[arg-type]
        for secret in ("pw", "user", "169.254", "etc/passwd"):
            assert secret not in str(refused.value)  # the message never repeats the URL


def test_the_middleware_is_registered_before_every_other_downloader_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = scrapy_settings(Settings())
    (path, priority) = next(iter(config["DOWNLOADER_MIDDLEWARES"].items()))  # type: ignore[attr-defined]
    assert path == "collector.spiders.run.SourceOnlyMiddleware"
    assert priority < 50  # Scrapy's own offsite (50), robots.txt (100) and redirect middlewares


def test_failure_urls_carry_no_credentials_query_or_fragment() -> None:
    spider = make_spider("lever")
    failure = Failure(IgnoreRequest("x"))
    failure.request = Request("https://user:pw@api.lever.co:8443/path?token=SECRET#frag")  # type: ignore[attr-defined]

    spider.on_error(failure)

    assert spider.fetch_failures[0].url == "https://api.lever.co:8443/path"
