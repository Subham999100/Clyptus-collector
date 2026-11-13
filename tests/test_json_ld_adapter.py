"""JSON-LD JobPosting adapter tests. Offline: synthetic HTML fixtures and in-memory pages only."""

import ast
import copy
import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from collector.adapters import common, json_ld
from collector.adapters.json_ld import (
    DESCRIPTION_SNIPPET_MAX,
    ExtractionFailure,
    FailureKind,
    JsonLdExtraction,
    extract_json_ld_jobs,
)
from collector.models import JobRecord
from collector.pipelines.date_window import DateOutcome, check_date_window
from collector.pipelines.dedupe import dedupe
from collector.pipelines.validate import Rejection, validate_job
from collector.utils.fingerprint import content_fingerprint
from collector.utils.identity import job_key
from collector.utils.urls import normalize_url

FIXTURES = Path(__file__).parent / "fixtures" / "jsonld"
PAGE_URL = "https://www.example.com/careers/software-engineer"
COLLECTED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ALL_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.html"))
JOB_URL = "https://www.example.com/careers/software-engineer"
DROP = object()  # pass as an override to delete a key


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def extract(html: Any, **overrides: Any) -> JsonLdExtraction:
    kwargs: dict[str, Any] = {"page_url": PAGE_URL, "collected_at": COLLECTED_AT}
    kwargs.update(overrides)
    return extract_json_ld_jobs(html, **kwargs)


def posting(**overrides: Any) -> dict[str, Any]:
    """A complete JobPosting object, with keys replaced (``DROP`` deletes)."""
    item: dict[str, Any] = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Software Engineer",
        "description": "<p>Build and maintain services.</p>",
        "identifier": {"@type": "PropertyValue", "name": "Example Corp", "value": "JOB-1001"},
        "datePosted": "2026-10-01",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {
            "@type": "Organization",
            "name": "Example Corp",
            "sameAs": "https://www.example.com",
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Bengaluru",
                "addressRegion": "Karnataka",
                "addressCountry": "IN",
            },
        },
        "url": JOB_URL,
    }
    for key, value in overrides.items():
        if value is DROP:
            item.pop(key, None)
        else:
            item[key] = value
    return item


def page(*blocks: Any) -> str:
    """An HTML page embedding each value as one JSON-LD script block (strings stay verbatim)."""

    def script(block: Any) -> str:
        text = block if isinstance(block, str) else json.dumps(block)
        return f'<script type="application/ld+json">{text}</script>'

    scripts = "\n".join(script(block) for block in blocks)
    return f"<!DOCTYPE html><html><head><title>Careers</title>{scripts}</head><body></body></html>"


def only_record(result: JsonLdExtraction) -> JobRecord:
    assert len(result.records) == 1, result.failures
    assert not result.failures
    return result.records[0]


def only_failure(result: JsonLdExtraction) -> ExtractionFailure:
    assert len(result.failures) == 1 and not result.records, result.records
    return result.failures[0]


# --- single postings and fixtures -------------------------------------------------------------


def test_single_job_posting_fixture() -> None:
    result = extract(load("single_job_posting.html"))
    assert (result.response_type, result.job_count) == ("listing", 1)
    record = only_record(result)
    assert record.title == "Software Engineer"
    assert record.company_name == "Example Corp"
    assert str(record.apply_url) == "https://www.example.com/careers/software-engineer"
    assert str(record.source_url) == PAGE_URL
    assert record.platform == "jsonld"
    assert record.collected_at == COLLECTED_AT
    assert str(record.company_website) == "https://www.example.com/"
    assert record.location == "Bengaluru, Karnataka, IN"
    assert record.work_type is None  # no jobLocationType
    assert record.employment_type == "FULL_TIME"
    assert (record.posted_date, record.posted_date_source) == (date(2026, 10, 1), "jsonld")
    assert record.external_id == "JOB-1001"
    assert record.description_snippet == "Build and maintain services. Python"
    assert record.salary_text is None  # baseSalary is present in the source but not mapped


def test_page_without_json_ld_blocks_extracts_nothing() -> None:
    html = "<!DOCTYPE html><html><head></head><body><h1>Careers</h1></body></html>"
    assert extract(html) == JsonLdExtraction((), (), "listing", 0)


