"""Lever adapter tests. Offline: synthetic fixtures and in-memory payloads only."""

import ast
import copy
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from collector.adapters import common, greenhouse, lever
from collector.adapters.lever import (
    DESCRIPTION_SNIPPET_MAX,
    ExtractionFailure,
    FailureKind,
    LeverExtraction,
    extract_lever_jobs,
)
from collector.models import JobRecord
from collector.pipelines.date_window import DateOutcome, check_date_window
from collector.pipelines.dedupe import dedupe
from collector.pipelines.validate import Rejection, validate_job
from collector.utils.fingerprint import content_fingerprint
from collector.utils.identity import job_key
from collector.utils.urls import normalize_url

FIXTURES = Path(__file__).parent / "fixtures" / "lever"
SITE = "example-corp-fixture"
SOURCE_URL = f"https://api.lever.co/v0/postings/{SITE}?mode=json"
COLLECTED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ALL_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.json"))
FIRST_ID = "00000000-0000-4000-8000-000000000001"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DROP = object()  # pass as an override to delete a key


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def extract(payload: Any, **overrides: Any) -> LeverExtraction:
    kwargs: dict[str, Any] = {
        "company_name": "Example Corp",
        "site_slug": SITE,
        "source_url": SOURCE_URL,
        "collected_at": COLLECTED_AT,
    }
    kwargs.update(overrides)
    return extract_lever_jobs(payload, **kwargs)


def posting(**overrides: Any) -> dict[str, Any]:
    """A complete posting from the synthetic fixture, with keys replaced (``DROP`` deletes)."""
    item = copy.deepcopy(load("postings_complete.json")[0])
    for key, value in overrides.items():
        if value is DROP:
            item.pop(key, None)
        else:
            item[key] = value
    return item


def categories(**overrides: Any) -> dict[str, Any]:
    cats = copy.deepcopy(load("postings_complete.json")[0]["categories"])
    for key, value in overrides.items():
        if value is DROP:
            cats.pop(key, None)
        else:
            cats[key] = value
    return cats


def only_record(result: LeverExtraction) -> JobRecord:
    assert len(result.records) == 1, result.failures
    assert not result.failures
    return result.records[0]


def only_failure(result: LeverExtraction) -> ExtractionFailure:
    assert len(result.failures) == 1 and not result.records, result.records
    return result.failures[0]


