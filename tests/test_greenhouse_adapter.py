"""Greenhouse adapter tests. Offline: synthetic fixtures and in-memory payloads only."""

import ast
import copy
import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from collector.adapters import greenhouse
from collector.adapters.greenhouse import (
    DESCRIPTION_SNIPPET_MAX,
    ExtractionFailure,
    ExtractionWarning,
    FailureKind,
    GreenhouseExtraction,
    extract_greenhouse_jobs,
)
from collector.models import JobRecord
from collector.pipelines.date_window import DateOutcome, check_date_window
from collector.pipelines.dedupe import dedupe
from collector.pipelines.validate import Rejection, validate_job
from collector.utils.fingerprint import content_fingerprint
from collector.utils.identity import job_key
from collector.utils.urls import normalize_url

FIXTURES = Path(__file__).parent / "fixtures" / "greenhouse"
BOARD = "example-corp-fixture"
SOURCE_URL = f"https://boards-api.greenhouse.io/v1/boards/{BOARD}/jobs"
COLLECTED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ALL_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.json"))


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def extract(payload: Any, **overrides: Any) -> GreenhouseExtraction:
    kwargs: dict[str, Any] = {
        "company_name": "Example Corp",
        "board_token": BOARD,
        "source_url": SOURCE_URL,
        "collected_at": COLLECTED_AT,
    }
    kwargs.update(overrides)
    return extract_greenhouse_jobs(payload, **kwargs)


def base_job(**overrides: Any) -> dict[str, Any]:
    """A complete listing job taken from the synthetic fixture, with fields replaced/removed
    (pass ``None`` via ``DROP`` to delete a key)."""
    job = copy.deepcopy(load("list_complete.json")["jobs"][0])
    for key, value in overrides.items():
        if value is DROP:
            job.pop(key, None)
        else:
            job[key] = value
    return job


DROP = object()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def listing(*jobs: dict[str, Any]) -> dict[str, Any]:
    return {"jobs": list(jobs), "meta": {"total": len(jobs)}}


def only_record(result: GreenhouseExtraction) -> JobRecord:
    assert len(result.records) == 1, result.failures
    assert not result.failures
    return result.records[0]


def only_failure(result: GreenhouseExtraction) -> ExtractionFailure:
    assert len(result.failures) == 1 and not result.records, result.records
    return result.failures[0]


def with_content(html_text: str) -> dict[str, Any]:
    return listing(base_job(content=html_text))


# --- 1-4: response shapes -------------------------------------------------------------------


def test_complete_listing_extraction() -> None:
    result = extract(load("list_complete.json"))
    assert result.response_type == "listing"
    assert result.job_count == 1
    record = only_record(result)
    assert record.title == "Senior Backend Engineer"
    assert record.company_name == "Example Corp"
    assert str(record.apply_url) == f"https://boards.greenhouse.io/{BOARD}/jobs/8800000001"
    assert str(record.source_url) == SOURCE_URL
    assert record.platform == "greenhouse"
    assert record.collected_at == COLLECTED_AT
    assert record.location == "Bengaluru, India"
    assert record.external_id == f"{BOARD}:8800000001"
    assert record.description_snippet == "Build and operate backend services."
    assert record.employment_type is None
    assert record.salary_text is None


def test_single_job_detail_extraction() -> None:
    result = extract(load("detail_complete.json"))
    assert result.response_type == "detail"
    assert result.job_count == 1
    record = only_record(result)
    assert record.title == "Senior Backend Engineer"
    assert record.external_id == f"{BOARD}:8800000001"
    assert record.posted_date == date(2026, 10, 1)
    assert record.posted_date_source == "platform"


def test_multiple_jobs_keep_source_order() -> None:
    jobs = [
        base_job(id=3, title="C", absolute_url="https://boards.greenhouse.io/x/jobs/3"),
        base_job(id=1, title="A", absolute_url="https://boards.greenhouse.io/x/jobs/1"),
        base_job(id=2, title="B", absolute_url="https://boards.greenhouse.io/x/jobs/2"),
    ]
    result = extract(listing(*jobs))
    assert [r.title for r in result.records] == ["C", "A", "B"]
    assert result.job_count == 3


