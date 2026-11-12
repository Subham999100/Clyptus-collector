from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from collector.models import JobRecord
from collector.pipelines.validate import Rejection, validate_job
from tests.conftest import make_raw


def codes(result: object) -> dict[str, str]:
    assert isinstance(result, Rejection)
    return {e.field: e.code for e in result.errors}


def test_valid_record_minimal_fields_stay_none() -> None:
    result = validate_job(make_raw())
    assert isinstance(result, JobRecord)
    assert result.location is None
    assert result.salary_text is None
    assert result.posted_date is None
    assert result.posted_date_source == "unknown"
    assert result.work_type is None
    assert result.external_id is None


def test_valid_record_all_fields() -> None:
    result = validate_job(
        make_raw(
            company_website="https://www.example.com",
            location="Testville",
            work_type="hybrid",
            employment_type="Full-time",
            salary_text="negotiable",
            posted_date="2026-01-10",
            posted_date_source="jsonld",
            external_id="abc-1",
            description_snippet="Snippet",
        )
    )
    assert isinstance(result, JobRecord)
    assert result.posted_date == date(2026, 1, 10)
    assert result.work_type == "hybrid"


@pytest.mark.parametrize("title", [None, "", "   \t"])
def test_missing_or_blank_title_rejected(title: str | None) -> None:
    raw = make_raw(title=title)
    if title is None:
        del raw["title"]
    assert "title" in codes(validate_job(raw))


def test_blank_company_name_rejected() -> None:
    assert "company_name" in codes(validate_job(make_raw(company_name="  ")))


def test_blank_platform_rejected() -> None:
    assert "platform" in codes(validate_job(make_raw(platform="")))


@pytest.mark.parametrize(
    "url",
    ["not a url", "ftp://example.com/job", "javascript:alert(1)", "/relative/path", "", "https://"],
)
def test_invalid_apply_url_rejected(url: str) -> None:
    assert "apply_url" in codes(validate_job(make_raw(apply_url=url)))


def test_missing_source_url_rejected() -> None:
    raw = make_raw()
    del raw["source_url"]
    assert codes(validate_job(raw))["source_url"] == "missing"


def test_invalid_company_website_rejected_but_blank_is_none() -> None:
    assert "company_website" in codes(validate_job(make_raw(company_website="nope")))
    result = validate_job(make_raw(company_website="  "))
    assert isinstance(result, JobRecord)
    assert result.company_website is None


def test_whitespace_is_stripped() -> None:
    result = validate_job(
        make_raw(
            title="  Test Engineer \n",
            apply_url="  https://jobs.example.com/apply/123  ",
            location="  Testville ",
            salary_text="   ",
        )
    )
    assert isinstance(result, JobRecord)
    assert result.title == "Test Engineer"
    assert str(result.apply_url) == "https://jobs.example.com/apply/123"
    assert result.location == "Testville"
    assert result.salary_text is None  # blank means not provided, not a guessed value


def test_unexpected_field_rejected() -> None:
    result = validate_job(make_raw(salary_min=100))
    assert codes(result)["salary_min"] == "extra_forbidden"


def test_invalid_enums_rejected() -> None:
    assert "work_type" in codes(validate_job(make_raw(work_type="remote-ish")))
    assert "posted_date_source" in codes(validate_job(make_raw(posted_date_source="guess")))


def test_date_source_without_date_rejected() -> None:
    assert "(record)" in codes(validate_job(make_raw(posted_date_source="jsonld")))


def test_invalid_datetime_rejected() -> None:
    assert "collected_at" in codes(validate_job(make_raw(collected_at="not-a-date")))
    assert "collected_at" in codes(validate_job(make_raw(collected_at="2026-13-45T00:00:00Z")))


def test_naive_collected_at_rejected_not_assumed_utc() -> None:
    result = validate_job(make_raw(collected_at=datetime(2026, 1, 15, 12, 0)))
    assert codes(result)["collected_at"] == "timezone_aware"


def test_collected_at_converted_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    result = validate_job(make_raw(collected_at=datetime(2026, 1, 15, 14, 0, tzinfo=plus_two)))
    assert isinstance(result, JobRecord)
    assert result.collected_at == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert result.collected_at.utcoffset() == timedelta(0)


def test_collected_at_accepts_iso_string() -> None:
    result = validate_job(make_raw(collected_at="2026-01-15T12:00:00+00:00"))
    assert isinstance(result, JobRecord)


def test_invalid_posted_date_rejected() -> None:
    assert "posted_date" in codes(validate_job(make_raw(posted_date="2026-02-31")))


def test_rejection_reports_all_errors_and_keeps_raw() -> None:
    raw = make_raw(title="", apply_url="nope")
    result = validate_job(raw)
    assert isinstance(result, Rejection)
    assert {e.field for e in result.errors} == {"title", "apply_url"}
    assert all(e.message for e in result.errors)
    assert result.raw is raw


def test_model_direct_construction_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        JobRecord.model_validate(make_raw(title=""))
