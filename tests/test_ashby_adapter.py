"""Ashby adapter tests. Offline: synthetic fixtures and in-memory payloads only."""

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

from collector.adapters import ashby, common
from collector.adapters.ashby import (
    DESCRIPTION_SNIPPET_MAX,
    AshbyExtraction,
    ExtractionFailure,
    FailureKind,
    SkippedJob,
    extract_ashby_jobs,
)
from collector.models import JobRecord
from collector.pipelines.date_window import DateOutcome, check_date_window
from collector.pipelines.dedupe import dedupe
from collector.pipelines.validate import Rejection, validate_job
from collector.utils.fingerprint import content_fingerprint
from collector.utils.identity import job_key
from collector.utils.urls import normalize_url

FIXTURES = Path(__file__).parent / "fixtures" / "ashby"
BOARD = "example-corp-fixture"
SOURCE_URL = f"https://api.ashbyhq.com/posting-api/job-board/{BOARD}"
COLLECTED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ALL_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.json"))
FIRST_ID = "00000000-0000-4000-8000-000000000001"
PUBLISHED_DATE = date(2026, 10, 6)  # every fixture's publishedAt falls on this day
DROP = object()  # pass as an override to delete a key


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def extract(payload: Any, **overrides: Any) -> AshbyExtraction:
    kwargs: dict[str, Any] = {
        "company_name": "Example Corp",
        "board_name": BOARD,
        "source_url": SOURCE_URL,
        "collected_at": COLLECTED_AT,
    }
    kwargs.update(overrides)
    return extract_ashby_jobs(payload, **kwargs)


def board(*jobs: Any) -> dict[str, Any]:
    return {"apiVersion": "1", "jobs": list(jobs)}


def job(**overrides: Any) -> dict[str, Any]:
    """A complete job from the synthetic fixture, with keys replaced (``DROP`` deletes)."""
    item = copy.deepcopy(load("board_complete.json")["jobs"][0])
    for key, value in overrides.items():
        if value is DROP:
            item.pop(key, None)
        else:
            item[key] = value
    return item


def only_record(result: AshbyExtraction) -> JobRecord:
    assert len(result.records) == 1, result.failures
    assert not result.failures and not result.skipped
    return result.records[0]


def only_failure(result: AshbyExtraction) -> ExtractionFailure:
    assert len(result.failures) == 1 and not result.records, result.records
    return result.failures[0]


def url(n: int) -> str:
    return f"https://jobs.ashbyhq.com/{BOARD}/job-{n}"


# --- response shape ---------------------------------------------------------------------------


def test_complete_listing() -> None:
    result = extract(load("board_complete.json"))
    assert (result.response_type, result.job_count) == ("listing", 1)
    record = only_record(result)
    assert record.title == "Data Engineer"
    assert record.company_name == "Example Corp"
    assert str(record.apply_url) == f"https://jobs.ashbyhq.com/{BOARD}/{FIRST_ID}/application"
    assert str(record.source_url) == SOURCE_URL
    assert record.platform == "ashby"
    assert record.collected_at == COLLECTED_AT
    assert record.location == "Bengaluru, India"
    assert record.work_type == "onsite"
    assert record.employment_type == "FullTime"
    assert record.external_id == f"{BOARD}:{FIRST_ID}"
    assert record.description_snippet == "Build data pipelines."
    assert (record.posted_date, record.posted_date_source) == (PUBLISHED_DATE, "platform")
    assert record.salary_text is None  # compensation is not mapped


def test_empty_listing() -> None:
    assert extract(load("board_empty.json")) == AshbyExtraction((), (), "listing", 0)


def test_multiple_jobs_keep_source_order() -> None:
    jobs = [job(id=f"id-{n}", title=t, applyUrl=url(n)) for n, t in ((3, "C"), (1, "A"), (2, "B"))]
    result = extract(board(*jobs))
    assert [r.title for r in result.records] == ["C", "A", "B"]
    assert result.job_count == 3


@pytest.mark.parametrize(
    "payload",
    [None, [], "text", 5, True, {"ok": False, "error": "Board not found"}, {"postings": []}],
)
def test_response_without_a_jobs_list_is_an_invalid_response(payload: Any) -> None:
    result = extract(payload)
    failure = only_failure(result)
    assert failure.kind is FailureKind.INVALID_RESPONSE and failure.index is None
    assert result == AshbyExtraction((), result.failures, None, 0)
    assert "Board not found" not in repr(failure)