def test_non_string_page_is_an_invalid_response() -> None:
    result = extract(None)
    failure = only_failure(result)
    assert failure.kind is FailureKind.INVALID_RESPONSE and failure.index is None


def test_blank_page_extracts_nothing() -> None:
    assert extract("   ") == JsonLdExtraction((), (), "listing", 0)


# --- multiple scripts, arrays, @graph, nesting -------------------------------------------------


def test_multiple_job_postings_fixture_keeps_source_order() -> None:
    result = extract(load("multiple_job_postings.html"))
    assert [r.title for r in result.records] == ["Software Engineer", "Data Analyst"]
    assert result.job_count == 2
    assert [r.external_id for r in result.records] == ["JOB-1001", "JOB-1002"]
    # the second posting's datePosted has an explicit offset: the date as written is kept
    assert [r.posted_date for r in result.records] == [date(2026, 10, 1), date(2026, 10, 3)]


def test_array_block_with_mixed_types_fixture() -> None:
    result = extract(load("mixed_schema_objects.html"))
    record = only_record(result)
    assert record.title == "Software Engineer"
    assert result.job_count == 1  # Organization, BreadcrumbList and Product are not jobs


def test_graph_representation_fixture() -> None:
    record = only_record(extract(load("graph_representation.html")))
    assert record.title == "Software Engineer"
    assert record.external_id == "JOB-1001"


def test_multiple_postings_in_one_array_block() -> None:
    result = extract(page([posting(title="A"), posting(title="B", url=DROP)]))
    assert [r.title for r in result.records] == ["A", "B"]


def test_graph_nested_inside_an_array_is_found() -> None:
    block = [{"@context": "https://schema.org", "@graph": [posting(title="Nested")]}]
    assert only_record(extract(page(block))).title == "Nested"


def test_hostile_nesting_is_cut_off_without_recursion() -> None:
    deep: Any = posting(title="Too deep")
    for _ in range(200):
        deep = {"@graph": [deep]}
    result = extract(page(deep))
    assert result.records == () and result.failures == ()  # beyond the documented depth


@pytest.mark.parametrize(
    "declared",
    ["JobPosting", ["JobPosting"], ["Thing", "JobPosting"], "https://schema.org/JobPosting",
     "schema:JobPosting", "jobposting"],
)  # fmt: skip
def test_job_posting_type_spellings_are_recognized(declared: Any) -> None:
    item = posting()
    item["@type"] = declared
    assert only_record(extract(page(item))).title == "Software Engineer"


@pytest.mark.parametrize(
    "declared", ["Organization", "Product", ["BreadcrumbList"], None, 5, {"x": 1}]
)
def test_unrelated_or_malformed_types_are_ignored(declared: Any) -> None:
    item = posting()
    if declared is None:
        del item["@type"]
    else:
        item["@type"] = declared
    assert extract(page(item)) == JsonLdExtraction((), (), "listing", 0)


# --- malformed JSON and empty scripts ----------------------------------------------------------


def test_invalid_structured_data_fixture() -> None:
    result = extract(load("invalid_structured_data.html"))
    # two unparseable blocks and one JobPosting without a title: nothing is dropped silently
    assert result.records == ()
    assert [f.kind for f in result.failures] == [
        FailureKind.INVALID_RESPONSE,
        FailureKind.INVALID_RESPONSE,
        FailureKind.MISSING_FIELD,
    ]
    assert result.job_count == 3 == len(result.failures)


def test_malformed_block_never_hides_the_valid_ones() -> None:
    html = page('{"@type": "JobPosting", "title": "Broken', posting(title="Kept"))
    result = extract(html)
    assert [r.title for r in result.records] == ["Kept"]
    (failure,) = result.failures
    assert (failure.kind, failure.index) == (FailureKind.INVALID_RESPONSE, 0)
    assert failure.errors[0].field == "(script)"


def test_trailing_comma_is_rejected_by_strict_json() -> None:
    html = page('{"@type": "JobPosting", "title": "X",}')
    failure = only_failure(extract(html))
    assert failure.kind is FailureKind.INVALID_RESPONSE


@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
def test_blank_scripts_are_ignored(content: str) -> None:
    result = extract(page(content, posting()))
    assert len(result.records) == 1
    assert result.failures == () and result.job_count == 1