def epoch_ms(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(milliseconds=1)


def url(n: int) -> str:
    return f"https://jobs.lever.co/{SITE}/{n}"


# --- 1-3: response shape --------------------------------------------------------------------


def test_complete_listing() -> None:
    result = extract(load("postings_complete.json"))
    assert (result.response_type, result.job_count) == ("listing", 1)
    record = only_record(result)
    assert record.title == "Product Designer"
    assert record.company_name == "Example Corp"
    assert str(record.apply_url) == f"https://jobs.lever.co/{SITE}/{FIRST_ID}/apply"
    assert str(record.source_url) == SOURCE_URL
    assert record.platform == "lever"
    assert record.collected_at == COLLECTED_AT
    assert record.location == "Pune, India"
    assert record.work_type == "hybrid"
    assert record.employment_type == "Full-time"
    assert record.external_id == f"{SITE}:{FIRST_ID}"
    assert record.description_snippet == "We are hiring a designer."
    # the fixture has a valid createdAt, which is deliberately not used
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    assert record.salary_text is None  # salary fields are present in the source but not mapped


def test_empty_listing() -> None:
    assert extract(load("postings_empty.json")) == LeverExtraction((), (), "listing", 0)


def test_multiple_postings_keep_source_order() -> None:
    jobs = [
        posting(id=f"id-{n}", text=t, applyUrl=url(n)) for n, t in ((3, "C"), (1, "A"), (2, "B"))
    ]
    result = extract(jobs)
    assert [r.title for r in result.records] == ["C", "A", "B"]
    assert result.job_count == 3


# --- 4-6: required fields and URLs ----------------------------------------------------------


@pytest.mark.parametrize("title", [DROP, None, "", "  \t"])
def test_missing_or_blank_title_fails(title: Any) -> None:
    failure = only_failure(extract([posting(text=title)]))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "text"


def test_fixture_posting_without_title_fails() -> None:
    first = extract(load("postings_invalid_incomplete.json")).failures[0]
    assert (first.kind, first.index, first.job_id) == (
        FailureKind.MISSING_FIELD,
        0,
        "00000000-0000-4000-8000-000000000007",
    )
    assert first.errors[0].field == "text"


BAD_URLS = [
    "not a url",
    "/relative/path",
    "//jobs.lever.co/x/1",
    "ftp://jobs.lever.co/x/1",
    "javascript:alert(1)",
    "https://",
    "https://user:pass@jobs.lever.co/x/1",
    "https://localhost/x/1",
    "http://127.0.0.1/x/1",
    "http://10.0.0.5/x/1",
    "http://192.168.1.1/x/1",
    "http://169.254.169.254/latest",
    "http://[::1]/x/1",
    "https://intranet/x/1",
    "https://jobs.lever.co:notaport/x/1",
]


@pytest.mark.parametrize("bad", BAD_URLS)
def test_invalid_or_unsafe_apply_url_without_a_fallback_fails(bad: str) -> None:
    failure = only_failure(extract([posting(applyUrl=bad, hostedUrl=DROP)]))
    assert failure.kind is FailureKind.INVALID_URL
    assert failure.errors[0].field == "applyUrl"
    assert bad not in repr(failure)


def test_both_urls_unusable_reports_both_fields() -> None:
    failure = only_failure(extract([posting(applyUrl="nope", hostedUrl="ftp://x")]))
    assert [(e.field, e.code) for e in failure.errors] == [
        ("applyUrl", "invalid_url"),
        ("hostedUrl", "invalid_url"),
    ]


@pytest.mark.parametrize("value", [DROP, None, "", "   "])
def test_both_urls_missing_is_a_missing_field_failure(value: Any) -> None:
    failure = only_failure(extract([posting(applyUrl=value, hostedUrl=value)]))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "applyUrl"


def test_fixture_posting_without_urls_fails() -> None:
    second = extract(load("postings_invalid_incomplete.json")).failures[1]
    assert (second.kind, second.index) == (FailureKind.MISSING_FIELD, 1)


def test_apply_url_is_preferred_over_hosted_url() -> None:
    record = only_record(extract([posting(applyUrl=url(1), hostedUrl=url(2))]))
    assert str(record.apply_url) == url(1)


@pytest.mark.parametrize("apply_value", [DROP, None, "", "  "])
def test_absent_apply_url_falls_back_to_hosted_url_without_a_warning(apply_value: Any) -> None:
    result = extract([posting(applyUrl=apply_value, hostedUrl=url(2))])
    assert str(only_record(result).apply_url) == url(2)
    assert result.warnings == ()


def test_unusable_apply_url_falls_back_to_hosted_url_with_a_warning() -> None:
    result = extract([posting(applyUrl="https://user:pw@jobs.lever.co/x", hostedUrl=url(2))])
    assert str(only_record(result).apply_url) == url(2)
    (warning,) = result.warnings
    assert (warning.kind, warning.index, warning.field) == (FailureKind.INVALID_URL, 0, "applyUrl")
    assert warning.job_id == FIRST_ID
    assert "pw" not in warning.message and "user" not in warning.message


def test_wrong_type_apply_url_falls_back_with_an_invalid_type_warning() -> None:
    result = extract([posting(applyUrl=["x"], hostedUrl=url(2))])
    assert str(only_record(result).apply_url) == url(2)
    assert [(w.kind, w.field) for w in result.warnings] == [(FailureKind.INVALID_TYPE, "applyUrl")]


def test_valid_apply_url_ignores_a_bad_hosted_url() -> None:
    result = extract([posting(applyUrl=url(1), hostedUrl="ftp://nope")])
    assert str(only_record(result).apply_url) == url(1)
    assert result.warnings == ()


def test_unusable_apply_url_and_unusable_hosted_url_are_fatal_not_warnings() -> None:
    result = extract([posting(applyUrl="nope", hostedUrl=["x"])])
    assert only_failure(result).kind is FailureKind.INVALID_URL
    assert result.warnings == ()


def test_url_longer_than_the_model_allows_is_caught_by_phase1_validation() -> None:
    long_url = "https://jobs.lever.co/x/1?q=" + "a" * 2100
    failure = only_failure(extract([posting(applyUrl=long_url)]))
    assert failure.kind is FailureKind.INVALID_URL
    assert long_url not in repr(failure)


def test_apply_url_is_normalized() -> None:
    messy = " HTTPS://Jobs.Lever.co:443/x/1/apply?utm_source=a&lever-source=b#top "
    record = only_record(extract([posting(applyUrl=messy)]))
    assert str(record.apply_url) == "https://jobs.lever.co/x/1/apply?lever-source=b"


def test_no_url_is_constructed_from_slug_and_id() -> None:
    failure = only_failure(extract([posting(applyUrl=DROP, hostedUrl=DROP)]))
    assert failure.kind is FailureKind.MISSING_FIELD


# --- 7-8: optional fields and locations -----------------------------------------------------


def test_missing_optional_fields_stay_none() -> None:
    record = only_record(extract(load("postings_missing_optional_fields.json")))
    assert record.employment_type is None  # no commitment
    assert record.work_type is None  # workplaceType is "unspecified"
    assert record.salary_text is None
    assert record.company_website is None
    assert record.location == "Pune, India"


def test_minimal_posting_needs_only_title_and_a_url() -> None:
    record = only_record(extract([{"text": "Role", "hostedUrl": url(1)}]))
    assert (record.location, record.work_type, record.employment_type) == (None, None, None)
    assert record.description_snippet is None
    assert (record.external_id, record.posted_date) == (None, None)


def test_multiple_locations_fixture() -> None:
    record = only_record(extract(load("postings_multiple_locations.json")))
    assert record.location == "Pune, India; Bengaluru, India"


@pytest.mark.parametrize(
    ("cats", "expected"),
    [
        ({"location": "A", "allLocations": ["A", "B", "C"]}, "A; B; C"),
        ({"location": "A", "allLocations": ["B", "A"]}, "A; B"),  # primary first, no duplicate
        ({"location": "A", "allLocations": ["a ", " B", "b"]}, "A; B"),  # case-insensitive dedupe
        ({"location": "C", "allLocations": ["A", "B"]}, "C; A; B"),  # primary not in the list
        ({"allLocations": ["A", "B"]}, "A; B"),
        ({"location": "A"}, "A"),
        ({"location": "  A,\t B  "}, "A, B"),
        ({"location": "", "allLocations": []}, None),
        ({"location": None, "allLocations": None}, None),
        ({}, None),
    ],
)
def test_locations_are_combined_deterministically(
    cats: dict[str, Any], expected: str | None
) -> None:
    assert only_record(extract([posting(categories=cats)])).location == expected


@pytest.mark.parametrize("value", [DROP, None])
def test_absent_categories_is_fine(value: Any) -> None:
    record = only_record(extract([posting(categories=value)]))
    assert (record.location, record.employment_type) == (None, None)


# --- 9-11: workplace type -------------------------------------------------------------------


def test_remote_fixture() -> None:
    record = only_record(extract(load("postings_remote.json")))
    assert (record.work_type, record.location) == ("remote", "Remote")


def test_hybrid_fixture() -> None:
    assert only_record(extract(load("postings_complete.json"))).work_type == "hybrid"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("remote", "remote"),
        ("hybrid", "hybrid"),
        ("on-site", "onsite"),
        (" Remote ", "remote"),
        ("HYBRID", "hybrid"),
        ("unspecified", None),
        ("flexible", None),  # unknown values are not guessed
        ("onsite", None),  # only the documented spelling counts
        ("", None),
        (None, None),
        (DROP, None),
    ],
)
def test_workplace_type_uses_only_documented_values(value: Any, expected: str | None) -> None:
    assert only_record(extract([posting(workplaceType=value)])).work_type == expected