def test_empty_listing() -> None:
    result = extract(load("list_empty.json"))
    assert result == GreenhouseExtraction((), (), "listing", 0)


# --- 5-7: required fields and URLs ----------------------------------------------------------


@pytest.mark.parametrize("title", [DROP, None, "", "   \t"])
def test_missing_or_blank_title_fails(title: Any) -> None:
    failure = only_failure(extract(listing(base_job(title=title))))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "title"


def test_fixture_job_without_title_is_a_missing_field_failure() -> None:
    result = extract(load("list_invalid_incomplete.json"))
    first = result.failures[0]
    assert (first.kind, first.index, first.job_id) == (FailureKind.MISSING_FIELD, 0, "8800000006")
    assert first.errors[0].field == "title"


@pytest.mark.parametrize("url", [DROP, None, "", "  "])
def test_missing_application_url_fails(url: Any) -> None:
    failure = only_failure(extract(listing(base_job(absolute_url=url))))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "absolute_url"


def test_fixture_job_without_application_url_is_a_missing_field_failure() -> None:
    second = extract(load("list_invalid_incomplete.json")).failures[1]
    assert (second.kind, second.index, second.job_id) == (
        FailureKind.MISSING_FIELD,
        1,
        "8800000007",
    )
    assert second.errors[0].field == "absolute_url"


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "/relative/path",
        "//boards.greenhouse.io/x/jobs/1",
        "ftp://boards.greenhouse.io/x/jobs/1",
        "javascript:alert(1)",
        "https://",
        "https://user:pass@boards.greenhouse.io/x/jobs/1",
        "https://localhost/jobs/1",
        "http://127.0.0.1/jobs/1",
        "http://10.0.0.5/jobs/1",
        "http://[::1]/jobs/1",
        "https://intranet/jobs/1",
        "https://boards.greenhouse.io:notaport/jobs/1",
    ],
)
def test_invalid_or_non_public_url_fails(url: str) -> None:
    failure = only_failure(extract(listing(base_job(absolute_url=url))))
    assert failure.kind is FailureKind.INVALID_URL
    assert failure.errors[0].field == "absolute_url"


def test_url_longer_than_the_model_allows_is_caught_by_phase1_validation() -> None:
    long_url = "https://boards.greenhouse.io/x/jobs/1?q=" + "a" * 2100
    failure = only_failure(extract(listing(base_job(absolute_url=long_url))))
    assert failure.kind is FailureKind.INVALID_URL
    assert long_url not in repr(failure)


def test_apply_url_is_normalized_and_gh_jid_is_kept() -> None:
    messy_url = " HTTPS://Careers.Example.com:443/open-roles/?utm_source=x&gh_jid=1#top "
    record = only_record(extract(listing(base_job(absolute_url=messy_url))))
    assert str(record.apply_url) == "https://careers.example.com/open-roles/?gh_jid=1"


def test_multiple_locations_fixture_keeps_gh_jid() -> None:
    record = only_record(extract(load("list_multiple_locations.json")))
    assert str(record.apply_url) == "https://careers.example.com/open-roles/?gh_jid=8800000003"


def test_no_url_is_constructed_from_board_token_and_id() -> None:
    failure = only_failure(extract(listing(base_job(absolute_url=DROP))))
    assert failure.kind is FailureKind.MISSING_FIELD  # not repaired from board token + id


# --- 8-9: optional fields and locations -----------------------------------------------------


def test_missing_optional_fields_stay_none() -> None:
    record = only_record(extract(load("list_missing_optional_fields.json")))
    assert record.location is None  # location.name is ""
    assert record.work_type is None
    assert record.employment_type is None
    assert record.salary_text is None
    assert record.posted_date is None
    assert record.posted_date_source == "unknown"
    assert record.company_website is None


@pytest.mark.parametrize("location", [DROP, None, {}, {"name": None}, {"name": "  "}])
def test_absent_or_blank_location_is_none(location: Any) -> None:
    assert only_record(extract(listing(base_job(location=location)))).location is None


def test_multiple_locations_are_kept_in_source_order_unsplit() -> None:
    record = only_record(extract(load("list_multiple_locations.json")))
    assert record.location == "Bengaluru, India; Pune, India"