def test_non_json_ld_scripts_are_never_parsed_or_executed() -> None:
    html = (
        "<!DOCTYPE html><html><head>"
        '<script>window.jobs = [{"@type": "JobPosting", "title": "JS only"}];</script>'
        '<script type="text/javascript">fetch("https://evil.example/x")</script>'
        f'<script type="application/ld+json">{json.dumps(posting(title="Real"))}</script>'
        "</head><body></body></html>"
    )
    result = extract(html)
    assert [r.title for r in result.records] == ["Real"]
    assert result.failures == ()


def test_script_type_matching_is_case_insensitive() -> None:
    html = (
        "<!DOCTYPE html><html><head>"
        f'<script type=" Application/LD+JSON ">{json.dumps(posting())}</script>'
        "</head><body></body></html>"
    )
    assert only_record(extract(html)).title == "Software Engineer"


# --- required fields ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", [DROP, None, "", "  \t"])
def test_missing_or_blank_title_fails(title: Any) -> None:
    failure = only_failure(extract(page(posting(title=title))))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "title"
    assert failure.job_id == "JOB-1001"


def test_missing_hiring_organization_fails_without_a_configured_company() -> None:
    failure = only_failure(extract(page(posting(hiringOrganization=DROP))))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "hiringOrganization"


def test_configured_company_name_saves_a_posting_without_an_organization() -> None:
    record = only_record(
        extract(page(posting(hiringOrganization=DROP)), company_name="Configured Ltd")
    )
    assert record.company_name == "Configured Ltd"
    assert record.company_website is None


def test_configured_company_name_wins_over_the_page_organization() -> None:
    record = only_record(extract(page(posting()), company_name="Configured Ltd"))
    assert record.company_name == "Configured Ltd"
    assert str(record.company_website) == "https://www.example.com/"  # still from the page


def test_confidential_hiring_organization_is_not_a_usable_company() -> None:
    item = posting(hiringOrganization={"@type": "Organization", "name": "confidential"})
    failure = only_failure(extract(page(item)))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "hiringOrganization.name"
    # ...unless configuration names the company
    record = only_record(extract(page(item), company_name="Example Corp"))
    assert record.company_name == "Example Corp"


def test_plain_string_hiring_organization_is_accepted() -> None:
    record = only_record(extract(page(posting(hiringOrganization="Example Corp"))))
    assert record.company_name == "Example Corp"
    assert record.company_website is None


@pytest.mark.parametrize(
    ("org", "field"),
    [
        (5, "hiringOrganization"),
        (["Example Corp"], "hiringOrganization"),
        ({"@type": "Organization"}, "hiringOrganization.name"),
        ({"@type": "Organization", "name": 5}, "hiringOrganization.name"),
        ({"@type": "Organization", "name": "  "}, "hiringOrganization.name"),
    ],
)
def test_unusable_hiring_organization_fails(org: Any, field: str) -> None:
    failure = only_failure(extract(page(posting(hiringOrganization=org))))
    assert failure.errors[0].field == field


# --- URLs --------------------------------------------------------------------------------------


def test_posting_url_is_the_apply_url_when_present() -> None:
    record = only_record(extract(page(posting(url="https://www.example.com/jobs/1"))))
    assert str(record.apply_url) == "https://www.example.com/jobs/1"


@pytest.mark.parametrize("value", [DROP, None, "", "   "])
def test_absent_url_falls_back_to_the_page_url_without_a_warning(value: Any) -> None:
    result = extract(page(posting(url=value)))
    assert str(only_record(result).apply_url) == PAGE_URL
    assert result.warnings == ()


BAD_URLS = [
    "not a url",
    "/relative/path",
    "ftp://example.com/x",
    "javascript:alert(1)",
    "https://user:pass@example.com/x",
    "https://localhost/x",
    "http://127.0.0.1/x",
    "http://169.254.169.254/latest",
    "https://intranet/x",
]


@pytest.mark.parametrize("bad", BAD_URLS)
def test_unusable_url_falls_back_to_the_page_url_with_a_warning(bad: str) -> None:
    result = extract(page(posting(url=bad)))
    record = only_record(result)
    assert str(record.apply_url) == PAGE_URL
    (warning,) = result.warnings
    assert (warning.kind, warning.field) == (FailureKind.INVALID_URL, "url")
    assert bad not in warning.message


