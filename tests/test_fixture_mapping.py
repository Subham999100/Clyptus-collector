"""Smoke-checks that the documented field mapping (docs/platform-field-mapping.md) can turn the
fixtures into valid ``JobRecord`` inputs, using the *existing* Phase 1 validation.

The ``raw_*`` functions below are test-only sketches of the documented mapping, not extraction
adapters. Company names and source URLs come from configuration/request context (they are not in
the Greenhouse, Lever or Ashby list responses), so they are supplied as constants here.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest

from collector.models import JobRecord
from collector.pipelines.validate import Rejection, validate_job
from collector.utils.dates import parse_posted_date
from tests.test_fixtures import JSON_ENTRIES, JSONLD_ENTRIES, job_postings, json_items, read

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
COMPANY = "Example Corp"  # from the seed/config, not from the API response


def raw_greenhouse(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": job.get("title"),
        "company_name": job.get("company_name", COMPANY),
        "apply_url": job.get("absolute_url"),
        "source_url": "https://boards-api.greenhouse.io/v1/boards/example-corp-fixture/jobs",
        "platform": "greenhouse",
        "collected_at": NOW,
        "external_id": str(job["id"]),
        "location": (job.get("location") or {}).get("name"),
    }


def raw_lever(posting: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": posting.get("text"),
        "company_name": COMPANY,
        "apply_url": posting.get("applyUrl") or posting.get("hostedUrl"),
        "source_url": "https://api.lever.co/v0/postings/example-corp-fixture?mode=json",
        "platform": "lever",
        "collected_at": NOW,
        "external_id": posting["id"],
    }


def raw_ashby(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": job.get("title"),
        "company_name": COMPANY,
        "apply_url": job.get("applyUrl") or job.get("jobUrl"),
        "source_url": "https://api.ashbyhq.com/posting-api/job-board/example-corp-fixture",
        "platform": "ashby",
        "collected_at": NOW,
        "external_id": job["id"],
    }


RAW = {"greenhouse": raw_greenhouse, "lever": raw_lever, "ashby": raw_ashby}


def api_cases(*, valid: bool) -> list[tuple[str, int, dict[str, Any]]]:
    cases = []
    for entry in JSON_ENTRIES:
        if entry["platform"] not in RAW or entry["scenario"] in ("empty", "unlisted_job"):
            continue
        if (entry["scenario"] == "invalid") is valid:
            continue
        cases += [(entry["path"], i, item) for i, item in enumerate(json_items(entry))]
    return cases


@pytest.mark.parametrize(
    ("path", "index", "item"), api_cases(valid=True), ids=lambda v: str(v)[:40]
)
def test_complete_and_partial_fixtures_map_to_valid_job_records(
    path: str, index: int, item: dict[str, Any]
) -> None:
    result = validate_job(RAW[path.split("/")[0]](item))
    assert isinstance(result, JobRecord), result


@pytest.mark.parametrize(
    ("path", "index", "item"), api_cases(valid=False), ids=lambda v: str(v)[:40]
)
def test_incomplete_fixtures_are_rejected_not_repaired(
    path: str, index: int, item: dict[str, Any]
) -> None:
    result = validate_job(RAW[path.split("/")[0]](item))
    assert isinstance(result, Rejection)
    assert {e.field for e in result.errors} & {"title", "apply_url"}


def test_unlisted_ashby_job_is_marked_not_listed() -> None:
    (entry,) = [e for e in JSON_ENTRIES if e["scenario"] == "unlisted_job"]
    assert [job["isListed"] for job in json_items(entry)] == [False]


@pytest.mark.parametrize("entry", [e for e in JSONLD_ENTRIES if e["scenario"] != "invalid"],
                         ids=lambda e: e["path"])  # fmt: skip
def test_jsonld_fixtures_map_to_valid_job_records(entry: dict[str, Any]) -> None:
    for posting in job_postings(read(entry)):
        raw = {
            "title": posting.get("title"),
            "company_name": posting["hiringOrganization"]["name"],
            "apply_url": posting.get("url") or "https://www.example.com/careers/page",
            "source_url": "https://www.example.com/careers/page",
            "platform": "jsonld",
            "collected_at": NOW,
        }
        assert isinstance(validate_job(raw), JobRecord)


def test_jsonld_posting_without_title_is_rejected() -> None:
    (entry,) = [e for e in JSONLD_ENTRIES if e["scenario"] == "invalid"]
    (posting,) = job_postings(read(entry))
    result = validate_job({"title": posting.get("title"), "company_name": COMPANY})
    assert isinstance(result, Rejection)


# --- documented date handling -------------------------------------------------------------


def only(path: str) -> dict[str, Any]:
    (item,) = json_items(next(e for e in JSON_ENTRIES if e["path"] == path))
    return item


def test_documented_date_fields_parse_to_the_date_as_written() -> None:
    assert parse_posted_date(only("greenhouse/detail_complete.json")["first_published"], NOW) == (
        date(2026, 10, 1)
    )
    assert parse_posted_date(only("ashby/board_complete.json")["publishedAt"], NOW) == (
        date(2026, 10, 6)
    )
    jsonld = {p.get("title"): p for e in JSONLD_ENTRIES for p in job_postings(read(e))}
    assert parse_posted_date(jsonld["Data Analyst"]["datePosted"], NOW) == date(2026, 10, 3)
    assert parse_posted_date(jsonld["Quality Engineer"]["datePosted"], NOW) == date(2026, 10, 2)


def test_greenhouse_updated_at_is_not_a_posting_date_field() -> None:
    """updated_at exists in every Greenhouse shape; the mapping must not use it as posted_date."""
    detail = only("greenhouse/detail_missing_posted_date.json")
    assert "updated_at" in detail
    assert "first_published" not in detail


def test_lever_created_at_is_epoch_milliseconds_assumption() -> None:
    """Assumption (third-party sources, not confirmed by the official page): epoch milliseconds."""
    created = only("lever/postings_complete.json")["createdAt"]
    assert datetime.fromtimestamp(created / 1000, UTC).date() == date(2026, 10, 5)