def test_offices_are_not_used_to_invent_a_location() -> None:
    job = base_job(location={"name": ""})  # offices still lists Bengaluru
    assert job["offices"]
    assert only_record(extract(listing(job))).location is None


def test_title_and_location_whitespace_is_collapsed() -> None:
    record = only_record(
        extract(listing(base_job(title="  Senior \n Engineer ", location={"name": "A,\t  B"})))
    )
    assert (record.title, record.location) == ("Senior Engineer", "A, B")


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Remote", "remote"),
        ("remote", "remote"),
        ("Remote - India", "remote"),
        ("Remote (EMEA)", "remote"),
        ("Remote, Spain", "remote"),
        ("Remote - India; Remote - Spain", "remote"),
        ("Bengaluru, India", None),
        ("Remote-first culture, Pune", None),
        ("Remotely located", None),
        ("Remote - India; Pune, India", None),
        ("Hybrid - Pune", None),
        ("Remote or on-site", None),
        ("Remote - hybrid", None),
    ],
)
def test_remote_only_when_explicitly_identified(location: str, expected: str | None) -> None:
    record = only_record(extract(listing(base_job(location={"name": location}))))
    assert record.work_type == expected


def test_remote_fixture() -> None:
    record = only_record(extract(load("list_remote.json")))
    assert (record.location, record.work_type) == ("Remote - India", "remote")


# --- 10-12: descriptions --------------------------------------------------------------------


def test_html_description_is_decoded_and_converted_to_text() -> None:
    record = only_record(extract(load("list_html_description.json")))
    assert record.description_snippet == (
        "About the role R&D team — you will own services. Python SQL"
    )


def test_description_drops_scripts_styles_comments_and_keeps_words_apart() -> None:
    content = (
        "<p>Hello<b>World</b></p><script>steal()</script><style>p{x:y}</style>"
        "<!-- hidden --><div>Next</div><img src='https://tracker.example/x.gif'>"
        "<ul><li>One</li><li>Two</li></ul>tail<br>end"
    )
    record = only_record(extract(with_content(content)))
    assert record.description_snippet == "HelloWorld Next One Two tail end"


def test_encoded_scripts_are_dropped_too() -> None:
    encoded = "&lt;p&gt;Safe&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;"
    assert only_record(extract(with_content(encoded))).description_snippet == "Safe"


def test_content_with_literal_tags_is_not_double_decoded() -> None:
    record = only_record(extract(with_content("<p>R&amp;amp;D &amp;lt;b&amp;gt;</p>")))
    assert record.description_snippet == "R&amp;D &lt;b&gt;"


@pytest.mark.parametrize("content", [DROP, None, "", "   ", "<p> </p>", "<script>x()</script>"])
def test_empty_description_is_none(content: Any) -> None:
    assert only_record(extract(listing(base_job(content=content)))).description_snippet is None


def test_unicode_whitespace_and_control_characters_are_normalized() -> None:
    record = only_record(extract(with_content("<p>a  b\x00c​d\t\n e</p>")))
    assert record.description_snippet == "a bc​d e"


def test_deeply_nested_html_does_not_overflow_the_stack() -> None:
    content = "<div>" * 20000 + "deep" + "</div>" * 20000
    record = only_record(extract(with_content(content)))
    assert record.description_snippet == "deep"


def test_snippet_never_exceeds_limit() -> None:
    content = "<p>" + " ".join(f"word{i}" for i in range(2000)) + "</p>"
    snippet = only_record(extract(with_content(content))).description_snippet
    assert snippet is not None and len(snippet) <= DESCRIPTION_SNIPPET_MAX


def test_snippet_counts_unicode_characters_not_bytes() -> None:
    snippet = only_record(extract(with_content("\U0001f600 " * 400))).description_snippet
    assert snippet is not None
    assert len(snippet) <= DESCRIPTION_SNIPPET_MAX
    assert len(snippet.encode("utf-8")) > DESCRIPTION_SNIPPET_MAX  # many bytes, still <= 500 chars


def test_short_description_is_not_truncated() -> None:
    text = "x" * DESCRIPTION_SNIPPET_MAX
    assert only_record(extract(with_content(text))).description_snippet == text