def test_wrong_type_url_falls_back_with_an_invalid_type_warning() -> None:
    result = extract(page(posting(url=["x"])))
    assert str(only_record(result).apply_url) == PAGE_URL
    assert [(w.kind, w.field) for w in result.warnings] == [(FailureKind.INVALID_TYPE, "url")]


def test_no_apply_url_is_invented_from_direct_apply() -> None:
    record = only_record(extract(page(posting(url=DROP, directApply=True))))
    assert str(record.apply_url) == PAGE_URL  # directApply is only a hint, never a URL


def test_company_website_from_same_as_then_url() -> None:
    org = {"@type": "Organization", "name": "Example Corp", "url": "https://corp.example.com"}
    record = only_record(extract(page(posting(hiringOrganization=org))))
    assert str(record.company_website) == "https://corp.example.com/"


def test_unusable_same_as_falls_back_to_org_url_with_a_warning() -> None:
    org = {
        "@type": "Organization",
        "name": "Example Corp",
        "sameAs": "ftp://x",
        "url": "https://corp.example.com",
    }
    result = extract(page(posting(hiringOrganization=org)))
    assert str(only_record(result).company_website) == "https://corp.example.com/"
    (warning,) = result.warnings
    assert warning.field == "hiringOrganization.sameAs"


def test_both_org_urls_unusable_leaves_the_website_unset_with_warnings() -> None:
    org = {"@type": "Organization", "name": "Example Corp", "sameAs": "nope", "url": ["x"]}
    result = extract(page(posting(hiringOrganization=org)))
    assert only_record(result).company_website is None
    assert [w.field for w in result.warnings] == [
        "hiringOrganization.sameAs",
        "hiringOrganization.url",
    ]


def test_configured_website_wins_and_silences_org_url_warnings() -> None:
    org = {"@type": "Organization", "name": "Example Corp", "sameAs": "nope"}
    result = extract(page(posting(hiringOrganization=org)), company_website="https://Example.org")
    assert str(only_record(result).company_website) == "https://example.org/"
    assert result.warnings == ()


# --- identifier --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ({"@type": "PropertyValue", "name": "Example Corp", "value": "JOB-1001"}, "JOB-1001"),
        ({"@type": "PropertyValue", "value": 12345}, "12345"),
        ({"@type": "PropertyValue", "value": " JOB 7 "}, "JOB 7"),
        ("REQ-42", "REQ-42"),
        (12345, "12345"),
        ({"@type": "PropertyValue"}, None),
        ({"@type": "PropertyValue", "value": None}, None),
        (None, None),
        (DROP, None),
    ],
)
def test_identifier_forms(identifier: Any, expected: str | None) -> None:
    record = only_record(extract(page(posting(identifier=identifier))))
    assert record.external_id == expected


def test_absent_identifier_falls_back_to_url_identity() -> None:
    record = only_record(extract(page(posting(identifier=DROP))))
    assert record.external_id is None
    key = normalize_url(str(record.apply_url))
    assert job_key(record) == f"url:{hashlib.sha256(key.encode()).hexdigest()}"


def test_multiple_postings_without_job_specific_identity_fail_instead_of_colliding() -> None:
    result = extract(
        page(
            posting(title="Role A", identifier=DROP, url=DROP),
            posting(title="Role B", identifier=DROP, url=DROP),
        )
    )

    assert result.records == ()
    assert [
        (failure.index, failure.kind, failure.errors[0].field) for failure in result.failures
    ] == [
        (0, FailureKind.INVALID_RESPONSE, "(identity)"),
        (1, FailureKind.INVALID_RESPONSE, "(identity)"),
    ]
    assert result.job_count == len(result.failures) == 2
    assert "Role A" not in repr(result.failures)
    assert "Role B" not in repr(result.failures)


def test_missing_identifier_with_distinct_job_urls_keeps_both_postings() -> None:
    result = extract(
        page(
            posting(title="Role A", identifier=DROP, url="https://example.com/jobs/a"),
            posting(title="Role B", identifier=DROP, url="https://example.com/jobs/b"),
        )
    )

    assert [record.title for record in result.records] == ["Role A", "Role B"]
    assert result.failures == ()
    assert len({job_key(record) for record in result.records}) == 2