def test_work_type_is_never_inferred_from_text() -> None:
    job = posting(
        workplaceType="unspecified",
        categories=categories(location="Remote - India"),
        descriptionPlain="This is a fully remote role.",
    )
    assert only_record(extract([job])).work_type is None


def test_workplace_type_inside_categories_is_not_read() -> None:
    job = posting(workplaceType=DROP, categories=categories(workplaceType="remote"))
    assert only_record(extract([job])).work_type is None  # undocumented location


def test_wrong_type_workplace_type_fails() -> None:
    failure = only_failure(extract([posting(workplaceType=3)]))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, "workplaceType")


# --- 12: employment type --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("commitment", "expected"),
    [
        ("Full-time", "Full-time"),
        ("  Part   time ", "Part time"),
        ("Contract", "Contract"),
        ("Intern", "Intern"),
        ("Full-time / Contract", None),
        ("Full-time, Part-time", None),
        ("Full-time; Contract", None),
        ("Part-time | Contract", None),
        ("Contract or Temporary", None),
        ("Permanent and Full-time", None),
        ("x" * 101, None),
        ("", None),
        ("   ", None),
        (None, None),
        (DROP, None),
    ],
)
def test_employment_type_only_when_unambiguous(commitment: Any, expected: str | None) -> None:
    job = posting(categories=categories(commitment=commitment))
    assert only_record(extract([job])).employment_type == expected


def test_wrong_type_commitment_fails() -> None:
    failure = only_failure(extract([posting(categories=categories(commitment=["Full-time"]))]))
    assert failure.errors[0].field == "categories.commitment"


# --- 13-15: descriptions --------------------------------------------------------------------


def test_html_description_is_converted_when_there_is_no_plain_text() -> None:
    item = load("postings_html_description.json")[0]
    del item["descriptionPlain"]
    assert only_record(extract([item])).description_snippet == (
        "About us We build tools & services. Remote friendly"
    )