def test_snippet_cuts_at_a_word_boundary() -> None:
    text = " ".join(["word"] * 300)
    snippet = only_record(extract(with_content(text))).description_snippet
    assert snippet is not None
    assert len(snippet) <= DESCRIPTION_SNIPPET_MAX
    assert text.startswith(snippet)
    assert text[len(snippet)] == " "  # the cut is between words
    assert snippet.endswith("word") and not snippet.endswith(" ")


def test_snippet_does_not_split_a_word_that_straddles_the_limit() -> None:
    text = "x" * 499 + " yy zz"
    assert only_record(extract(with_content(text))).description_snippet == "x" * 499


def test_snippet_keeps_a_word_ending_exactly_at_the_limit() -> None:
    text = "x" * DESCRIPTION_SNIPPET_MAX + " tail"
    assert only_record(extract(with_content(text))).description_snippet == "x" * 500


def test_a_single_overlong_word_is_cut_hard() -> None:
    snippet = only_record(extract(with_content("y" * 900))).description_snippet
    assert snippet == "y" * DESCRIPTION_SNIPPET_MAX


# --- 13-15: posting dates -------------------------------------------------------------------


def test_missing_posting_date_stays_unknown() -> None:
    record = only_record(extract(load("detail_missing_posted_date.json")))
    assert record.posted_date is None
    assert record.posted_date_source == "unknown"


def test_first_published_is_the_posting_date() -> None:
    record = only_record(extract(listing(base_job(first_published="2026-10-01T13:00:00Z"))))
    assert record.posted_date == date(2026, 10, 1)
    assert record.posted_date_source == "platform"