@pytest.mark.parametrize(
    ("identifier", "field"),
    [
        (True, "identifier"),
        (1.5, "identifier"),
        (["x"], "identifier"),
        ("", "identifier"),
        ("   ", "identifier"),
        ({"@type": "PropertyValue", "value": ["x"]}, "identifier.value"),
        ({"@type": "PropertyValue", "value": ""}, "identifier.value"),
    ],
)
def test_unusable_identifier_is_a_fatal_invalid_type(identifier: Any, field: str) -> None:
    failure = only_failure(extract(page(posting(identifier=identifier))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, field)


def test_identity_is_stable_across_collection_times_and_pages() -> None:
    a = only_record(extract(page(posting())))
    b = only_record(
        extract(
            page(posting()),
            collected_at=COLLECTED_AT + timedelta(days=3),
            page_url="https://www.example.com/careers?page=2",
        )
    )
    assert job_key(a) == job_key(b)


def test_duplicate_postings_are_both_extracted_and_phase1_dedupes_them() -> None:
    result = extract(page(posting(), posting(), posting(identifier="OTHER", url=DROP)))
    assert len(result.records) == 3
    deduped = dedupe(result.records)
    assert (len(deduped.accepted), len(deduped.duplicates)) == (2, 1)


# --- locations and remote ----------------------------------------------------------------------


def test_multiple_locations_and_array_employment_type_fixture() -> None:
    result = extract(load("multiple_locations_html_description.html"))
    record = only_record(result)
    assert record.location == "Pune, Maharashtra, IN; Hyderabad, Telangana, IN"
    assert record.employment_type == "FULL_TIME, CONTRACTOR"
    # HTML-escaped description is decoded once, then converted to text
    assert record.description_snippet == "Test & verify. Selenium"
    # the datePosted offset is not converted: the date as written is kept
    assert (record.posted_date, record.posted_date_source) == (date(2026, 10, 2), "jsonld")


def test_remote_fixture_sets_work_type_without_a_location() -> None:
    record = only_record(extract(load("remote_job_posting.html")))
    assert record.work_type == "remote"
    assert record.location is None  # applicantLocationRequirements is not a workplace


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"@type": "Place", "address": {"addressLocality": "Pune"}}, "Pune"),
        ({"@type": "Place", "address": "Pune, India"}, "Pune, India"),
        ({"@type": "Place", "name": "Pune Office"}, "Pune Office"),
        ({"@type": "Place"}, None),
        ("Pune, India", "Pune, India"),
        ([], None),
        (None, None),
        (DROP, None),
        (
            {"@type": "Place", "address": {"addressLocality": " Pune ", "addressCountry": "IN"}},
            "Pune, IN",
        ),
        (
            {"@type": "Place", "address": {"addressCountry": {"@type": "Country", "name": "IN"}}},
            "IN",
        ),
        (
            [
                {"@type": "Place", "address": {"addressLocality": "Pune"}},
                {"@type": "Place", "address": {"addressLocality": " pune "}},
                {"@type": "Place", "address": {"addressLocality": "Delhi"}},
            ],
            "Pune; Delhi",  # case-insensitive dedupe, source order
        ),
    ],
)
def test_job_location_forms(value: Any, expected: str | None) -> None:
    assert only_record(extract(page(posting(jobLocation=value)))).location == expected


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (5, "jobLocation"),
        ([5], "jobLocation"),
        ({"@type": "Place", "address": 5}, "jobLocation.address"),
        ({"@type": "Place", "name": 5}, "jobLocation.name"),
        (
            {"@type": "Place", "address": {"addressLocality": 5}},
            "jobLocation.address.addressLocality",
        ),
    ],
)
def test_wrong_type_job_location_fails(value: Any, field: str) -> None:
    failure = only_failure(extract(page(posting(jobLocation=value))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, field)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("TELECOMMUTE", "remote"),
        (" telecommute ", "remote"),
        (["TELECOMMUTE"], "remote"),
        ("ONSITE", None),  # not in the vocabulary; nothing is guessed
        ("", None),
        ([], None),
        (None, None),
        (DROP, None),
    ],
)
def test_job_location_type_maps_only_telecommute(value: Any, expected: str | None) -> None:
    assert only_record(extract(page(posting(jobLocationType=value)))).work_type == expected


def test_work_type_is_never_inferred_from_text() -> None:
    item = posting(jobLocationType=DROP, description="<p>This is a fully remote role.</p>")
    assert only_record(extract(page(item))).work_type is None