@pytest.mark.parametrize("jobs", ["x", 5, {"a": 1}, None])
def test_jobs_that_is_not_a_list_is_an_invalid_response(jobs: Any) -> None:
    failure = only_failure(extract({"apiVersion": "1", "jobs": jobs}))
    assert failure.kind is FailureKind.INVALID_RESPONSE


# --- unlisted jobs are skipped, not collected -------------------------------------------------


def test_unlisted_fixture_job_is_skipped_not_extracted() -> None:
    result = extract(load("board_unlisted_job.json"))
    assert result.records == () and result.failures == ()
    (skip,) = result.skipped
    assert (skip.index, skip.job_id) == (0, "00000000-0000-4000-8000-000000000007")
    assert "isListed" in skip.reason
    assert result.job_count == 1  # counted, never silently dropped


def test_skip_wins_even_when_the_unlisted_job_is_malformed() -> None:
    result = extract(board(job(isListed=False, title=None, applyUrl="nope")))
    assert result.failures == () and result.records == ()
    assert len(result.skipped) == 1


def test_listed_and_unlisted_jobs_are_partitioned() -> None:
    jobs = [
        job(id="a", applyUrl=url(1)),
        job(id="b", isListed=False),
        job(id="c", applyUrl=url(3), title=None),
    ]
    result = extract(board(*jobs))
    assert [r.external_id for r in result.records] == [f"{BOARD}:a"]
    assert [(s.index, s.job_id) for s in result.skipped] == [(1, "b")]
    assert [(f.index, f.job_id) for f in result.failures] == [(2, "c")]
    assert result.job_count == len(result.records) + len(result.failures) + len(result.skipped)


@pytest.mark.parametrize("listed", [DROP, None, True])
def test_absent_or_true_is_listed_is_collected(listed: Any) -> None:
    assert only_record(extract(board(job(isListed=listed)))).title == "Data Engineer"