def test_updated_at_is_never_the_posting_date() -> None:
    # list fixtures only carry updated_at
    for name in ("list_complete.json", "detail_missing_posted_date.json"):
        payload = load(name)
        job = payload["jobs"][0] if "jobs" in payload else payload
        assert "updated_at" in job
        assert only_record(extract(payload)).posted_date is None
    # and when both exist, first_published wins
    both = base_job(first_published="2026-09-01T00:00:00Z", updated_at="2026-10-12T09:30:00-04:00")
    assert only_record(extract(listing(both))).posted_date == date(2026, 9, 1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-01T13:00:00Z", date(2026, 10, 1)),
        ("2026-10-01T23:30:00Z", date(2026, 10, 1)),  # not shifted to the business timezone
        ("2026-10-01T23:30:00-05:00", date(2026, 10, 1)),  # date as written, offset not applied
        ("2026-10-02T01:00:00+05:30", date(2026, 10, 2)),
        ("2026-10-01T13:00:00.123Z", date(2026, 10, 1)),
        ("2026-10-01", date(2026, 10, 1)),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_first_published_formats(value: str | None, expected: date | None) -> None:
    record = only_record(extract(listing(base_job(first_published=value))))
    assert record.posted_date == expected
    assert record.posted_date_source == ("platform" if expected else "unknown")


@pytest.mark.parametrize(
    "value",
    [
        "yesterday",
        "3 days ago",
        "12 Oct 2026",
        "10/12/2026",
        "2026-13-45",
        "2026-10-01T25:00Z",
        "x",
    ],
)
def test_malformed_first_published_keeps_the_job_with_an_unknown_date_and_warns(
    value: str,
) -> None:
    result = extract(listing(base_job(first_published=value)))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    (warning,) = result.warnings
    assert warning == ExtractionWarning(
        kind=FailureKind.INVALID_DATE,
        index=0,
        job_id="8800000001",
        field="first_published",
        message=warning.message,
    )
    assert value not in warning.message


# --- recoverable posting date: the job is kept, the problem is a warning --------------------


def test_wrong_type_first_published_keeps_the_job_with_an_unknown_date_and_warns() -> None:
    for value in (20261001, 1.5, True, ["2026-10-01"], {"date": "2026-10-01"}):
        result = extract(listing(base_job(first_published=value)))
        record = only_record(result)
        assert (record.posted_date, record.posted_date_source) == (None, "unknown")
        (warning,) = result.warnings
        assert (warning.kind, warning.index, warning.job_id, warning.field) == (
            FailureKind.INVALID_TYPE,
            0,
            "8800000001",
            "first_published",
        )
        assert type(value).__name__ in warning.message


@pytest.mark.parametrize("value", [DROP, None, "", "   "])
def test_missing_date_keeps_the_job_without_any_warning(value: Any) -> None:
    result = extract(listing(base_job(first_published=value)))
    assert only_record(result).posted_date is None
    assert result.warnings == ()


@pytest.mark.parametrize(
    "value", ["2026-10-01T13:00:00Z", "2026-10-01T23:30:00-05:00", "2026-10-01"]
)
def test_valid_date_produces_no_warning(value: str) -> None:
    result = extract(listing(base_job(first_published=value)))
    assert only_record(result).posted_date == date(2026, 10, 1)
    assert result.warnings == ()


def test_every_fixture_extracts_without_warnings() -> None:
    assert all(extract(load(name)).warnings == () for name in ALL_FIXTURES)


def test_warning_for_a_detail_response_has_no_index_but_has_the_job_id() -> None:
    detail = load("detail_complete.json")
    detail["first_published"] = "not a date"
    result = extract(detail)
    assert only_record(result).posted_date is None
    (warning,) = result.warnings
    assert (warning.kind, warning.index, warning.job_id) == (
        FailureKind.INVALID_DATE,
        None,
        "8800000001",
    )


def test_recovered_job_never_borrows_updated_at_or_the_collection_date() -> None:
    job = base_job(first_published="garbage", updated_at="2026-10-12T09:30:00-04:00")
    record = only_record(extract(listing(job)))
    assert record.posted_date is None
    assert record.posted_date_source == "unknown"
    wrong_type = base_job(first_published=12345, updated_at="2026-10-12T09:30:00-04:00")
    assert only_record(extract(listing(wrong_type))).posted_date is None


@pytest.mark.parametrize("value", ["garbage", 12345, ["2026-10-01"]])
def test_recovered_jobs_are_unknown_date_for_the_date_window(value: Any) -> None:
    record = only_record(extract(listing(base_job(first_published=value))))
    assert check_date_window(record, COLLECTED_AT).outcome is DateOutcome.UNKNOWN_DATE


@pytest.mark.parametrize("value", ["garbage", 12345])
def test_recovery_changes_nothing_else_about_the_record(value: Any) -> None:
    recovered = only_record(extract(listing(base_job(first_published=value))))
    without_date = only_record(extract(listing(base_job(first_published=DROP))))
    assert recovered == without_date


@pytest.mark.parametrize(
    ("overrides", "kind", "field"),
    [
        ({"title": None}, FailureKind.MISSING_FIELD, "title"),
        ({"absolute_url": None}, FailureKind.MISSING_FIELD, "absolute_url"),
        ({"absolute_url": "ftp://x"}, FailureKind.INVALID_URL, "absolute_url"),
        ({"id": "abc"}, FailureKind.INVALID_TYPE, "id"),
    ],
)
def test_fatal_errors_still_reject_the_job_even_when_the_date_is_also_bad(
    overrides: dict[str, Any], kind: FailureKind, field: str
) -> None:
    result = extract(listing(base_job(first_published="garbage", **overrides)))
    failure = only_failure(result)
    assert (failure.kind, failure.errors[0].field) == (kind, field)
    assert all(e.field != "first_published" for e in failure.errors)  # not a failure reason
    assert [w.field for w in result.warnings] == ["first_published"]  # but still reported


def test_warnings_never_expose_source_payloads() -> None:
    marker = "SECRET-MARKER-<script>alert(1)</script>"
    jobs = [
        base_job(first_published=marker),
        base_job(first_published=[marker]),
        base_job(first_published={"x": marker}),
    ]
    result = extract(listing(*jobs))
    assert len(result.records) == 3 and len(result.warnings) == 3
    assert "SECRET-MARKER" not in repr(result.warnings)
    assert "<script>" not in repr(result.warnings)


def test_mixed_valid_and_invalid_jobs_stay_isolated() -> None:
    def url(n: int) -> str:
        return f"https://boards.greenhouse.io/x/jobs/{n}"

    jobs = [
        base_job(id=1, absolute_url=url(1), first_published="2026-10-01T13:00:00Z"),
        base_job(id=2, absolute_url=url(2), first_published="garbage"),
        base_job(id=3, absolute_url=url(3), title=None),
        base_job(id=4, absolute_url=url(4), first_published=4),
        base_job(id=5, absolute_url=url(5)),
    ]
    result = extract(listing(*jobs))
    assert [r.external_id for r in result.records] == [f"{BOARD}:{n}" for n in (1, 2, 4, 5)]
    assert [r.posted_date for r in result.records] == [date(2026, 10, 1), None, None, None]
    assert [(f.index, f.job_id, f.kind) for f in result.failures] == [
        (2, "3", FailureKind.MISSING_FIELD)
    ]
    assert [(w.index, w.job_id, w.kind) for w in result.warnings] == [
        (1, "2", FailureKind.INVALID_DATE),
        (3, "4", FailureKind.INVALID_TYPE),
    ]
    assert result.job_count == len(result.records) + len(result.failures) == 5


# --- 16-17: identity ------------------------------------------------------------------------


def test_identity_is_stable_across_repeated_extraction_and_collection_times() -> None:
    first = only_record(extract(load("list_complete.json")))
    later = only_record(
        extract(load("list_complete.json"), collected_at=COLLECTED_AT + timedelta(days=3))
    )
    assert job_key(first) == job_key(later)


def test_identity_is_scoped_by_board_token() -> None:
    payload = load("list_complete.json")
    one = only_record(extract(payload, board_token="board-one"))
    two = only_record(extract(payload, board_token="board-two"))
    assert one.external_id == "board-one:8800000001"
    assert two.external_id == "board-two:8800000001"
    assert job_key(one) != job_key(two)  # same Greenhouse id, different boards


def test_same_board_different_ids_have_different_identity() -> None:
    a, b = (extract(listing(base_job(id=i))).records[0] for i in (1, 2))
    assert job_key(a) != job_key(b)


def test_identity_survives_a_changed_url_when_the_id_is_present() -> None:
    a = only_record(
        extract(listing(base_job(absolute_url="https://a.example.com/jobs/1?gh_jid=1")))
    )
    b = only_record(
        extract(listing(base_job(absolute_url="https://b.example.com/careers/?gh_jid=1")))
    )
    assert job_key(a) == job_key(b)


@pytest.mark.parametrize(("raw_id", "text"), [(8800000001, "8800000001"), ("0042", "42"), (7, "7")])
def test_id_forms_are_canonicalized(raw_id: Any, text: str) -> None:
    assert only_record(extract(listing(base_job(id=raw_id)))).external_id == f"{BOARD}:{text}"


def test_absent_id_falls_back_to_url_identity() -> None:
    record = only_record(extract(listing(base_job(id=DROP))))
    assert record.external_id is None
    assert job_key(record) == f"url:{_sha256(normalize_url(str(record.apply_url)))}"


@pytest.mark.parametrize("bad_id", [True, 1.5, "abc", "", [1], {"x": 1}, -3])
def test_unusable_id_is_an_invalid_type_failure(bad_id: Any) -> None:
    failure = only_failure(extract(listing(base_job(id=bad_id))))
    assert failure.kind is FailureKind.INVALID_TYPE
    assert failure.job_id is None
    assert failure.errors[0].field == "id"


# --- 18-19: configuration and unknown fields ------------------------------------------------


def test_company_name_comes_from_configuration_only() -> None:
    detail = load("detail_complete.json")
    assert detail["company_name"] == "Example Corp"
    record = only_record(extract(detail, company_name="Configured Name Ltd"))
    assert record.company_name == "Configured Name Ltd"


def test_company_name_is_not_inferred_from_description() -> None:
    job = base_job(content="&lt;p&gt;Join Globex Corporation today&lt;/p&gt;")
    assert only_record(extract(listing(job))).company_name == "Example Corp"


def test_company_website_from_configuration_is_normalized() -> None:
    record = only_record(extract(load("list_complete.json"), company_website="https://Example.com"))
    assert str(record.company_website) == "https://example.com/"
    assert only_record(extract(load("list_complete.json"))).company_website is None


def test_source_url_is_recorded_as_given() -> None:
    record = only_record(
        extract(load("list_complete.json"), source_url=SOURCE_URL + "?content=true")
    )
    assert str(record.source_url) == SOURCE_URL + "?content=true"


def test_unknown_fields_are_ignored() -> None:
    job = base_job(
        brand_new_field={"nested": [1, 2, 3]},
        pay_input_ranges=[{"min_cents": 1}],
        questions="not used",
    )
    payload = {**listing(job), "pagination": {"next": "x"}, "extra": 1}
    assert only_record(extract(payload)) == only_record(extract(listing(base_job())))


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_name": ""},
        {"company_name": "   "},
        {"board_token": ""},
        {"board_token": "has space"},
        {"board_token": "a:b"},
        {"board_token": "a/b"},
        {"collected_at": datetime(2026, 10, 20, 12, 0)},
        {"source_url": "not a url"},
        {"source_url": "https://localhost/jobs"},
        {"company_website": "ftp://example.com"},
    ],
)
def test_invalid_configuration_raises(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        extract(load("list_complete.json"), **overrides)


def test_non_utc_collection_time_is_accepted_and_stored_as_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    record = only_record(
        extract(load("list_complete.json"), collected_at=datetime(2026, 10, 20, 17, 30, tzinfo=ist))
    )
    assert record.collected_at == datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


# --- 20: error isolation --------------------------------------------------------------------


def test_one_invalid_job_does_not_stop_the_others_and_is_reported() -> None:
    good_a = base_job(id=1, absolute_url="https://boards.greenhouse.io/x/jobs/1")
    bad = base_job(id=2, title=None)
    good_b = base_job(id=3, absolute_url="https://boards.greenhouse.io/x/jobs/3")
    result = extract(listing(good_a, bad, good_b))
    assert [r.external_id for r in result.records] == [f"{BOARD}:1", f"{BOARD}:3"]
    assert [(f.index, f.job_id, f.kind) for f in result.failures] == [
        (1, "2", FailureKind.MISSING_FIELD)
    ]
    assert result.job_count == len(result.records) + len(result.failures) == 3


def test_all_problems_of_a_job_are_reported_together() -> None:
    job = base_job(title="", absolute_url="ftp://x", first_published="nope", location="flat string")
    failure = only_failure(extract(listing(job)))
    assert [(e.field, e.code) for e in failure.errors] == [
        ("title", "missing_field"),
        ("absolute_url", "invalid_url"),
        ("location", "invalid_type"),
    ]
    assert failure.kind is FailureKind.MISSING_FIELD  # kind of the first problem


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"title": 123}, "title"),
        ({"absolute_url": ["x"]}, "absolute_url"),
        ({"location": "Pune"}, "location"),
        ({"location": {"name": 5}}, "location.name"),
        ({"content": {"html": "x"}}, "content"),
    ],
)
def test_unexpected_field_types_are_invalid_type_failures(
    overrides: dict[str, Any], field: str
) -> None:
    failure = only_failure(extract(listing(base_job(**overrides))))
    assert failure.kind is FailureKind.INVALID_TYPE
    assert failure.errors[0].field == field


