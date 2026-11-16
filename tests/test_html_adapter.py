"""Generic HTML adapter (Phase 8): selector configuration, listing cards, detail pages and
pagination, on synthetic fixtures (tests/fixtures/generic). Offline: nothing is fetched."""

import copy
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from collector.adapters.html import (
    MAX_PAGES,
    FieldSelectors,
    HtmlConfig,
    ListingPage,
    PendingJob,
    detail_unavailable,
    extract_html_detail,
    extract_html_listing,
    loads_html_config,
    parse_html_config,
)
from collector.pipelines.date_window import DateOutcome, check_date_window
from collector.pipelines.validate import validate_job
from collector.utils.identity import job_key

FIXTURES = Path(__file__).parent / "fixtures" / "generic"
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=IST)
LISTING = "https://careers.example.com/careers/jobs"

CONFIG: dict[str, Any] = {
    "listing": "ul.jobs",
    "job": "li.job",
    "title": "a.job-link",
    "url": "a.job-link",
    "location": ".job-location",
    "employment_type": ".job-type",
    "work_type": ".work-mode",
    "posted_date": ".posted",
    "description": ".summary",
    "no_jobs": "p.no-openings",
    "next_page": "a.next",
    "max_pages": 3,
    "path_prefix": "/careers/",
}
DETAIL = {
    "location": "dd.location",
    "posted_date": "dd.posted",
    "employment_type": "dd.type",
    "description": "div.description",
}


@pytest.fixture(autouse=True)
def _offline(no_network: None) -> None:
    """Every test in this module runs with sockets blocked."""


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def config(**changes: Any) -> HtmlConfig:
    data = copy.deepcopy(CONFIG)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return parse_html_config(data)


def listing(markup: str, cfg: HtmlConfig | None = None, **options: Any) -> ListingPage:
    arguments: dict[str, Any] = {
        "config": cfg or config(),
        "page_url": LISTING,
        "listing_url": LISTING,
        "company_name": "Example Corp",
        "collected_at": NOW,
    }
    arguments.update(options)
    return extract_html_listing(markup, **arguments)


def detail(markup: str, job: PendingJob, cfg: HtmlConfig) -> Any:
    return extract_html_detail(
        markup,
        job=job,
        config=cfg,
        page_url=job.url,
        listing_url=LISTING,
        company_name="Example Corp",
        collected_at=NOW,
    )


def settled(page: ListingPage) -> int:
    e = page.extraction
    return len(e.records) + len(e.failures) + len(e.skipped)


# --- configuration --------------------------------------------------------------------------


def test_minimal_configuration_defaults() -> None:
    cfg = parse_html_config({"listing": "ul", "job": "li", "title": "a", "url": "a"})
    assert cfg.fields == FieldSelectors()
    assert (cfg.next_page, cfg.max_pages, cfg.path_prefix, cfg.detail) == (None, 1, "/", None)
    assert parse_html_config({**CONFIG, "max_pages": None}).max_pages == 3  # default with next


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"title": None}, "title selector is required"),
        ({"url": "   "}, "url must be a non-blank"),
        ({"listing": 5}, "listing must be a non-blank"),
        ({"job": "li >"}, "job is not a valid CSS selector"),
        ({"location": "[["}, "location is not a valid CSS selector"),
        ({"title": "a::foo"}, "title is not a valid CSS selector"),
        ({"job": "li::attr(id)"}, "job must select elements"),
        ({"no_jobs": "p::text"}, "no_jobs must select elements"),
        ({"titel": "h2"}, "unknown key(s): titel"),
        ({"max_pages": 0}, "between 1 and 10"),
        ({"max_pages": MAX_PAGES + 1}, "between 1 and 10"),
        ({"max_pages": "3"}, "max_pages must be an integer"),
        ({"max_pages": True}, "max_pages must be an integer"),
        ({"next_page": None, "max_pages": 2}, "max_pages needs next_page"),
        ({"path_prefix": "careers"}, "path_prefix must be a string starting with '/'"),
        ({"path_prefix": "/careers/../admin"}, "plain path"),
        ({"path_prefix": "/careers?x=1"}, "plain path"),
        ({"detail": []}, "detail must be an object"),
        ({"detail": {}}, "detail needs at least one selector"),
        ({"detail": {"title": "h1"}}, "unknown detail key(s): title"),
        ({"detail": {"location": "dd >"}}, "detail.location is not a valid CSS selector"),
        ({"title": "a" * 301}, "too long"),
        ({"title": "a\x00"}, "control characters"),
    ],
)
def test_invalid_configuration_is_rejected_early(changes: dict[str, Any], message: str) -> None:
    data = {**CONFIG, **changes}
    data = {key: value for key, value in data.items() if value is not None}
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_html_config(data)