@pytest.mark.parametrize("listed", ["false", "true", 0, 1, [], {}])
def test_wrong_type_is_listed_fails(listed: Any) -> None:
    failure = only_failure(extract(board(job(isListed=listed))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, "isListed")


def test_skipped_job_with_unusable_id_reports_no_id_and_no_errors() -> None:
    result = extract(board(job(isListed=False, id=["x"])))
    assert result.skipped[0].job_id is None
    assert result.failures == ()


# --- required fields and URLs -----------------------------------------------------------------


@pytest.mark.parametrize("title", [DROP, None, "", "  \t"])
def test_missing_or_blank_title_fails(title: Any) -> None:
    failure = only_failure(extract(board(job(title=title))))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "title"


def test_fixture_job_without_title_fails() -> None:
    first = extract(load("board_invalid_incomplete.json")).failures[0]
    assert (first.kind, first.index, first.job_id) == (
        FailureKind.MISSING_FIELD,
        0,
        "00000000-0000-4000-8000-000000000008",
    )
    assert first.errors[0].field == "title"


BAD_URLS = [
    "not a url",
    "/relative/path",
    "//jobs.ashbyhq.com/x/1",
    "ftp://jobs.ashbyhq.com/x/1",
    "javascript:alert(1)",
    "https://",
    "https://user:pass@jobs.ashbyhq.com/x/1",
    "https://localhost/x/1",
    "http://127.0.0.1/x/1",
    "http://10.0.0.5/x/1",
    "http://169.254.169.254/latest",
    "http://[::1]/x/1",
    "https://intranet/x/1",
    "https://jobs.ashbyhq.com:notaport/x/1",
]


@pytest.mark.parametrize("bad", BAD_URLS)
def test_invalid_or_unsafe_apply_url_without_a_fallback_fails(bad: str) -> None:
    failure = only_failure(extract(board(job(applyUrl=bad, jobUrl=DROP))))
    assert failure.kind is FailureKind.INVALID_URL
    assert failure.errors[0].field == "applyUrl"
    assert bad not in repr(failure)


def test_both_urls_unusable_reports_both_fields() -> None:
    failure = only_failure(extract(board(job(applyUrl="nope", jobUrl="ftp://x"))))
    assert [(e.field, e.code) for e in failure.errors] == [
        ("applyUrl", "invalid_url"),
        ("jobUrl", "invalid_url"),
    ]


@pytest.mark.parametrize("value", [DROP, None, "", "   "])
def test_both_urls_missing_is_a_missing_field_failure(value: Any) -> None:
    failure = only_failure(extract(board(job(applyUrl=value, jobUrl=value))))
    assert failure.kind is FailureKind.MISSING_FIELD
    assert failure.errors[0].field == "applyUrl"


def test_fixture_job_without_urls_fails() -> None:
    second = extract(load("board_invalid_incomplete.json")).failures[1]
    assert (second.kind, second.index) == (FailureKind.MISSING_FIELD, 1)


def test_apply_url_is_preferred_over_job_url() -> None:
    record = only_record(extract(board(job(applyUrl=url(1), jobUrl=url(2)))))
    assert str(record.apply_url) == url(1)


@pytest.mark.parametrize("apply_value", [DROP, None, "", "  "])
def test_absent_apply_url_falls_back_to_job_url_without_a_warning(apply_value: Any) -> None:
    result = extract(board(job(applyUrl=apply_value, jobUrl=url(2))))
    assert str(only_record(result).apply_url) == url(2)
    assert result.warnings == ()


def test_unusable_apply_url_falls_back_to_job_url_with_a_warning() -> None:
    result = extract(board(job(applyUrl="https://user:pw@jobs.ashbyhq.com/x", jobUrl=url(2))))
    assert str(only_record(result).apply_url) == url(2)
    (warning,) = result.warnings
    assert (warning.kind, warning.index, warning.field) == (FailureKind.INVALID_URL, 0, "applyUrl")
    assert warning.job_id == FIRST_ID
    assert "pw" not in warning.message and "user" not in warning.message


def test_wrong_type_apply_url_falls_back_with_an_invalid_type_warning() -> None:
    result = extract(board(job(applyUrl=["x"], jobUrl=url(2))))
    assert str(only_record(result).apply_url) == url(2)
    assert [(w.kind, w.field) for w in result.warnings] == [(FailureKind.INVALID_TYPE, "applyUrl")]


def test_no_url_is_constructed_from_board_and_id() -> None:
    failure = only_failure(extract(board(job(applyUrl=DROP, jobUrl=DROP))))
    assert failure.kind is FailureKind.MISSING_FIELD


def test_apply_url_is_normalized() -> None:
    messy = " HTTPS://Jobs.AshbyHQ.com:443/x/1/application?utm_source=a&ref=b#top "
    record = only_record(extract(board(job(applyUrl=messy))))
    assert str(record.apply_url) == "https://jobs.ashbyhq.com/x/1/application?ref=b"


# --- optional fields and locations ------------------------------------------------------------


def test_missing_optional_fields_stay_none() -> None:
    record = only_record(extract(load("board_missing_optional_fields.json")))
    assert record.employment_type is None
    assert record.salary_text is None
    assert record.company_website is None
    assert record.location == "Bengaluru, India"


def test_minimal_job_needs_only_title_and_a_url() -> None:
    record = only_record(extract(board({"title": "Role", "jobUrl": url(1)})))
    assert (record.location, record.work_type, record.employment_type) == (None, None, None)
    assert record.description_snippet is None
    assert (record.external_id, record.posted_date) == (None, None)


def test_multiple_locations_fixture() -> None:
    record = only_record(extract(load("board_multiple_locations.json")))
    assert record.location == "Bengaluru, India; Pune, India; Hyderabad, India"


@pytest.mark.parametrize(
    ("primary", "secondary", "expected"),
    [
        ("A", [{"location": "B"}, {"location": "C"}], "A; B; C"),
        ("A", [{"location": "a "}, {"location": "B"}], "A; B"),  # case-insensitive dedupe
        ("A", [], "A"),
        ("  A,\t B  ", DROP, "A, B"),
        (DROP, [{"location": "B"}], "B"),
        ("A", [{"address": {"addressLocality": "X"}}], "A"),  # entry without a location name
        ("", [], None),
        (DROP, DROP, None),
        (None, None, None),
    ],
)
def test_locations_are_combined_deterministically(
    primary: Any, secondary: Any, expected: str | None
) -> None:
    item = job(location=primary, secondaryLocations=secondary)
    assert only_record(extract(board(item))).location == expected


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"location": 5}, "location"),
        ({"secondaryLocations": "Pune"}, "secondaryLocations"),
        ({"secondaryLocations": ["Pune"]}, "secondaryLocations"),
        ({"secondaryLocations": [{"location": 5}]}, "secondaryLocations.location"),
    ],
)
def test_wrong_type_locations_fail(overrides: dict[str, Any], field: str) -> None:
    failure = only_failure(extract(board(job(**overrides))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, field)


# --- workplace type ---------------------------------------------------------------------------


def test_remote_fixture() -> None:
    record = only_record(extract(load("board_remote.json")))
    assert (record.work_type, record.location) == ("remote", "Remote - India")


def test_hybrid_fixture() -> None:
    assert only_record(extract(load("board_multiple_locations.json"))).work_type == "hybrid"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("OnSite", "onsite"),
        ("Remote", "remote"),
        ("Hybrid", "hybrid"),
        (" remote ", "remote"),
        ("HYBRID", "hybrid"),
        ("on-site", None),  # only the documented spelling counts
        ("flexible", None),  # unknown values are not guessed
        ("", None),
        (None, None),
        (DROP, None),
    ],
)
def test_workplace_type_uses_only_documented_values(value: Any, expected: str | None) -> None:
    assert only_record(extract(board(job(workplaceType=value)))).work_type == expected