@pytest.mark.parametrize("item", [None, "job", 7, ["a"], True])
def test_non_object_job_is_isolated(item: Any) -> None:
    result = extract(listing(item, base_job()))  # type: ignore[arg-type]
    assert len(result.records) == 1
    failure = result.failures[0]
    assert (failure.kind, failure.index, failure.job_id) == (FailureKind.INVALID_TYPE, 0, None)


@pytest.mark.parametrize(
    "payload",
    [None, [], [{"id": 1}], "text", 5, {}, {"error": "Job not found", "status": 404}],
)
def test_unrecognized_response_is_an_invalid_response_failure(payload: Any) -> None:
    result = extract(payload)
    assert result == GreenhouseExtraction((), result.failures, None, 0)
    failure = only_failure(result)
    assert failure.kind is FailureKind.INVALID_RESPONSE
    assert failure.index is None


@pytest.mark.parametrize("jobs", [None, {}, "x", 3])
def test_jobs_that_is_not_a_list_is_an_invalid_response(jobs: Any) -> None:
    failure = only_failure(extract({"jobs": jobs}))
    assert failure.kind is FailureKind.INVALID_RESPONSE


def test_detail_without_required_fields_is_a_job_failure_not_an_invalid_response() -> None:
    failure = only_failure(extract({"id": 5}))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.job_id == "5"