def test_html_conversion_drops_scripts_styles_comments_and_keeps_words_apart() -> None:
    markup = (
        "<div>Hello<b>World</b></div><script>steal()</script><style>p{x:y}</style>"
        "<!-- hidden --><p>Next</p><img src='https://tracker.example/x.gif'>"
        "<ul><li>One</li><li>Two</li></ul>tail<br>end"
    )
    job = posting(descriptionPlain=DROP, description=markup)
    assert only_record(extract([job])).description_snippet == "HelloWorld Next One Two tail end"


def test_html_entities_are_decoded_once() -> None:
    job = posting(descriptionPlain=DROP, description="<p>R&amp;D &amp;amp; &lt;b&gt;</p>")
    assert only_record(extract([job])).description_snippet == "R&D &amp; <b>"


def test_plain_text_is_preferred_over_html() -> None:
    job = posting(descriptionPlain="Plain wins", description="<p>HTML loses</p>")
    assert only_record(extract([job])).description_snippet == "Plain wins"


def test_html_fixture_uses_its_plain_text() -> None:
    record = only_record(extract(load("postings_html_description.json")))
    assert record.description_snippet == "About us We build tools & services. - Remote friendly"


def test_plain_text_is_not_parsed_as_html() -> None:
    job = posting(descriptionPlain="Use <b>bold</b> & <script>x()</script> if a < b")
    snippet = only_record(extract([job])).description_snippet
    assert snippet == "Use <b>bold</b> & <script>x()</script> if a < b"


@pytest.mark.parametrize("plain", [DROP, None, "", "   \n "])
def test_blank_plain_text_falls_back_to_html(plain: Any) -> None:
    job = posting(descriptionPlain=plain, description="<p>From HTML</p>")
    assert only_record(extract([job])).description_snippet == "From HTML"


@pytest.mark.parametrize("value", [DROP, None, "", "<p> </p>", "<script>x()</script>"])
def test_no_usable_description_is_none(value: Any) -> None:
    job = posting(descriptionPlain=DROP, description=value)
    assert only_record(extract([job])).description_snippet is None


def test_control_characters_and_whitespace_are_cleaned() -> None:
    job = posting(descriptionPlain="a  b\x00c\x1bd\t\n\r\n e")
    assert only_record(extract([job])).description_snippet == "a bcd e"


def test_html_with_control_characters_is_cleaned() -> None:
    job = posting(descriptionPlain=DROP, description="<p>a\x00b  c</p>")
    assert only_record(extract([job])).description_snippet == "ab c"


def test_deeply_nested_html_does_not_overflow_the_stack() -> None:
    job = posting(descriptionPlain=DROP, description="<div>" * 20000 + "deep" + "</div>" * 20000)
    assert only_record(extract([job])).description_snippet == "deep"


def test_description_never_changes_the_company() -> None:
    job = posting(descriptionPlain="Join Globex Corporation (globex.example) today")
    record = only_record(extract([job]))
    assert record.company_name == "Example Corp"
    assert record.company_website is None


@pytest.mark.parametrize("field", ["descriptionPlain", "description"])
def test_wrong_type_description_fails(field: str) -> None:
    overrides = {"descriptionPlain": DROP, "description": DROP, field: {"x": 1}}
    failure = only_failure(extract([posting(**overrides)]))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, field)


def test_snippet_never_exceeds_limit() -> None:
    plain = " ".join(f"word{i}" for i in range(2000))
    snippet = only_record(extract([posting(descriptionPlain=plain)])).description_snippet
    assert snippet is not None and len(snippet) <= DESCRIPTION_SNIPPET_MAX


def test_snippet_limit_applies_to_html_too() -> None:
    markup = "<p>" + " ".join(f"word{i}" for i in range(2000)) + "</p>"
    job = posting(descriptionPlain=DROP, description=markup)
    snippet = only_record(extract([job])).description_snippet
    assert snippet is not None and len(snippet) <= DESCRIPTION_SNIPPET_MAX


def test_snippet_cuts_at_a_word_boundary() -> None:
    text = " ".join(["word"] * 300)
    snippet = only_record(extract([posting(descriptionPlain=text)])).description_snippet
    assert snippet is not None
    assert text.startswith(snippet) and text[len(snippet)] == " "
    assert snippet.endswith("word")


def test_snippet_does_not_split_a_word_straddling_the_limit() -> None:
    job = posting(descriptionPlain="x" * 499 + " yy zz")
    assert only_record(extract([job])).description_snippet == "x" * 499


def test_snippet_keeps_a_word_ending_exactly_at_the_limit() -> None:
    job = posting(descriptionPlain="x" * DESCRIPTION_SNIPPET_MAX + " tail")
    assert only_record(extract([job])).description_snippet == "x" * DESCRIPTION_SNIPPET_MAX