def test_is_remote_alone_never_sets_the_work_type() -> None:
    item = job(workplaceType=DROP, isRemote=True, location="Remote - India")
    assert only_record(extract(board(item))).work_type is None


def test_is_remote_is_not_in_the_extraction_path_at_all() -> None:
    """Documented as true for Hybrid too: the adapter code never refers to the field."""
    assert ashby.__file__ is not None
    tree = ast.parse(Path(ashby.__file__).read_text(encoding="utf-8"))
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert "isRemote" not in strings


def test_work_type_is_never_inferred_from_text() -> None:
    item = job(
        workplaceType=DROP, location="Remote - India", descriptionPlain="A fully remote role."
    )
    assert only_record(extract(board(item))).work_type is None


def test_wrong_type_workplace_type_fails() -> None:
    failure = only_failure(extract(board(job(workplaceType=3))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, "workplaceType")


# --- employment type --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("FullTime", "FullTime"),
        ("PartTime", "PartTime"),
        ("Intern", "Intern"),
        ("Contract", "Contract"),
        ("Temporary", "Temporary"),
        ("  Full   Time ", "Full Time"),  # kept verbatim (collapsed), not mapped
        ("Seasonal", "Seasonal"),  # an undocumented value is kept, not guessed away
        ("", None),
        ("   ", None),
        (None, None),
        (DROP, None),
    ],
)
def test_employment_type_is_stored_verbatim(value: Any, expected: str | None) -> None:
    assert only_record(extract(board(job(employmentType=value)))).employment_type == expected