def test_configuration_must_be_a_json_object() -> None:
    for bad in ([], "x", None):
        with pytest.raises(ValueError, match="JSON object"):
            parse_html_config(bad)
    with pytest.raises(ValueError, match="not valid JSON"):
        loads_html_config("{not json")
    assert loads_html_config('{"listing": "ul", "job": "li", "title": "a", "url": "a"}').job == "li"


@pytest.mark.parametrize(
    "options",
    [
        {"company_name": " "},
        {"collected_at": datetime(2026, 11, 16)},
        {"listing_url": "http://careers.example.com/careers/jobs"},
        {"listing_url": "https://localhost/careers"},
        {"company_website": "not a url"},
    ],
)
def test_bad_trusted_configuration_raises(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        listing("<html></html>", **options)


# --- one listing page -----------------------------------------------------------------------


def test_single_job_maps_every_configured_field() -> None:
    page = listing(html("listing_single_job.html"), config(external_id="::attr(data-job-id)"))
    extraction = page.extraction
    assert (extraction.response_type, extraction.job_count) == ("listing", 1)
    assert extraction.failures == extraction.warnings == extraction.skipped == ()
    (record,) = extraction.records
    assert record.title == "Site Reliability Engineer"
    assert record.company_name == "Example Corp"  # trusted configuration, never page text
    assert str(record.apply_url) == "https://careers.example.com/careers/jobs/100"
    assert str(record.source_url) == LISTING
    assert record.platform == "html"
    assert record.location == "Bengaluru, India"
    assert record.employment_type == "Full-time"
    assert record.work_type == "hybrid"
    assert (record.posted_date, record.posted_date_source) == (date(2026, 11, 10), "page_text")
    assert record.external_id == "J-100"
    assert record.description_snippet == "Keep our platform reliable."  # the script is dropped
    assert record.salary_text is None  # never configured, never invented
    assert validate_job(record.model_dump()) == record
    assert page.pending == () and page.next_page is None
    assert page.job_urls == ("https://careers.example.com/careers/jobs/100",)


def test_attribute_selectors_read_attributes() -> None:
    cfg = config(posted_date="time.posted::attr(datetime)", url="a.job-link::attr(href)")
    (record,) = listing(html("listing_single_job.html"), cfg).extraction.records
    assert record.posted_date == date(2026, 11, 10)


def test_multiple_cards_in_another_layout() -> None:
    cfg = parse_html_config(
        {
            "listing": "#openings",
            "job": "div.job-card",
            "title": "h3",
            "url": "a",
            "external_id": "::attr(data-job-id)",
            "location": "p.meta",
            "path_prefix": "/apply/",
        }
    )
    page = listing(html("listing_cards.html"), cfg)
    assert [(r.title, r.external_id, str(r.apply_url)) for r in page.extraction.records] == [
        ("Product Designer", "a-201", "https://careers.example.com/apply/a-201"),
        ("QA Engineer", "a-202", "https://careers.example.com/apply/a-202"),
    ]
    assert page.extraction.records[0].location == "Pune · Full-time"  # the text as published


def test_relative_links_resolve_against_the_page_url() -> None:
    page = listing(html("listing_unordered_list.html"), config())
    assert [str(r.apply_url) for r in page.extraction.records] == [
        f"https://careers.example.com/careers/jobs/{n}" for n in (101, 102, 103)
    ]
    assert [r.location for r in page.extraction.records] == ["Bengaluru", "Pune", "Remote"]


def test_missing_optional_fields_stay_unknown_without_warnings() -> None:
    extraction = listing(html("listing_missing_optional_fields.html")).extraction
    assert extraction.warnings == () and len(extraction.records) == 2
    for record in extraction.records:
        assert record.location is record.employment_type is record.work_type is None
        assert record.description_snippet is record.posted_date is None
        assert record.posted_date_source == "unknown"


def test_missing_required_fields_fail_only_their_own_job() -> None:
    extraction = listing(html("listing_missing_required_fields.html")).extraction
    assert [(f.index, f.kind.value, [e.field for e in f.errors]) for f in extraction.failures] == [
        (0, "missing_field", ["title"]),
        (1, "missing_field", ["title", "url"]),  # a span: neither title link nor job link
        (2, "missing_field", ["url"]),  # a link without href
    ]
    assert [r.title for r in extraction.records] == ["Editor"]
    assert extraction.job_count == 4


def test_malformed_html_is_parsed_leniently() -> None:
    records = listing(html("listing_malformed.html")).extraction.records
    assert [(r.title, r.location) for r in records] == [
        ("Platform Engineer", "Chennai"),
        ("Security Analyst", "Delhi"),
    ]


def test_duplicate_job_links_are_skipped_not_collected_twice() -> None:
    page = listing(html("listing_duplicate_links.html"))
    extraction = page.extraction
    assert [r.title for r in extraction.records] == ["Data Engineer", "ML Engineer"]
    assert [(s.index, s.reason) for s in extraction.skipped] == [
        (1, "duplicate job link: already found"),
        (2, "duplicate job link: already found"),
    ]
    assert settled(page) == extraction.job_count == 4
    # A job already found on an earlier page of this run is skipped as well.
    later = listing(html("listing_duplicate_links.html"), seen_jobs=set(page.job_urls))
    assert later.extraction.records == () and len(later.extraction.skipped) == 4


@pytest.mark.parametrize(
    ("index", "message"),
    [
        (0, "job link needs JavaScript or is an in-page anchor (not supported)"),
        (1, "job link is not a public URL (not an absolute http(s) URL)"),
        (2, "job link is not an https URL"),
        (3, "job link is not a public URL (host is not public)"),
        (4, "job link is outside the configured path_prefix"),
    ],
)
def test_unusable_job_links_are_failures_that_never_echo_the_link(index: int, message: str) -> None:
    extraction = listing(html("listing_invalid_urls.html")).extraction
    failure = next(f for f in extraction.failures if f.index == index)
    assert (failure.kind.value, failure.errors[0].message) == ("invalid_url", message)
    assert [r.title for r in extraction.records] == ["Accountant"]


def test_links_to_other_hosts_are_never_accepted() -> None:
    page = listing(html("listing_cross_domain.html"))
    messages = [f.errors[0].message for f in page.extraction.failures]
    assert messages == ["job link points to another host than the source"] * 3
    assert [r.title for r in page.extraction.records] == ["Account Manager"]
    assert page.next_page is None
    (warning,) = page.extraction.warnings
    assert warning.field == "next_page" and "another host" in warning.message
    assert "other.example.org" not in repr(page.extraction)


def test_unknown_posting_dates_stay_unknown() -> None:
    extraction = listing(html("listing_unknown_date.html")).extraction
    assert {(r.posted_date, r.posted_date_source) for r in extraction.records} == {
        (None, "unknown")
    }
    assert extraction.warnings == ()


def test_unreadable_posting_dates_are_warnings_and_never_replaced() -> None:
    extraction = listing(html("listing_invalid_date.html")).extraction
    dates = {r.title: r.posted_date for r in extraction.records}
    assert dates == {
        "Legal Counsel": None,  # "Posted recently": vague
        "Paralegal": None,  # 31/02/2026 does not exist
        "Auditor": date(2026, 11, 2),
        "Controller": date(2026, 11, 13),  # "3 days ago" from 16 Nov (collection timezone)
    }
    assert [(w.index, w.field, w.kind.value) for w in extraction.warnings] == [
        (0, "posted_date", "invalid_date"),
        (1, "posted_date", "invalid_date"),
    ]
    assert all(r.posted_date != NOW.date() for r in extraction.records)  # never the crawl date


def test_dates_work_with_the_existing_date_window() -> None:
    markup = (
        html("listing_invalid_date.html")
        .replace("2026-11-02", "x")
        .replace("2 November 2026", "2 September 2026")
    )
    records = {r.title: r for r in listing(markup).extraction.records}
    outcome = {
        title: check_date_window(record, NOW, 30, IST).outcome for title, record in records.items()
    }
    assert outcome == {
        "Legal Counsel": DateOutcome.UNKNOWN_DATE,
        "Paralegal": DateOutcome.UNKNOWN_DATE,
        "Auditor": DateOutcome.OLD_DATE,
        "Controller": DateOutcome.ELIGIBLE,
    }


def test_work_type_and_lengths_are_only_taken_when_clear() -> None:
    markup = (
        '<ul class="jobs"><li class="job"><a class="job-link" href="/careers/jobs/1">A</a>'
        '<span class="work-mode">Flexible</span><span class="job-type">' + "x" * 101 + "</span>"
        "</li></ul>"
    )
    extraction = listing(markup).extraction
    (record,) = extraction.records
    assert record.work_type is None and record.employment_type is None
    assert [w.field for w in extraction.warnings] == ["employment_type", "work_type"]
    long_title = markup.replace(">A<", ">" + "T" * 301 + "<")
    assert listing(long_title).extraction.failures[0].errors[0].field == "title"


def test_long_html_descriptions_become_safe_snippets() -> None:
    body = "<p>" + "word " * 400 + "</p><script>alert('x')</script><style>p{color:red}</style>"
    markup = (
        '<ul class="jobs"><li class="job"><a class="job-link" href="/careers/jobs/1">A</a>'
        f'<div class="summary">{body}</div></li></ul>'
    )
    (record,) = listing(markup).extraction.records
    snippet = record.description_snippet or ""
    assert 0 < len(snippet) <= 500 and "alert" not in snippet and "color" not in snippet


def cards(*numbers: int, attribute: bool = False) -> str:
    items = "".join(
        f'<li class="job"{f" data-job-id=J-{n}" if attribute else ""}>'
        f'<a class="job-link" href="/careers/jobs/{n}">Job {n}</a></li>'
        for n in numbers
    )
    return f'<ul class="jobs">{items}</ul>'


def test_a_configured_external_id_is_required_on_every_card() -> None:
    markup = cards(1, attribute=True).replace("</ul>", "") + cards(2)[len('<ul class="jobs">') :]
    extraction = listing(markup, config(external_id="::attr(data-job-id)")).extraction
    assert [r.external_id for r in extraction.records] == ["J-1"]
    assert [(f.index, f.errors[0].field) for f in extraction.failures] == [(1, "external_id")]


def test_identity_is_the_job_url_or_id_never_the_card_position() -> None:
    def keys(markup: str, cfg: HtmlConfig | None = None, **options: Any) -> dict[str, str]:
        records = listing(markup, cfg, **options).extraction.records
        return {r.title: job_key(r) for r in records}

    forward = keys(cards(1, 2, 3))
    assert keys(cards(3, 1, 2)) == forward  # same jobs, other order: same identities
    assert keys(cards(1, 2, 3), collected_at=NOW + timedelta(days=1)) == forward  # not the time
    assert len(set(forward.values())) == 3
    by_id = config(external_id="::attr(data-job-id)")
    assert keys(cards(2, 1, attribute=True), by_id) == keys(cards(1, 2, attribute=True), by_id)


def test_output_is_deterministic() -> None:
    markup = html("listing_invalid_date.html")
    assert listing(markup) == listing(markup)


# --- unusable and empty pages -----------------------------------------------------------------


def test_a_javascript_only_page_is_unusable_not_empty() -> None:
    extraction = listing(html("listing_js_rendered_shell.html")).extraction
    assert extraction.response_type is None
    (failure,) = extraction.failures
    assert failure.kind.value == "invalid_response"
    assert "JavaScript, which is not supported" in failure.errors[0].message


def test_a_changed_layout_is_unusable_not_empty() -> None:
    for page in (
        "<html><body><h1>Careers</h1><p>Plenty of text here.</p></body></html>",
        # A tracking <noscript> on an ordinary page does not make it a JavaScript page.
        "<html><body><noscript><img src='/px.gif'></noscript>"
        + "<p>About our company and culture. </p>" * 10
        + "</body></html>",
    ):
        extraction = listing(page).extraction
        assert extraction.response_type is None
        assert "layout may have changed" in extraction.failures[0].errors[0].message


def test_an_empty_listing_is_recognized_by_its_container_or_marker() -> None:
    marker = listing(html("listing_empty.html")).extraction
    assert (marker.response_type, marker.job_count, marker.failures) == ("listing", 0, ())
    container = listing('<ul class="jobs"></ul>').extraction
    assert (container.response_type, container.job_count) == ("listing", 0)
    no_marker = listing(html("listing_empty.html"), config(no_jobs=None)).extraction
    assert no_marker.response_type is None  # without the marker it is not provably empty


def test_non_text_input_is_an_invalid_response() -> None:
    for bad in (None, b"<html></html>", 42):
        assert listing(bad).extraction.failures[0].kind.value == "invalid_response"


# --- pagination ---------------------------------------------------------------------------------


def test_next_page_link_is_resolved_and_validated() -> None:
    page = listing(html("listing_page_1.html"))
    assert page.next_page == "https://careers.example.com/careers/jobs?page=2"
    assert page.extraction.warnings == ()
    last = listing(html("listing_page_2.html"), page_url=page.next_page, page_number=2)
    assert last.next_page is None and last.extraction.warnings == ()
    assert [r.title for r in last.extraction.records] == ["Mobile Engineer"]


def test_page_limit_stops_pagination_with_a_warning() -> None:
    page = listing(html("listing_page_1.html"), config(max_pages=2), page_number=2)
    assert page.next_page is None
    assert (
        page.extraction.warnings[0].message == "page limit (2) reached; later pages not collected"
    )
    single = listing(html("listing_page_1.html"), config(next_page=None, max_pages=None))
    assert single.next_page is None and single.extraction.warnings == ()  # not configured


def test_pagination_loops_are_detected() -> None:
    page_2 = "https://careers.example.com/careers/jobs?page=2"
    back = html("listing_page_2.html").replace('class="prev"', 'class="next"')
    looped = listing(back, page_url=page_2, page_number=2, visited_pages=[LISTING, page_2])
    assert looped.next_page is None
    assert looped.extraction.warnings[0].message == (
        "pagination loop: next page was already fetched"
    )
    itself = back.replace('href="/careers/jobs"', 'href="?page=2"')
    assert listing(itself, page_url=page_2, page_number=2).extraction.warnings == ()


@pytest.mark.parametrize(
    ("control", "expected"),
    [
        ('<a class="next" href="#" onclick="loadMore()">Next</a>', "needs JavaScript"),
        ('<a class="next" href="javascript:next()">Next</a>', "needs JavaScript"),
        ('<button class="next" onclick="more()">More</button>', "is not a link"),
        ('<a class="next" href="https://other.example.org/careers/p2">Next</a>', "another host"),
        ('<a class="next" href="/blog/page/2">Next</a>', "outside the configured path_prefix"),
        ('<a class="next" href="http://careers.example.com/careers/p2">Next</a>', "not an https"),
    ],
)
def test_unusable_next_page_controls_are_reported_and_not_followed(
    control: str, expected: str
) -> None:
    markup = html("listing_broken_pagination.html").replace(
        '<a class="next" href="#" onclick="loadMore()">Next</a>', control
    )
    page = listing(markup, config(next_page=".next"))
    assert page.next_page is None
    (warning,) = page.extraction.warnings
    assert warning.field == "next_page" and expected in warning.message
    assert [r.title for r in page.extraction.records] == ["Field Engineer"]  # jobs still kept


# --- detail pages -------------------------------------------------------------------------------


def detail_config() -> HtmlConfig:
    return config(detail=DETAIL, location=".job-location")


def test_with_detail_selectors_cards_wait_for_their_detail_page() -> None:
    page = listing(html("listing_unordered_list.html"), detail_config())
    assert page.extraction.records == () and page.extraction.job_count == 0
    assert [(j.index, j.url, j.title) for j in page.pending] == [
        (0, "https://careers.example.com/careers/jobs/101", "Backend Engineer"),
        (1, "https://careers.example.com/careers/jobs/102", "Data Analyst"),
        (2, "https://careers.example.com/careers/jobs/103", "Support Engineer"),
    ]
    assert settled(page) + len(page.pending) == 3


def test_detail_page_fills_in_and_overrides_card_fields() -> None:
    cfg = detail_config()
    backend, _, support = listing(html("listing_unordered_list.html"), cfg).pending
    result = detail(html("detail_page_101.html"), backend, cfg)
    assert (result.response_type, result.job_count, result.failures) == ("detail", 1, ())
    (record,) = result.records
    assert record.title == "Backend Engineer"  # always the card's title
    assert record.location == "Bengaluru, Karnataka, India"  # the detail page wins
    assert record.posted_date == date(2026, 11, 12) and record.posted_date_source == "page_text"
    assert record.employment_type == "Full-time"
    assert record.description_snippet == "About the role Design and run our APIs."
    assert str(record.source_url) == str(record.apply_url) == backend.url

    (plain,) = detail(html("detail_plain_html.html"), support, cfg).records
    assert plain.location == "Remote, India"
    assert plain.posted_date == date(2026, 11, 13)  # "Posted 3 days ago"
    assert plain.employment_type is None  # not on that page, not invented


def test_card_values_remain_when_the_detail_page_lacks_them() -> None:
    cfg = config(detail={"description": "div.description"}, location=".job-location")
    job = listing(html("listing_unordered_list.html"), cfg).pending[0]
    (record,) = detail(html("detail_page_101.html"), job, cfg).records
    assert record.location == "Bengaluru"  # from the card


def test_a_page_without_any_detail_field_fails_the_job() -> None:
    cfg = detail_config()
    job = listing(html("listing_unordered_list.html"), cfg).pending[0]
    gone = "<html><body><h1>This job is no longer available</h1></body></html>"
    result = detail(gone, job, cfg)
    assert result.records == () and result.job_count == 1
    assert "none of the configured detail fields" in result.failures[0].errors[0].message
    assert detail(None, job, cfg).failures[0].kind.value == "invalid_response"


def test_an_unavailable_detail_page_fails_only_its_job() -> None:
    cfg = detail_config()
    job = listing(html("listing_unordered_list.html"), cfg).pending[1]
    result = detail_unavailable(job, "HTTP 404")
    assert result.response_type == "detail" and result.job_count == 1
    (failure,) = result.failures
    assert (failure.index, failure.errors[0].message) == (1, "detail page unavailable (HTTP 404)")


def test_detail_extraction_needs_detail_selectors() -> None:
    job = listing(html("listing_unordered_list.html"), detail_config()).pending[0]
    with pytest.raises(ValueError, match="no detail selectors"):
        detail("<html></html>", job, config())


def test_naive_and_utc_collection_times() -> None:
    utc = listing(html("listing_invalid_date.html"), collected_at=NOW.astimezone(UTC))
    assert {r.title: r.posted_date for r in utc.extraction.records}["Controller"] == date(
        2026, 11, 13
    )