def test_an_overlong_single_word_is_cut_hard() -> None:
    job = posting(descriptionPlain="y" * 900)
    assert only_record(extract([job])).description_snippet == "y" * DESCRIPTION_SNIPPET_MAX


def test_snippet_counts_characters_not_bytes() -> None:
    snippet = only_record(extract([posting(descriptionPlain="\U0001f600 " * 400)]))
    assert snippet.description_snippet is not None
    assert len(snippet.description_snippet) <= DESCRIPTION_SNIPPET_MAX
    assert len(snippet.description_snippet.encode()) > DESCRIPTION_SNIPPET_MAX


# --- 16-18: posting dates. Lever's createdAt is deliberately NOT used -----------------------

VALID_CREATED_AT = [1791194400000, 1791194400000.0, epoch_ms(COLLECTED_AT)]
MALFORMED_CREATED_AT = [
    1791194400,  # seconds
    1791194400000000,  # microseconds
    -1,
    0,
    10**30,
    1791194400000.5,
    float("inf"),
    float("nan"),
    epoch_ms(COLLECTED_AT + timedelta(days=3)),  # in the future
]
WRONG_TYPE_CREATED_AT = ["1791194400000", "2026-10-05", "", True, False, [1791194400000], {"ms": 1}]


def assert_created_at_is_ignored(value: Any) -> None:
    """The job is extracted exactly as if createdAt were absent: no date, no warning, no failure."""
    result = extract([posting(createdAt=value)])
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    assert result.warnings == ()
    assert record == only_record(extract([posting(createdAt=DROP)]))


@pytest.mark.parametrize("value", VALID_CREATED_AT)
def test_valid_created_at_is_ignored(value: Any) -> None:
    assert_created_at_is_ignored(value)


@pytest.mark.parametrize("value", MALFORMED_CREATED_AT)
def test_malformed_created_at_is_ignored(value: Any) -> None:
    assert_created_at_is_ignored(value)


@pytest.mark.parametrize("value", WRONG_TYPE_CREATED_AT)
def test_wrong_type_created_at_is_ignored(value: Any) -> None:
    assert_created_at_is_ignored(value)


@pytest.mark.parametrize("value", [DROP, None])
def test_missing_created_at_is_accepted(value: Any) -> None:
    assert_created_at_is_ignored(value)


def test_missing_created_at_fixture_is_accepted() -> None:
    result = extract(load("postings_missing_posted_date.json"))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    assert result.warnings == ()


def test_lever_records_always_have_unknown_posted_dates() -> None:
    records = [r for name in ALL_FIXTURES for r in extract(load(name)).records]
    assert records  # the fixtures include postings with a valid createdAt
    assert {(r.posted_date, r.posted_date_source) for r in records} == {(None, "unknown")}