@pytest.mark.parametrize("value", [5, [5], [None]])
def test_wrong_type_job_location_type_fails(value: Any) -> None:
    failure = only_failure(extract(page(posting(jobLocationType=value))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, "jobLocationType")


# --- employment type and description -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("FULL_TIME", "FULL_TIME"),
        (["FULL_TIME", "CONTRACTOR"], "FULL_TIME, CONTRACTOR"),
        (["FULL_TIME", "full_time"], "FULL_TIME"),  # case-insensitive dedupe
        ("  Part   time ", "Part time"),  # verbatim, collapsed; not mapped to a vocabulary
        ([""], None),
        ([], None),
        ("", None),
        (None, None),
        (DROP, None),
    ],
)
def test_employment_type_forms(value: Any, expected: str | None) -> None:
    assert only_record(extract(page(posting(employmentType=value)))).employment_type == expected


@pytest.mark.parametrize("value", [5, [5], ["FULL_TIME", 5], {"x": 1}])
def test_wrong_type_employment_type_fails(value: Any) -> None:
    failure = only_failure(extract(page(posting(employmentType=value))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, "employmentType")


def test_escaped_html_description_is_decoded_to_markup_once() -> None:
    # fully escaped content is decoded once into markup; the markup's own entities are then
    # decoded by the HTML parser, exactly as for Greenhouse's entity-encoded ``content``
    item = posting(description="&lt;p&gt;R&amp;D &amp;amp; &lt;b&gt;x&lt;/b&gt;&lt;/p&gt;")
    assert only_record(extract(page(item))).description_snippet == "R&D & x"


def test_description_html_is_stripped_and_never_executed() -> None:
    markup = "<p>Hello</p><script>steal()</script><ul><li>One</li></ul>"
    # a raw "</script>" inside the JSON would end the embedded block early, so the block is
    # serialized with the standard "<\/" escape, exactly as careful real pages emit it
    block = json.dumps(posting(description=markup)).replace("</", "<\\/")
    assert only_record(extract(page(block))).description_snippet == "Hello One"


@pytest.mark.parametrize("value", [DROP, None, "", "<p> </p>"])
def test_missing_or_empty_description_is_none(value: Any) -> None:
    assert only_record(extract(page(posting(description=value)))).description_snippet is None


def test_wrong_type_description_fails() -> None:
    failure = only_failure(extract(page(posting(description={"x": 1}))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, "description")


def test_snippet_never_exceeds_limit() -> None:
    text = "<p>" + " ".join(f"word{i}" for i in range(2000)) + "</p>"
    snippet = only_record(extract(page(posting(description=text)))).description_snippet
    assert snippet is not None and len(snippet) <= DESCRIPTION_SNIPPET_MAX


def test_description_never_changes_the_company() -> None:
    item = posting(description="<p>Join Globex Corporation (globex.example) today</p>")
    record = only_record(extract(page(item)))
    assert record.company_name == "Example Corp"


# --- posting dates: datePosted only ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-01", date(2026, 10, 1)),
        ("2026-10-01T09:00:00Z", date(2026, 10, 1)),
        ("2026-10-03T09:00:00+05:30", date(2026, 10, 3)),
        ("2026-10-02T18:30:00-05:00", date(2026, 10, 2)),  # the date as written, not converted
    ],
)
def test_valid_iso_date_posted_is_used(value: str, expected: date) -> None:
    record = only_record(extract(page(posting(datePosted=value))))
    assert (record.posted_date, record.posted_date_source) == (expected, "jsonld")


@pytest.mark.parametrize("value", [DROP, None, "", "   "])
def test_absent_date_posted_means_unknown_without_a_warning(value: Any) -> None:
    result = extract(page(posting(datePosted=value)))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    assert result.warnings == ()


def test_missing_date_posted_fixture_is_unknown_date() -> None:
    result = extract(load("missing_date_posted.html"))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    assert result.warnings == ()
    window = check_date_window(record, COLLECTED_AT)
    assert window.outcome is DateOutcome.UNKNOWN_DATE
    assert window.needs_review and not window.eligible


