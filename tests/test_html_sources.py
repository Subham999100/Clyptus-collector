"""Generic HTML sources (Phase 8): loading reviewed selector configurations, the HtmlSpider's
request flow (synthetic responses, no reactor, sockets blocked) and the discovery boundary."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from scrapy.http import HtmlResponse, Request
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.python.failure import Failure

from collector.adapters.html import MAX_PAGES, parse_html_config
from collector.companies import registry
from collector.companies.registry import Authorization, State
from collector.spiders.platforms import SPIDERS, HtmlSpider
from collector.spiders.run import SourceOnlyMiddleware, scrapy_settings
from collector.spiders.sources import (
    PLATFORMS,
    SOURCE_PLATFORMS,
    Source,
    check_source,
    load_sources,
)

FIXTURES = Path(__file__).parent / "fixtures" / "generic"
LISTING = "https://careers.example.com/careers/jobs"
COLLECTED_AT = datetime(2026, 11, 16, 3, 30, tzinfo=UTC)
HEADER = "platform,company_name,identifier,company_website,html_config\n"
SELECTORS: dict[str, Any] = {
    "listing": "ul.jobs",
    "job": "li.job",
    "title": "a.job-link",
    "url": "a.job-link",
    "location": ".job-location",
    "next_page": "a.next",
    "max_pages": 2,
    "path_prefix": "/careers/",
}
REVIEW = {
    "verified_by": "A. Reviewer",
    "ownership_evidence": "careers page linked from the official homepage",
    "authorization": "APPROVED",
    "authorized_by": "B. Owner",
    "note": "written permission on file",
}


@pytest.fixture(autouse=True)
def _offline(no_network: None) -> None:
    """Every test in this module runs with sockets blocked."""


def write_config(folder: Path, name: str = "acme.json", **changes: Any) -> Path:
    data = {**SELECTORS, "review": dict(REVIEW)}
    data.update(changes)
    data = {key: value for key, value in data.items() if value is not None}
    path = folder / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_sources(folder: Path, text: str) -> Path:
    path = folder / "sources.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return path


# --- loading sources ----------------------------------------------------------------------------


def test_an_html_source_loads_with_its_reviewed_configuration(tmp_path: Path) -> None:
    write_config(tmp_path)
    path = write_sources(
        tmp_path,
        HEADER
        + "greenhouse,Example Corp,example-board,,\n"
        + f"html,Acme Ltd,{LISTING},https://www.example.com,acme.json\n",
    )
    greenhouse, html = load_sources(path)
    assert greenhouse == Source("greenhouse", "Example Corp", "example-board")
    assert (html.platform, html.company_name, html.identifier) == ("html", "Acme Ltd", LISTING)
    assert html.company_website == "https://www.example.com/"
    assert html.html == parse_html_config(SELECTORS)  # the review block is checked, not kept


def test_existing_four_column_source_files_still_load(tmp_path: Path) -> None:
    path = write_sources(
        tmp_path,
        "platform,company_name,identifier,company_website\n"
        "lever,Example,example-site,\njsonld,Example,https://jobs.example.org/x,\n",
    )
    assert [s.platform for s in load_sources(path)] == ["lever", "jsonld"]
    assert all(source.html is None for source in load_sources(path))


@pytest.mark.parametrize(
    ("config", "row", "message"),
    [
        ({}, f"html,Acme,{LISTING},,", "need a reviewed selector configuration"),
        ({}, f"html,Acme,{LISTING},,missing.json", "html_config cannot be read"),
        ({"review": None}, f"html,Acme,{LISTING},,acme.json", "needs a review object"),
        (
            {"review": {**REVIEW, "authorization": "PENDING"}},
            f"html,Acme,{LISTING},,acme.json",
            "collection is not approved",
        ),
        (
            {"review": {**REVIEW, "authorization": "RESTRICTED"}},
            f"html,Acme,{LISTING},,acme.json",
            "collection is not approved",
        ),
        (
            {"review": {**REVIEW, "verified_by": " "}},
            f"html,Acme,{LISTING},,acme.json",
            "review.verified_by is required",
        ),
        (
            {"review": {**REVIEW, "approved": "yes"}},
            f"html,Acme,{LISTING},,acme.json",
            "unknown review key",
        ),
        ({"job": "li >"}, f"html,Acme,{LISTING},,acme.json", "job is not a valid CSS selector"),
        ({"title": None}, f"html,Acme,{LISTING},,acme.json", "title selector is required"),
        ({}, "html,Acme,http://careers.example.com/careers/jobs,,acme.json", "must use https"),
        ({}, "html,Acme,https://10.0.0.1/careers,,acme.json", "html page URL"),
        ({}, "greenhouse,Acme,example-board,,acme.json", "only used by html sources"),
        (
            {},
            "html,Acme,https://boards.greenhouse.io/acme,,acme.json",
            "use the greenhouse platform",
        ),
        (
            {},
            "html,Acme,https://acme.keka.com/careers,,acme.json",
            "keka, which the collector does not support",
        ),
        (
            {},
            "html,Acme,https://acme.zohorecruit.in/jobs/Careers,,acme.json",
            "not a way around its access requirements",
        ),
        (
            {},
            "html,Acme,https://acme.darwinbox.in/ms/candidate/careers,,acme.json",
            "darwinbox",
        ),
    ],
)
def test_html_sources_are_refused_unless_reviewed_approved_and_not_an_ats(
    tmp_path: Path, config: dict[str, Any], row: str, message: str
) -> None:
    write_config(tmp_path, **config)
    path = write_sources(tmp_path, HEADER + row + "\n")
    with pytest.raises(ValueError, match=message) as refused:
        load_sources(path)
    assert "line 2" in str(refused.value)


def test_malformed_configuration_files_are_refused(tmp_path: Path) -> None:
    (tmp_path / "acme.json").write_text("{broken", encoding="utf-8")
    path = write_sources(tmp_path, HEADER + f"html,Acme,{LISTING},,acme.json\n")
    with pytest.raises(ValueError, match="html_config: not valid JSON"):
        load_sources(path)
    (tmp_path / "acme.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_sources(path)


def test_the_same_listing_page_cannot_be_configured_twice(tmp_path: Path) -> None:
    write_config(tmp_path)
    row = f"html,Acme,{LISTING},,acme.json\n"
    path = write_sources(tmp_path, HEADER + row + row.replace("Acme", "Acme Two"))
    with pytest.raises(ValueError, match="duplicate source"):
        load_sources(path)


def test_platform_lists() -> None:
    assert PLATFORMS == ("greenhouse", "lever", "ashby", "jsonld")  # unchanged
    assert SOURCE_PLATFORMS == (*PLATFORMS, "html")
    assert set(SPIDERS) == set(SOURCE_PLATFORMS) and SPIDERS["html"] is HtmlSpider
    with pytest.raises(ValueError, match="platform must be one of"):
        check_source("custom", "Acme", LISTING)


# --- discovery never creates a runnable html source --------------------------------------------


def discovery_record(platform: str, identifier: str | None) -> dict[str, Any]:
    return {
        "company_name": "Acme Ltd",
        "input": {},
        "status": "VERIFIED",
        "official_website": "https://www.example.com/",
        "careers_url": LISTING,
        "platform": platform,
        "identifier": identifier,
        "evidence": [],
        "reason": None,
        "failed": False,
        "discovered_at": "2026-11-16T00:00:00+00:00",
        "checked_at": "2026-11-16T00:00:00+00:00",
        "last_verified_at": "2026-11-16T00:00:00+00:00",
        "recheck": None,
    }


@pytest.mark.parametrize(("platform", "identifier"), [("custom", None), ("html", LISTING)])
def test_discovery_records_never_become_html_crawler_sources(
    tmp_path: Path, platform: str, identifier: str | None
) -> None:
    state_path = tmp_path / "state.json"
    record = discovery_record(platform, identifier)
    registry.save_state(state_path, State({"acme ltd": record}, {}))
    with pytest.raises(ValueError, match="not a usable crawler source"):
        registry.authorize(state_path, "Acme Ltd", Authorization.APPROVED, by="x", note="y")
    approval = {"decision": "APPROVED", "platform": platform, "identifier": identifier}
    export = registry.approved_sources(State({"acme ltd": record}, {"acme ltd": approval}))
    assert export.sources == ()
    assert "not a usable crawler source" in export.review[0].reason


# --- the spider ---------------------------------------------------------------------------------


def make_spider(**changes: Any) -> HtmlSpider:
    config = parse_html_config({**SELECTORS, **changes})
    source = check_source("html", "Acme Ltd", LISTING, html=config)
    return HtmlSpider(source, collected_at=COLLECTED_AT)


def page(url: str, body: str, *, meta: dict[str, Any] | None = None, status: int = 200) -> Any:
    request = Request(url, meta=meta or {})
    return HtmlResponse(
        url=url, status=status, body=body.encode(), encoding="utf-8", request=request
    )


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_the_spider_starts_with_the_one_listing_page() -> None:
    spider = make_spider()
    (request,) = list(spider.start_requests())
    assert (request.url, request.meta) == (LISTING, {"html_page": 1})
    assert (request.callback, request.errback) == (spider.parse, spider.on_error)
    assert spider.allowed_domains == ["careers.example.com"]
    with pytest.raises(ValueError, match="selector configuration"):
        HtmlSpider(Source("html", "Acme", LISTING))
    assert HtmlSpider.custom_settings == {"DEPTH_LIMIT": MAX_PAGES}
    assert scrapy_settings()["DEPTH_LIMIT"] == 1  # the other spiders still follow nothing


def test_listing_pages_are_followed_once_and_within_the_page_limit() -> None:
    spider = make_spider()
    follow = list(
        spider.parse(page(LISTING, fixture("listing_page_1.html"), meta={"html_page": 1}))
    )
    assert [(r.url, r.meta) for r in follow] == [(f"{LISTING}?page=2", {"html_page": 2})]
    body = fixture("listing_page_1.html").replace("?page=2", "?page=3")
    second = page(follow[0].url, body, meta={"html_page": 2})
    assert list(spider.parse(second)) == []  # page limit (2) reached
    assert [len(e.records) for e in spider.extractions] == [2, 0]
    assert spider.extractions[1].skipped  # page 2 repeated page 1's jobs: skipped, not doubled
    assert "page limit (2) reached" in spider.extractions[1].warnings[0].message


def test_detail_pages_are_requested_once_and_complete_their_job() -> None:
    spider = make_spider(detail={"location": "dd.location", "posted_date": "dd.posted"})
    listing = page(LISTING, fixture("listing_unordered_list.html"), meta={"html_page": 1})
    requests = list(spider.parse(listing))
    assert [r.url for r in requests] == [f"{LISTING}/{n}" for n in (101, 102, 103)]
    assert all(SourceOnlyMiddleware(_Crawler(spider)).process_request(r) is None for r in requests)
    again = list(spider.parse(page(LISTING, fixture("listing_unordered_list.html"))))
    assert again == []  # the same jobs are never requested twice in a run

    first, second, third = requests
    assert (
        list(spider.parse(page(first.url, fixture("detail_page_101.html"), meta=first.meta))) == []
    )
    spider.on_error(Failure(HttpError(page(second.url, "", meta=second.meta, status=404))))
    spider.on_error(Failure(HttpError(page(third.url, "", meta=third.meta, status=500))))

    detail_results = [e for e in spider.extractions if e.response_type == "detail"]
    assert [(len(e.records), len(e.failures)) for e in detail_results] == [(1, 0), (0, 1)]
    assert detail_results[0].records[0].location == "Bengaluru, Karnataka, India"
    assert "HTTP 404" in detail_results[1].failures[0].errors[0].message
    (fetch_failure,) = spider.fetch_failures  # a server error is a fetch failure of the source
    assert fetch_failure.reason == "HTTP 500" and fetch_failure.url == third.url


def test_challenge_pages_are_blocked_on_listing_and_detail_pages() -> None:
    challenge = "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>"
    spider = make_spider()
    assert list(spider.parse(page(LISTING, challenge))) == []
    assert spider.extractions == [] and spider.fetch_failures[0].blocked

    spider = make_spider(detail={"location": "dd.location"})
    (job, *_) = spider.parse(page(LISTING, fixture("listing_unordered_list.html")))
    list(spider.parse(page(job.url, challenge, meta=job.meta)))
    assert spider.fetch_failures[0].blocked and spider.extractions[-1].response_type == "listing"


def test_a_job_list_with_a_captcha_form_is_not_blocked() -> None:
    spider = make_spider()
    body = fixture("listing_page_2.html").replace("</ul>", '</ul><div class="g-recaptcha"></div>')
    list(spider.parse(page(LISTING, body)))
    assert spider.fetch_failures == [] and len(spider.extractions[0].records) == 1


def test_a_javascript_shell_is_a_failure_not_an_empty_listing() -> None:
    spider = make_spider()
    list(spider.parse(page(LISTING, fixture("listing_js_rendered_shell.html"))))
    (extraction,) = spider.extractions
    assert extraction.response_type is None and "JavaScript" in repr(extraction.failures)


def test_non_https_responses_are_not_extracted() -> None:
    spider = make_spider()
    insecure = LISTING.replace("https://", "http://")
    assert list(spider.parse(page(insecure, fixture("listing_page_1.html")))) == []
    assert spider.extractions == [] and "non-HTTPS" in spider.fetch_failures[0].reason


class _Crawler:
    def __init__(self, spider: Any) -> None:
        self.spider = spider