@pytest.mark.parametrize(
    "collected_at",
    [
        COLLECTED_AT,
        datetime(2030, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 5, 12, 0, tzinfo=UTC),  # the day the fixtures' createdAt falls on
        datetime(2026, 10, 20, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    ],
)
def test_collected_at_is_never_substituted_as_the_posting_date(collected_at: datetime) -> None:
    for payload in (
        load("postings_complete.json"),
        load("postings_missing_posted_date.json"),
        [posting(createdAt=epoch_ms(collected_at))],  # createdAt equal to the collection time
    ):
        record = only_record(extract(payload, collected_at=collected_at))
        assert (record.posted_date, record.posted_date_source) == (None, "unknown")
        assert record.collected_at == collected_at


@pytest.mark.parametrize(
    "reference",
    [
        COLLECTED_AT,
        datetime(2026, 10, 5, 12, 0, tzinfo=UTC),  # when a createdAt-based date would be eligible
        datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
        datetime(2027, 6, 1, 12, 0, tzinfo=UTC),
    ],
)
def test_phase1_date_window_is_unknown_date_for_every_lever_record(reference: datetime) -> None:
    records = [r for name in ALL_FIXTURES for r in extract(load(name)).records]
    assert records
    for record in records:
        window = check_date_window(record, reference)
        assert window.outcome is DateOutcome.UNKNOWN_DATE
        assert window.needs_review and not window.eligible


def test_created_at_is_not_in_the_extraction_path_at_all() -> None:
    """It is not parsed and discarded: the adapter code never refers to the field."""
    assert lever.__file__ is not None
    tree = ast.parse(Path(lever.__file__).read_text(encoding="utf-8"))
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "createdAt" not in strings
    assert not [name for name in names if "created" in name.lower()]


@pytest.mark.parametrize(
    ("overrides", "kind", "field"),
    [
        ({"text": None}, FailureKind.MISSING_FIELD, "text"),
        ({"applyUrl": DROP, "hostedUrl": DROP}, FailureKind.MISSING_FIELD, "applyUrl"),
        ({"applyUrl": "ftp://x", "hostedUrl": DROP}, FailureKind.INVALID_URL, "applyUrl"),
        ({"id": ["x"]}, FailureKind.INVALID_TYPE, "id"),
    ],
)
def test_required_field_errors_are_unchanged_whatever_createdAt_holds(
    overrides: dict[str, Any], kind: FailureKind, field: str
) -> None:
    for created_at in (1791194400000, "garbage", DROP):
        result = extract([posting(createdAt=created_at, **overrides)])
        failure = only_failure(result)
        assert (failure.kind, failure.errors[0].field) == (kind, field)
        assert result.warnings == ()


# --- 19-21: identity and configuration ------------------------------------------------------


def test_identity_is_stable_across_repeated_extraction_and_collection_times() -> None:
    first = only_record(extract(load("postings_complete.json")))
    later = only_record(
        extract(load("postings_complete.json"), collected_at=COLLECTED_AT + timedelta(days=3))
    )
    assert job_key(first) == job_key(later)


def test_identity_survives_a_changed_url_when_the_id_is_present() -> None:
    a = only_record(extract([posting(applyUrl="https://a.example.com/x")]))
    b = only_record(extract([posting(applyUrl="https://b.example.com/y")]))
    assert job_key(a) == job_key(b)


def test_identity_is_scoped_by_site_slug() -> None:
    payload = load("postings_complete.json")
    one = only_record(extract(payload, site_slug="site-one"))
    two = only_record(extract(payload, site_slug="site-two"))
    assert one.external_id == f"site-one:{FIRST_ID}"
    assert two.external_id == f"site-two:{FIRST_ID}"
    assert job_key(one) != job_key(two)  # same Lever id, different sites


def test_different_ids_on_one_site_have_different_identity() -> None:
    a, b = (extract([posting(id=f"id-{i}")]).records[0] for i in (1, 2))
    assert job_key(a) != job_key(b)


@pytest.mark.parametrize(
    ("raw_id", "canonical"),
    [
        (FIRST_ID, FIRST_ID),
        (FIRST_ID.upper(), FIRST_ID),
        (FIRST_ID.replace("-", ""), FIRST_ID),
        (f"  {FIRST_ID}  ", FIRST_ID),
        ("custom-id_7", "custom-id_7"),
    ],
)
def test_ids_are_canonicalized(raw_id: str, canonical: str) -> None:
    assert only_record(extract([posting(id=raw_id)])).external_id == f"{SITE}:{canonical}"


def test_one_id_in_two_spellings_is_one_identity() -> None:
    a = only_record(extract([posting(id=FIRST_ID)]))
    b = only_record(extract([posting(id=FIRST_ID.upper())]))
    assert job_key(a) == job_key(b)


def test_absent_id_falls_back_to_url_identity() -> None:
    record = only_record(extract([posting(id=DROP)]))
    assert record.external_id is None
    assert job_key(record) == f"url:{hashlib.sha256(_url_key(record).encode()).hexdigest()}"


def _url_key(record: JobRecord) -> str:
    return normalize_url(str(record.apply_url))


@pytest.mark.parametrize(
    "bad_id", [123, True, 1.5, ["a"], {"x": 1}, "", "  ", "has space", "a\x00b"]
)
def test_unusable_id_is_a_fatal_invalid_type(bad_id: Any) -> None:
    failure = only_failure(extract([posting(id=bad_id)]))
    assert (failure.kind, failure.job_id, failure.errors[0].field) == (
        FailureKind.INVALID_TYPE,
        None,
        "id",
    )


def test_company_name_comes_from_configuration_only() -> None:
    record = only_record(extract(load("postings_complete.json"), company_name="Configured Ltd"))
    assert record.company_name == "Configured Ltd"


def test_company_website_is_normalized_and_optional() -> None:
    payload = load("postings_complete.json")
    assert str(
        only_record(extract(payload, company_website="https://Example.com")).company_website
    ) == ("https://example.com/")
    assert only_record(extract(payload)).company_website is None


def test_source_url_is_recorded_as_requested() -> None:
    record = only_record(extract(load("postings_complete.json"), source_url=SOURCE_URL + "&skip=0"))
    assert str(record.source_url) == SOURCE_URL + "&skip=0"


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_name": ""},
        {"company_name": "   "},
        {"site_slug": ""},
        {"site_slug": "has space"},
        {"site_slug": "a:b"},
        {"site_slug": "a/b"},
        {"collected_at": datetime(2026, 10, 20, 12, 0)},
        {"source_url": "not a url"},
        {"source_url": "https://localhost/x"},
        {"company_website": "ftp://example.com"},
        {"company_website": "https://user:pw@example.com"},
    ],
)
def test_invalid_configuration_raises(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        extract(load("postings_complete.json"), **overrides)


def test_non_utc_collection_time_is_stored_as_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    moment = datetime(2026, 10, 20, 17, 30, tzinfo=ist)
    record = only_record(extract(load("postings_complete.json"), collected_at=moment))
    assert record.collected_at == COLLECTED_AT


def test_unknown_fields_are_ignored() -> None:
    job = posting(brandNew={"nested": [1, 2]}, salaryRange={"min": 1}, additional="<b>x</b>")
    payload = [job]
    assert only_record(extract(payload)) == only_record(extract([posting()]))


# --- 22: isolation and invalid responses ----------------------------------------------------


def test_one_invalid_posting_does_not_stop_the_others() -> None:
    jobs = [
        posting(id="a", applyUrl=url(1)),
        posting(id="b", text=None),
        posting(id="c", applyUrl=url(3)),
    ]
    result = extract(jobs)
    assert [r.external_id for r in result.records] == [f"{SITE}:a", f"{SITE}:c"]
    assert [(f.index, f.job_id, f.kind) for f in result.failures] == [
        (1, "b", FailureKind.MISSING_FIELD)
    ]
    assert result.job_count == len(result.records) + len(result.failures) == 3


def test_all_problems_of_a_posting_are_reported_together() -> None:
    job = posting(
        text="",
        applyUrl="ftp://x",
        hostedUrl=DROP,
        categories={"location": 5, "allLocations": "x"},
        workplaceType=9,
    )
    failure = only_failure(extract([job]))
    assert [(e.field, e.code) for e in failure.errors] == [
        ("text", "missing_field"),
        ("applyUrl", "invalid_url"),
        ("categories.location", "invalid_type"),
        ("categories.allLocations", "invalid_type"),
        ("workplaceType", "invalid_type"),
    ]
    assert failure.kind is FailureKind.MISSING_FIELD  # kind of the first problem


@pytest.mark.parametrize("cats", ["Pune", ["x"], 5])
def test_wrong_type_categories_fails(cats: Any) -> None:
    failure = only_failure(extract([posting(categories=cats)]))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, "categories")