@pytest.mark.parametrize(
    "value", ["3 days ago", "yesterday", "2026-13-45", "01/10/2026", "October 1, 2026"]
)
def test_malformed_date_posted_is_a_warning_and_the_job_is_kept(value: str) -> None:
    result = extract(page(posting(datePosted=value)))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    (warning,) = result.warnings
    assert (warning.kind, warning.field) == (FailureKind.INVALID_DATE, "datePosted")
    assert warning.job_id == "JOB-1001"
    assert value not in warning.message


@pytest.mark.parametrize("value", [20261001, True, ["2026-10-01"], {"date": "x"}])
def test_wrong_type_date_posted_is_a_warning_and_the_job_is_kept(value: Any) -> None:
    result = extract(page(posting(datePosted=value)))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    (warning,) = result.warnings
    assert (warning.kind, warning.field) == (FailureKind.INVALID_TYPE, "datePosted")


def test_valid_through_is_never_a_posting_date() -> None:
    item = posting(datePosted=DROP, validThrough="2026-12-01T00:00")
    record = only_record(extract(page(item)))
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")


@pytest.mark.parametrize(
    "collected_at",
    [
        COLLECTED_AT,
        datetime(2030, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 20, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    ],
)
def test_collected_at_is_never_substituted_as_the_posting_date(collected_at: datetime) -> None:
    for html in (load("missing_date_posted.html"), page(posting(datePosted="garbage"))):
        record = extract(html, collected_at=collected_at).records[0]
        assert (record.posted_date, record.posted_date_source) == (None, "unknown")
        assert record.collected_at == collected_at


def test_valid_through_is_not_in_the_extraction_path_at_all() -> None:
    """An expiry is not a publication date: the adapter code never refers to the field."""
    assert json_ld.__file__ is not None
    tree = ast.parse(Path(json_ld.__file__).read_text(encoding="utf-8"))
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert "validThrough" not in strings


# --- configuration -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_name": ""},
        {"company_name": "   "},
        {"collected_at": datetime(2026, 10, 20, 12, 0)},
        {"page_url": "not a url"},
        {"page_url": "https://localhost/x"},
        {"page_url": "ftp://example.com/x"},
        {"company_website": "ftp://example.com"},
        {"company_website": "https://user:pw@example.com"},
    ],
)
def test_invalid_configuration_raises(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        extract(load("single_job_posting.html"), **overrides)


def test_non_utc_collection_time_is_stored_as_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    moment = datetime(2026, 10, 20, 17, 30, tzinfo=ist)
    record = only_record(extract(load("single_job_posting.html"), collected_at=moment))
    assert record.collected_at == COLLECTED_AT


def test_source_url_is_the_page_url_as_requested() -> None:
    record = only_record(extract(page(posting()), page_url=PAGE_URL + "?lang=en"))
    assert str(record.source_url) == PAGE_URL + "?lang=en"


def test_unknown_properties_are_ignored() -> None:
    item = posting(
        baseSalary={"@type": "MonetaryAmount", "currency": "INR"},
        directApply=True,
        applicantLocationRequirements={"@type": "Country", "name": "India"},
        experienceRequirements="none",
    )
    assert only_record(extract(page(item))) == only_record(extract(page(posting())))


# --- isolation and safety ----------------------------------------------------------------------


def test_one_invalid_posting_does_not_stop_the_others() -> None:
    html = page(
        posting(title="A"),
        posting(title=None, identifier="BAD-1"),
        posting(title="C", url=DROP, identifier="C-1"),
    )
    result = extract(html)
    assert [r.title for r in result.records] == ["A", "C"]
    assert [(f.index, f.job_id, f.kind) for f in result.failures] == [
        (1, "BAD-1", FailureKind.MISSING_FIELD)
    ]
    assert result.job_count == 3


def test_all_problems_of_a_posting_are_reported_together() -> None:
    item = posting(
        title="",
        hiringOrganization=5,
        jobLocation=7,
        jobLocationType=9,
        employmentType={"x": 1},
        description=["x"],
    )
    failure = only_failure(extract(page(item)))
    assert [(e.field, e.code) for e in failure.errors] == [
        ("title", "missing_field"),
        ("hiringOrganization", "invalid_type"),
        ("jobLocation", "invalid_type"),
        ("jobLocationType", "invalid_type"),
        ("employmentType", "invalid_type"),
        ("description", "invalid_type"),
    ]
    assert failure.kind is FailureKind.MISSING_FIELD  # kind of the first problem


def test_warning_for_a_failed_posting_is_still_reported() -> None:
    result = extract(page(posting(title=None, datePosted="garbage")))
    assert only_failure(result).errors[0].field == "title"
    assert [w.field for w in result.warnings] == ["datePosted"]


def test_messages_never_expose_source_values_or_html() -> None:
    # a raw "</script>" would end the embedded block early, so the marker carries no tags
    marker = "SECRET-MARKER-9000"
    bad = [
        posting(title=[marker]),
        posting(hiringOrganization=[marker]),
        posting(jobLocation=[[marker]]),
        posting(employmentType={"x": marker}),
        posting(description={"x": marker}),
        posting(identifier=[marker]),
    ]
    warned = [
        posting(url=f"https://{marker}@x.example"),
        posting(datePosted=marker),
    ]
    html = page(*bad, *warned, f'{{"@type": "JobPosting", "title": "{marker}"')
    result = extract(html)
    assert len(result.failures) == len(bad) + 1
    assert len(result.records) == len(warned) == len(result.warnings)
    text = repr(result.failures) + repr(result.warnings)
    assert "SECRET-MARKER" not in text


def test_every_fixture_extracts_without_warnings() -> None:
    assert all(extract(load(name)).warnings == () for name in ALL_FIXTURES)


# --- determinism, no mutation, no side effects --------------------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_output_is_deterministic(name: str) -> None:
    assert extract(load(name)) == extract(load(name))


def test_decoded_blocks_are_not_mutated() -> None:
    """The adapter parses its own copy of the text, so caller-held structures are untouched."""
    item = posting()
    snapshot = copy.deepcopy(item)
    extract(page(item))
    assert item == snapshot


def test_no_network_access(no_network: None) -> None:
    for name in ALL_FIXTURES:
        extract(load(name))


@pytest.mark.parametrize("module", [common, json_ld], ids=lambda m: m.__name__)
def test_adapter_modules_import_nothing_that_does_io_or_reads_the_clock(module: ModuleType) -> None:
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = (
        "socket", "sqlite3", "os", "sys", "requests", "httpx", "urllib3", "urllib.request",
        "scrapy", "time", "logging", "structlog", "pathlib", "shutil", "tempfile",
        "subprocess", "asyncio", "collector.settings", "collector.pipelines.store",
    )  # fmt: skip
    assert not [m for m in imported if m == "urllib" or m.startswith(forbidden)]
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not called & {"open", "print", "now", "utcnow", "today", "getenv", "get_settings"}


def test_extraction_touches_no_database_and_writes_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("database access attempted")

    monkeypatch.setattr(sqlite3, "connect", blocked)
    monkeypatch.chdir(tmp_path)
    for name in ALL_FIXTURES:
        extract(load(name))
    assert list(tmp_path.iterdir()) == []


# --- Phase 1 and fixture compatibility ----------------------------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_records_round_trip_through_phase1_validation(name: str) -> None:
    for record in extract(load(name)).records:
        revalidated = validate_job(record.model_dump())
        assert not isinstance(revalidated, Rejection), revalidated
        assert revalidated == record


def test_fingerprint_is_stable_and_ignores_observation_fields() -> None:
    a = only_record(extract(load("single_job_posting.html")))
    b = only_record(
        extract(
            load("single_job_posting.html"),
            collected_at=COLLECTED_AT + timedelta(days=3),
            page_url=PAGE_URL + "?ref=feed",
        )
    )
    assert content_fingerprint(a.model_dump()) == content_fingerprint(b.model_dump())
    changed = only_record(extract(page(posting(title="Renamed Role"))))
    assert content_fingerprint(changed.model_dump()) != content_fingerprint(a.model_dump())


def test_every_fixture_posting_is_extracted_or_reported() -> None:
    manifest = json.loads((FIXTURES.parent / "manifest.json").read_text(encoding="utf-8"))
    expected = {
        e["path"].split("/", 1)[1]: e["expected_listing_count"] + e.get("invalid_blocks", 0)
        for e in manifest["fixtures"]
        if e["platform"] == "jsonld"
    }
    assert set(expected) == set(ALL_FIXTURES)
    for name, count in expected.items():
        result = extract(load(name))
        assert result.job_count == len(result.records) + len(result.failures) == count, name