def test_failures_never_echo_source_values_or_html() -> None:
    marker = "SECRET-MARKER-<script>alert(1)</script>"
    jobs = [
        base_job(title=[marker]),
        base_job(absolute_url=f"ftp://{marker}"),
        base_job(location={"name": [marker]}),
        base_job(content={"x": marker}),
        base_job(id=marker),
    ]
    result = extract(listing(*jobs))
    assert len(result.failures) == len(jobs)
    assert "SECRET-MARKER" not in repr(result.failures)
    assert "<script>" not in repr(result.failures)


# --- 21-23: determinism, no mutation, no side effects ---------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_output_is_deterministic(name: str) -> None:
    assert extract(load(name)) == extract(load(name))


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_input_is_not_mutated(name: str) -> None:
    payload = load(name)
    snapshot = copy.deepcopy(payload)
    extract(payload)
    assert payload == snapshot


def test_input_with_failures_is_not_mutated() -> None:
    payload = listing(base_job(title=None), base_job(absolute_url="x"), None)  # type: ignore[arg-type]
    snapshot = copy.deepcopy(payload)
    extract(payload)
    assert payload == snapshot


def test_no_network_access(no_network: None) -> None:
    for name in ALL_FIXTURES:
        extract(load(name))


def test_module_imports_nothing_that_does_io_or_reads_the_clock() -> None:
    tree = ast.parse(Path(greenhouse.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "socket", "sqlite3", "os", "sys", "requests", "httpx", "urllib3", "scrapy", "time",
        "logging", "structlog", "pathlib", "shutil", "tempfile", "subprocess", "asyncio",
    }  # fmt: skip
    assert not imported & forbidden
    assert "urllib" not in imported or all(
        not (isinstance(n, ast.ImportFrom) and n.module == "urllib.request") for n in ast.walk(tree)
    )
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not called & {"open", "print", "now", "utcnow", "today", "getenv", "get_settings"}


# --- 24: no database / file-system side effects ---------------------------------------------


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


# --- 25: compatibility with Phase 1 ---------------------------------------------------------


@pytest.mark.parametrize("name", [n for n in ALL_FIXTURES if n != "list_invalid_incomplete.json"])
def test_records_round_trip_through_phase1_validation(name: str) -> None:
    for record in extract(load(name)).records:
        revalidated = validate_job(record.model_dump())
        assert not isinstance(revalidated, Rejection), revalidated
        assert revalidated == record


def test_records_work_with_phase1_dedupe() -> None:
    job = base_job()
    twin = base_job(
        absolute_url="https://boards.greenhouse.io/example-corp-fixture/jobs/8800000001?utm_source=x"
    )
    other = base_job(id=8800000099, absolute_url="https://boards.greenhouse.io/x/jobs/8800000099")
    result = dedupe(extract(listing(job, twin, other)).records)
    assert len(result.accepted) == 2
    assert len(result.duplicates) == 1
    assert result.duplicates[0].record.external_id == result.duplicates[0].first.external_id


def test_records_work_with_phase1_date_window_and_fingerprint() -> None:
    dated = only_record(extract(load("detail_complete.json")))
    undated = only_record(extract(load("detail_missing_posted_date.json")))
    assert check_date_window(dated, COLLECTED_AT).outcome is DateOutcome.ELIGIBLE
    assert check_date_window(undated, COLLECTED_AT).outcome is DateOutcome.UNKNOWN_DATE
    assert content_fingerprint(dated.model_dump()) == content_fingerprint(undated.model_dump())


def test_identity_is_url_independent_but_normalization_is_idempotent() -> None:
    record = only_record(extract(load("list_multiple_locations.json")))
    assert normalize_url(str(record.apply_url)) == str(record.apply_url)


def test_every_fixture_is_accounted_for() -> None:
    """Jobs in each fixture are either extracted or reported; none vanish."""
    for name in ALL_FIXTURES:
        payload = load(name)
        result = extract(payload)
        expected = len(payload["jobs"]) if "jobs" in payload else 1
        assert result.job_count == len(result.records) + len(result.failures) == expected