def test_wrong_type_employment_type_fails() -> None:
    failure = only_failure(extract(board(job(employmentType=["FullTime"]))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, "employmentType")


# --- descriptions -----------------------------------------------------------------------------


def test_html_description_is_converted_when_there_is_no_plain_text() -> None:
    item = load("board_html_description.json")["jobs"][0]
    del item["descriptionPlain"]
    snippet = only_record(extract(board(item))).description_snippet
    assert snippet == "About We build & ship. SQL Python"


def test_html_fixture_uses_its_plain_text() -> None:
    record = only_record(extract(load("board_html_description.json")))
    assert record.description_snippet == "About We build & ship. - SQL - Python"


def test_plain_text_is_preferred_over_html() -> None:
    item = job(descriptionPlain="Plain wins", descriptionHtml="<p>HTML loses</p>")
    assert only_record(extract(board(item))).description_snippet == "Plain wins"


def test_plain_text_is_not_parsed_as_html() -> None:
    item = job(descriptionPlain="Use <b>bold</b> & <script>x()</script> if a < b")
    snippet = only_record(extract(board(item))).description_snippet
    assert snippet == "Use <b>bold</b> & <script>x()</script> if a < b"


def test_html_conversion_drops_scripts_styles_and_comments() -> None:
    markup = (
        "<div>Hello<b>World</b></div><script>steal()</script><style>p{x:y}</style>"
        "<!-- hidden --><p>Next</p><ul><li>One</li><li>Two</li></ul>"
    )
    item = job(descriptionPlain=DROP, descriptionHtml=markup)
    assert only_record(extract(board(item))).description_snippet == "HelloWorld Next One Two"


@pytest.mark.parametrize("plain", [DROP, None, "", "   \n "])
def test_blank_plain_text_falls_back_to_html(plain: Any) -> None:
    item = job(descriptionPlain=plain, descriptionHtml="<p>From HTML</p>")
    assert only_record(extract(board(item))).description_snippet == "From HTML"


@pytest.mark.parametrize("value", [DROP, None, "", "<p> </p>", "<script>x()</script>"])
def test_no_usable_description_is_none(value: Any) -> None:
    item = job(descriptionPlain=DROP, descriptionHtml=value)
    assert only_record(extract(board(item))).description_snippet is None


@pytest.mark.parametrize("field", ["descriptionPlain", "descriptionHtml"])
def test_wrong_type_description_fails(field: str) -> None:
    overrides = {"descriptionPlain": DROP, "descriptionHtml": DROP, field: {"x": 1}}
    failure = only_failure(extract(board(job(**overrides))))
    assert (failure.kind, failure.errors[0].field) == (FailureKind.INVALID_TYPE, field)


def test_snippet_never_exceeds_limit() -> None:
    plain = " ".join(f"word{i}" for i in range(2000))
    snippet = only_record(extract(board(job(descriptionPlain=plain)))).description_snippet
    assert snippet is not None and len(snippet) <= DESCRIPTION_SNIPPET_MAX


def test_deeply_nested_html_does_not_overflow_the_stack() -> None:
    markup = "<div>" * 20000 + "deep" + "</div>" * 20000
    item = job(descriptionPlain=DROP, descriptionHtml=markup)
    assert only_record(extract(board(item))).description_snippet == "deep"


# --- posting dates: publishedAt ---------------------------------------------------------------


def test_published_at_fixture_sets_the_platform_posting_date() -> None:
    result = extract(load("board_complete.json"))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (PUBLISHED_DATE, "platform")
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-06", date(2026, 10, 6)),
        ("2026-10-06T08:15:30Z", date(2026, 10, 6)),
        ("2026-10-06T08:15:30.000+00:00", date(2026, 10, 6)),
        ("2026-10-06 08:15:30", date(2026, 10, 6)),
        # the date as written in the source: an offset is not converted
        ("2026-10-06T23:30:00-05:00", date(2026, 10, 6)),
        ("2026-10-06T01:30:00+05:30", date(2026, 10, 6)),
    ],
)
def test_valid_iso_published_at_is_used(value: str, expected: date) -> None:
    record = only_record(extract(board(job(publishedAt=value))))
    assert (record.posted_date, record.posted_date_source) == (expected, "platform")


@pytest.mark.parametrize("value", [DROP, None, "", "   "])
def test_absent_published_at_means_unknown_without_a_warning(value: Any) -> None:
    result = extract(board(job(publishedAt=value)))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    assert result.warnings == ()


def test_missing_published_at_fixture_is_unknown_date() -> None:
    result = extract(load("board_missing_published_at.json"))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    assert result.warnings == ()
    assert check_date_window(record, COLLECTED_AT).outcome is DateOutcome.UNKNOWN_DATE


@pytest.mark.parametrize(
    "value",
    ["3 days ago", "yesterday", "2026-13-45", "06/10/2026", "October 6, 2026", "1791194400000"],
)
def test_malformed_published_at_is_a_warning_and_the_job_is_kept(value: str) -> None:
    result = extract(board(job(publishedAt=value)))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    (warning,) = result.warnings
    assert (warning.kind, warning.field) == (FailureKind.INVALID_DATE, "publishedAt")
    assert value not in warning.message