def test_non_string_all_locations_entry_fails() -> None:
    failure = only_failure(extract([posting(categories=categories(allLocations=["A", 5]))]))
    assert failure.errors[0].field == "categories.allLocations"


@pytest.mark.parametrize("item", [None, "posting", 7, ["a"], True])
def test_non_object_posting_is_isolated(item: Any) -> None:
    result = extract([item, posting()])
    assert len(result.records) == 1
    failure = result.failures[0]
    assert (failure.kind, failure.index, failure.job_id) == (FailureKind.INVALID_TYPE, 0, None)


@pytest.mark.parametrize(
    "payload",
    [None, {}, "text", 5, {"ok": False, "error": "Site not found"}, {"postings": []}, (1, 2)],
)
def test_response_that_is_not_a_list_is_an_invalid_response(payload: Any) -> None:
    result = extract(payload)
    failure = only_failure(result)
    assert failure.kind is FailureKind.INVALID_RESPONSE and failure.index is None
    assert result == LeverExtraction((), result.failures, None, 0)
    assert "Site not found" not in repr(failure)


def test_mixed_valid_and_invalid_postings_stay_isolated() -> None:
    jobs = [
        posting(id="a", applyUrl=url(1), createdAt=1791194400000),
        posting(id="b", applyUrl=url(2), createdAt="garbage"),
        posting(id="c", applyUrl=url(3), text=None),
        posting(id="d", applyUrl="nope", hostedUrl=url(4)),
        None,
        posting(id="e", applyUrl=url(5)),
    ]
    result = extract(jobs)
    assert [r.external_id for r in result.records] == [f"{SITE}:{c}" for c in "abde"]
    assert [(f.index, f.job_id) for f in result.failures] == [(2, "c"), (4, None)]
    assert [(w.index, w.job_id, w.field) for w in result.warnings] == [(3, "d", "applyUrl")]
    assert all(r.posted_date is None for r in result.records)
    assert result.job_count == len(result.records) + len(result.failures) == 6


# --- 28-29: warnings and message safety -----------------------------------------------------


def test_warning_for_a_failed_posting_is_still_reported() -> None:
    result = extract([posting(text=None, applyUrl="nope", hostedUrl=url(1))])
    assert only_failure(result).errors[0].field == "text"
    assert [w.field for w in result.warnings] == ["applyUrl"]


def test_every_fixture_extracts_without_warnings() -> None:
    assert all(extract(load(name)).warnings == () for name in ALL_FIXTURES)