@pytest.mark.parametrize("value", [1791194400000, True, ["2026-10-06"], {"iso": "x"}])
def test_wrong_type_published_at_is_a_warning_and_the_job_is_kept(value: Any) -> None:
    result = extract(board(job(publishedAt=value)))
    record = only_record(result)
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    (warning,) = result.warnings
    assert (warning.kind, warning.field) == (FailureKind.INVALID_TYPE, "publishedAt")


@pytest.mark.parametrize(
    "collected_at",
    [
        COLLECTED_AT,
        datetime(2030, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 20, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    ],
)
def test_collected_at_is_never_substituted_as_the_posting_date(collected_at: datetime) -> None:
    for payload in (load("board_missing_published_at.json"), board(job(publishedAt="garbage"))):
        record = extract(payload, collected_at=collected_at).records[0]
        assert (record.posted_date, record.posted_date_source) == (None, "unknown")
        assert record.collected_at == collected_at


def test_published_at_date_does_not_depend_on_the_collection_time() -> None:
    later = COLLECTED_AT + timedelta(days=400)
    a = only_record(extract(load("board_complete.json")))
    b = only_record(extract(load("board_complete.json"), collected_at=later))
    assert a.posted_date == b.posted_date == PUBLISHED_DATE


def test_phase1_date_window_classifies_ashby_dates() -> None:
    dated = only_record(extract(load("board_complete.json")))
    assert check_date_window(dated, COLLECTED_AT).outcome is DateOutcome.ELIGIBLE
    undated = only_record(extract(load("board_missing_published_at.json")))
    window = check_date_window(undated, COLLECTED_AT)
    assert window.outcome is DateOutcome.UNKNOWN_DATE
    assert window.needs_review and not window.eligible


# --- identity and configuration ---------------------------------------------------------------


def test_identity_is_stable_across_repeated_extraction_and_collection_times() -> None:
    first = only_record(extract(load("board_complete.json")))
    later = only_record(
        extract(load("board_complete.json"), collected_at=COLLECTED_AT + timedelta(days=3))
    )
    assert job_key(first) == job_key(later)


def test_identity_survives_a_changed_url_when_the_id_is_present() -> None:
    a = only_record(extract(board(job(applyUrl="https://a.example.com/x"))))
    b = only_record(extract(board(job(applyUrl="https://b.example.com/y"))))
    assert job_key(a) == job_key(b)


def test_identity_is_scoped_by_board_name() -> None:
    payload = load("board_complete.json")
    one = only_record(extract(payload, board_name="board-one"))
    two = only_record(extract(payload, board_name="board-two"))
    assert one.external_id == f"board-one:{FIRST_ID}"
    assert two.external_id == f"board-two:{FIRST_ID}"
    assert job_key(one) != job_key(two)


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
    assert only_record(extract(board(job(id=raw_id)))).external_id == f"{BOARD}:{canonical}"


def test_absent_id_falls_back_to_url_identity() -> None:
    record = only_record(extract(board(job(id=DROP))))
    assert record.external_id is None
    key = normalize_url(str(record.apply_url))
    assert job_key(record) == f"url:{hashlib.sha256(key.encode()).hexdigest()}"


@pytest.mark.parametrize(
    "bad_id", [123, True, 1.5, ["a"], {"x": 1}, "", "  ", "has space", "a\x00b"]
)
def test_unusable_id_is_a_fatal_invalid_type(bad_id: Any) -> None:
    failure = only_failure(extract(board(job(id=bad_id))))
    assert (failure.kind, failure.job_id, failure.errors[0].field) == (
        FailureKind.INVALID_TYPE,
        None,
        "id",
    )


def test_duplicate_job_records_are_both_extracted_and_phase1_dedupes_them() -> None:
    twin = job(applyUrl=f"https://jobs.ashbyhq.com/{BOARD}/{FIRST_ID}/application?gh_src=x")
    other = job(id="00000000-0000-4000-8000-000000000099", applyUrl=url(99))
    result = dedupe(extract(board(job(), twin, other)).records)
    assert (len(result.accepted), len(result.duplicates)) == (2, 1)


def test_company_name_comes_from_configuration_only() -> None:
    record = only_record(extract(load("board_complete.json"), company_name="Configured Ltd"))
    assert record.company_name == "Configured Ltd"


def test_company_website_is_normalized_and_optional() -> None:
    payload = load("board_complete.json")
    assert str(
        only_record(extract(payload, company_website="https://Example.com")).company_website
    ) == ("https://example.com/")
    assert only_record(extract(payload)).company_website is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_name": ""},
        {"company_name": "   "},
        {"board_name": ""},
        {"board_name": "has space"},
        {"board_name": "a:b"},
        {"collected_at": datetime(2026, 10, 20, 12, 0)},
        {"source_url": "not a url"},
        {"source_url": "https://localhost/x"},
        {"company_website": "ftp://example.com"},
    ],
)
def test_invalid_configuration_raises(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        extract(load("board_complete.json"), **overrides)


def test_unknown_fields_are_ignored() -> None:
    item = job(brandNew={"nested": [1, 2]}, compensation={"x": 1}, departmentId="d-1")
    assert only_record(extract(board(item))) == only_record(extract(board(job())))


# --- isolation --------------------------------------------------------------------------------


def test_one_invalid_job_does_not_stop_the_others() -> None:
    jobs = [
        job(id="a", applyUrl=url(1)),
        job(id="b", title=None),
        job(id="c", applyUrl=url(3)),
    ]
    result = extract(board(*jobs))
    assert [r.external_id for r in result.records] == [f"{BOARD}:a", f"{BOARD}:c"]
    assert [(f.index, f.job_id, f.kind) for f in result.failures] == [
        (1, "b", FailureKind.MISSING_FIELD)
    ]
    assert result.job_count == 3


def test_all_problems_of_a_job_are_reported_together() -> None:
    item = job(
        title="",
        applyUrl="ftp://x",
        jobUrl=DROP,
        location=5,
        secondaryLocations="x",
        workplaceType=9,
        employmentType=["FullTime"],
    )
    failure = only_failure(extract(board(item)))
    assert [(e.field, e.code) for e in failure.errors] == [
        ("title", "missing_field"),
        ("applyUrl", "invalid_url"),
        ("location", "invalid_type"),
        ("secondaryLocations", "invalid_type"),
        ("employmentType", "invalid_type"),
        ("workplaceType", "invalid_type"),
    ]
    assert failure.kind is FailureKind.MISSING_FIELD  # kind of the first problem


@pytest.mark.parametrize("item", [None, "job", 7, ["a"], True])
def test_non_object_job_is_isolated(item: Any) -> None:
    result = extract(board(item, job()))
    assert len(result.records) == 1
    failure = result.failures[0]
    assert (failure.kind, failure.index, failure.job_id) == (FailureKind.INVALID_TYPE, 0, None)


def test_mixed_valid_invalid_and_skipped_jobs_stay_isolated() -> None:
    jobs = [
        job(id="a", applyUrl=url(1)),
        job(id="b", applyUrl=url(2), publishedAt="garbage"),
        job(id="c", applyUrl=url(3), title=None),
        job(id="d", applyUrl="nope", jobUrl=url(4)),
        None,
        job(id="e", isListed=False),
        job(id="f", applyUrl=url(6)),
    ]
    result = extract(board(*jobs))
    assert [r.external_id for r in result.records] == [f"{BOARD}:{c}" for c in "abdf"]
    assert [(f.index, f.job_id) for f in result.failures] == [(2, "c"), (4, None)]
    assert [(s.index, s.job_id) for s in result.skipped] == [(5, "e")]
    assert [(w.index, w.job_id, w.field) for w in result.warnings] == [
        (1, "b", "publishedAt"),
        (3, "d", "applyUrl"),
    ]
    assert result.job_count == 7 == len(result.records) + len(result.failures) + len(result.skipped)


def test_warning_for_a_failed_job_is_still_reported() -> None:
    result = extract(board(job(title=None, publishedAt="garbage")))
    assert only_failure(result).errors[0].field == "title"
    assert [w.field for w in result.warnings] == ["publishedAt"]


def test_every_fixture_extracts_without_warnings() -> None:
    assert all(extract(load(name)).warnings == () for name in ALL_FIXTURES)


def test_messages_never_expose_source_values_or_html() -> None:
    marker = "SECRET-MARKER-<script>alert(1)</script>"
    bad = [
        job(title=[marker]),
        job(applyUrl=f"ftp://{marker}", jobUrl=DROP),
        job(location=[marker]),
        job(secondaryLocations=marker),
        job(workplaceType=[marker]),
        job(employmentType={"x": marker}),
        job(descriptionPlain={"x": marker}),
        job(id=[marker]),
        job(isListed=marker),
        marker,
    ]
    warned = [
        job(applyUrl=f"https://{marker}@x.example", jobUrl=url(1)),
        job(publishedAt=marker),
    ]
    skipped = [job(isListed=False, title=marker)]
    result = extract(board(*bad, *warned, *skipped))
    assert len(result.failures) == len(bad) and len(result.warnings) == len(warned)
    assert len(result.records) == len(warned) and len(result.skipped) == len(skipped)
    text = repr(result.failures) + repr(result.warnings) + repr(result.skipped)
    assert "SECRET-MARKER" not in text and "<script>" not in text


# --- determinism, no mutation, no side effects ------------------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_output_is_deterministic(name: str) -> None:
    assert extract(load(name)) == extract(load(name))


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_input_is_not_mutated(name: str) -> None:
    payload = load(name)
    snapshot = copy.deepcopy(payload)
    extract(payload)
    assert payload == snapshot


def test_input_with_failures_warnings_and_skips_is_not_mutated() -> None:
    payload = board(job(title=None), job(publishedAt="x"), None, job(isListed=False), job(id=5))
    snapshot = copy.deepcopy(payload)
    extract(payload)
    assert payload == snapshot


def test_no_network_access(no_network: None) -> None:
    for name in ALL_FIXTURES:
        extract(load(name))


@pytest.mark.parametrize("module", [common, ashby], ids=lambda m: m.__name__)
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


# --- Phase 1 and fixture compatibility --------------------------------------------------------


@pytest.mark.parametrize("name", [n for n in ALL_FIXTURES if n != "board_invalid_incomplete.json"])
def test_records_round_trip_through_phase1_validation(name: str) -> None:
    for record in extract(load(name)).records:
        revalidated = validate_job(record.model_dump())
        assert not isinstance(revalidated, Rejection), revalidated
        assert revalidated == record


def test_fingerprint_is_stable_and_ignores_observation_fields() -> None:
    a = only_record(extract(load("board_complete.json")))
    b = only_record(
        extract(
            load("board_complete.json"),
            collected_at=COLLECTED_AT + timedelta(days=3),
            source_url=SOURCE_URL + "?includeCompensation=true",
        )
    )
    assert content_fingerprint(a.model_dump()) == content_fingerprint(b.model_dump())
    changed = only_record(extract(board(job(title="Renamed Role"))))
    assert content_fingerprint(changed.model_dump()) != content_fingerprint(a.model_dump())


def test_every_fixture_job_is_extracted_reported_or_skipped() -> None:
    manifest = json.loads((FIXTURES.parent / "manifest.json").read_text(encoding="utf-8"))
    expected = {
        e["path"].split("/", 1)[1]: e["expected_listing_count"]
        for e in manifest["fixtures"]
        if e["platform"] == "ashby"
    }
    assert set(expected) == set(ALL_FIXTURES)
    for name, count in expected.items():
        result = extract(load(name))
        accounted = len(result.records) + len(result.failures) + len(result.skipped)
        assert result.job_count == accounted == count, name


def test_invalid_fixture_failures_match_the_manifest() -> None:
    manifest = json.loads((FIXTURES.parent / "manifest.json").read_text(encoding="utf-8"))
    entry = next(
        e for e in manifest["fixtures"] if e["path"].endswith("board_invalid_incomplete.json")
    )
    result = extract(load("board_invalid_incomplete.json"))
    assert entry["absent_fields_per_item"] == [["title"], ["jobUrl", "applyUrl"]]
    assert [f.errors[0].field for f in result.failures] == ["title", "applyUrl"]
    assert not result.records


def test_extractions_without_skips_compare_equal_to_the_plain_result_type() -> None:
    """The ``skipped`` field defaults to empty, so results from adapters that never skip are
    unchanged (guards the shared ``Extraction`` change)."""
    result = extract(load("board_empty.json"))
    assert result == AshbyExtraction((), (), "listing", 0, ())
    assert result.skipped == () and isinstance(result.skipped, tuple)
    (skip,) = extract(load("board_unlisted_job.json")).skipped
    assert isinstance(skip, SkippedJob)