def test_messages_never_expose_source_values_or_html() -> None:
    marker = "SECRET-MARKER-<script>alert(1)</script>"
    bad = [
        posting(text=[marker]),
        posting(applyUrl=f"ftp://{marker}", hostedUrl=DROP),
        posting(categories={"location": [marker]}),
        posting(categories={"commitment": {"x": marker}}),
        posting(workplaceType=[marker]),
        posting(descriptionPlain={"x": marker}),
        posting(id=[marker]),
        posting(id=f"has space {marker}"),
        marker,
    ]
    warned = [
        posting(applyUrl=f"https://{marker}@x.example", hostedUrl=url(1)),
        posting(applyUrl={"x": marker}, hostedUrl=url(1)),
    ]
    ignored = [
        posting(createdAt=marker),
        posting(createdAt=[marker]),
        posting(createdAt={"x": marker}),
    ]
    result = extract(bad + warned + ignored)
    assert len(result.failures) == len(bad) and len(result.warnings) == len(warned)
    assert len(result.records) == len(warned) + len(ignored)
    text = repr(result.failures) + repr(result.warnings)
    assert "SECRET-MARKER" not in text and "<script>" not in text


def test_invalid_response_message_names_the_type_only() -> None:
    failure = only_failure(extract({"error": "SECRET-MARKER"}))
    assert "SECRET-MARKER" not in repr(failure)


# --- 23-26: determinism, no mutation, no side effects ---------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_output_is_deterministic(name: str) -> None:
    assert extract(load(name)) == extract(load(name))


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_input_is_not_mutated(name: str) -> None:
    payload = load(name)
    snapshot = copy.deepcopy(payload)
    extract(payload)
    assert payload == snapshot


def test_input_with_failures_and_warnings_is_not_mutated() -> None:
    payload = [posting(text=None), posting(createdAt="x", applyUrl="y"), None, posting(id=5)]
    snapshot = copy.deepcopy(payload)
    extract(payload)
    assert payload == snapshot


def test_no_network_access(no_network: None) -> None:
    for name in ALL_FIXTURES:
        extract(load(name))


@pytest.mark.parametrize("module", [common, greenhouse, lever], ids=lambda m: m.__name__)
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


# --- 27, 30: Phase 1 and fixture compatibility ----------------------------------------------


@pytest.mark.parametrize(
    "name", [n for n in ALL_FIXTURES if n != "postings_invalid_incomplete.json"]
)
def test_records_round_trip_through_phase1_validation(name: str) -> None:
    for record in extract(load(name)).records:
        revalidated = validate_job(record.model_dump())
        assert not isinstance(revalidated, Rejection), revalidated
        assert revalidated == record


def test_records_work_with_phase1_dedupe() -> None:
    twin = posting(applyUrl=f"https://jobs.lever.co/{SITE}/{FIRST_ID}/apply?utm_source=x")
    other = posting(id="00000000-0000-4000-8000-000000000099", applyUrl=url(99))
    result = dedupe(extract([posting(), twin, other]).records)
    assert (len(result.accepted), len(result.duplicates)) == (2, 1)


def test_records_work_with_phase1_date_window_and_fingerprint() -> None:
    with_created_at = only_record(extract(load("postings_complete.json")))
    without = only_record(extract(load("postings_missing_posted_date.json")))
    for record in (with_created_at, without):
        assert check_date_window(record, COLLECTED_AT).outcome is DateOutcome.UNKNOWN_DATE
    assert content_fingerprint(with_created_at.model_dump()) == content_fingerprint(
        without.model_dump()
    )


def test_every_fixture_job_is_extracted_or_reported() -> None:
    manifest = json.loads((FIXTURES.parent / "manifest.json").read_text(encoding="utf-8"))
    expected = {
        e["path"].split("/", 1)[1]: e["expected_listing_count"]
        for e in manifest["fixtures"]
        if e["platform"] == "lever"
    }
    assert set(expected) == set(ALL_FIXTURES)
    for name, count in expected.items():
        result = extract(load(name))
        assert result.job_count == len(result.records) + len(result.failures) == count, name


def test_invalid_fixture_failures_match_the_manifest() -> None:
    manifest = json.loads((FIXTURES.parent / "manifest.json").read_text(encoding="utf-8"))
    entry = next(
        e for e in manifest["fixtures"] if e["path"].endswith("postings_invalid_incomplete.json")
    )
    result = extract(load("postings_invalid_incomplete.json"))
    assert entry["absent_fields_per_item"] == [["text"], ["hostedUrl", "applyUrl"]]
    assert [f.errors[0].field for f in result.failures] == ["text", "applyUrl"]
    assert not result.records
