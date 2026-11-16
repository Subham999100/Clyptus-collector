"""Keka Hire adapter: synthetic fixtures (tests/fixtures/keka) shaped like the documented JobDTO.

Offline: the adapter is pure and nothing here touches the network. Status code 1 is a synthetic
"published" code; Keka does not document its status values.
"""

import copy
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from collector.adapters.keka import extract_keka_jobs
from collector.pipelines.validate import validate_job

FIXTURES = Path(__file__).parent / "fixtures" / "keka"
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
SOURCE = "https://exampletenant.keka.com/api/v1/hire/jobs"
PORTAL = "https://exampletenant.keka.com/careers/jobdetails/"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def extract(payload: Any, **overrides: Any):  # noqa: ANN201
    options: dict[str, Any] = {
        "company_name": "Example Tenant Ltd",
        "tenant": "exampletenant",
        "listed_statuses": {1},
        "source_url": SOURCE,
        "collected_at": NOW,
    }
    options.update(overrides)
    return extract_keka_jobs(payload, **options)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def test_complete_job_maps_documented_fields_and_no_date() -> None:
    result = extract(load("jobs_complete.json"))
    assert (result.response_type, result.job_count) == ("listing", 1)
    assert result.failures == result.warnings == result.skipped == ()
    (record,) = result.records
    assert record.title == "Backend Engineer"
    assert record.company_name == "Example Tenant Ltd"
    assert str(record.apply_url) == PORTAL + "101"
    assert str(record.source_url) == SOURCE
    assert record.platform == "keka"
    assert record.location == "Bengaluru, Karnataka, India"
    assert record.employment_type == "Full Time"
    assert record.external_id == "exampletenant:6f1d2c3b-0000-4000-8000-000000000101"
    assert record.description_snippet == "Build and run the services behind our hiring products."
    # publishedOn/createdOn are present but have no documented meaning or format: not used.
    assert (record.posted_date, record.posted_date_source) == (None, "unknown")
    assert record.work_type is None and record.salary_text is None
    assert validate_job(record.model_dump()) == record  # passes Phase 1 validation again


def test_multiple_jobs_keep_source_order_and_ignore_publication_fields() -> None:
    result = extract(load("jobs_multiple.json"))
    assert [r.title for r in result.records] == [
        "Data Analyst",
        "Product Designer",
        "Support Specialist",
    ]
    assert result.records[2].employment_type == "Part Time"
    assert {r.posted_date for r in result.records} == {None}
    assert result.warnings == ()  # a malformed publishedOn is not read, so it is not a warning


def test_empty_listing() -> None:
    result = extract(load("jobs_empty.json"))
    assert (result.records, result.failures, result.job_count) == ((), (), 0)
    assert result.response_type == "listing"


def test_missing_optional_fields_stay_none() -> None:
    (record,) = extract(load("jobs_missing_optional_fields.json")).records
    assert record.location is None and record.employment_type is None
    assert record.description_snippet is None and record.posted_date is None


def test_multiple_locations_are_joined_once_each_in_source_order() -> None:
    (record,) = extract(load("jobs_multiple_locations.json")).records
    assert record.location == (
        "Bengaluru, Karnataka, India; Pune, Maharashtra, India; Remote - India"
    )


def test_html_description_becomes_safe_text() -> None:
    (record,) = extract(load("jobs_html_description.json")).records
    snippet = record.description_snippet or ""
    assert snippet == "About the role Design reliable systems. Python SQL"
    assert "alert" not in snippet and "color" not in snippet and "<" not in snippet


def test_each_invalid_job_fails_alone_with_a_safe_reason() -> None:
    result = extract(load("jobs_invalid.json"))
    assert result.records == () and result.job_count == 4
    reasons = [(f.index, f.kind.value, [e.field for e in f.errors]) for f in result.failures]
    assert reasons == [
        (0, "missing_field", ["title"]),
        (1, "missing_field", ["careerPortalUrl"]),
        (2, "invalid_url", ["careerPortalUrl"]),
        (3, "invalid_type", ["title", "jobLocations"]),
    ]
    text = json.dumps([[e.message for e in f.errors] for f in result.failures])
    assert "localhost" not in text and "Pune" not in text  # messages never echo source values
    assert result.failures[0].job_id == "6f1d2c3b-0000-4000-8000-000000000601"


def test_one_invalid_job_does_not_discard_valid_ones() -> None:
    payload = load("jobs_invalid.json") + load("jobs_complete.json")
    result = extract(payload)
    assert len(result.failures) == 4 and [r.title for r in result.records] == ["Backend Engineer"]


def test_only_careers_site_jobs_with_a_published_status_are_collected() -> None:
    result = extract(load("jobs_unlisted.json"))
    assert [r.external_id for r in result.records] == [
        "exampletenant:6f1d2c3b-0000-4000-8000-000000000703"
    ]
    assert [(s.index, s.job_id, s.reason) for s in result.skipped] == [
        (
            0,
            "6f1d2c3b-0000-4000-8000-000000000701",
            "canListOnCareersSite is false: not listed on the careers site",
        ),
        (1, "6f1d2c3b-0000-4000-8000-000000000702", "status is not a configured published status"),
    ]
    assert len(result.records) + len(result.failures) + len(result.skipped) == result.job_count
    # A different, verified status set changes what counts as published.
    assert len(extract(load("jobs_unlisted.json"), listed_statuses={1, 3}).records) == 2


@pytest.mark.parametrize(
    ("changes", "field", "kind"),
    [
        ({"canListOnCareersSite": None}, "canListOnCareersSite", "missing_field"),
        ({"canListOnCareersSite": "true"}, "canListOnCareersSite", "invalid_type"),
        ({"status": None}, "status", "missing_field"),
        ({"status": "1"}, "status", "invalid_type"),
        ({"status": True}, "status", "invalid_type"),
        ({"id": 101}, "id", "invalid_type"),
        ({"id": "  "}, "id", "invalid_type"),
        ({"title": "   "}, "title", "missing_field"),
        ({"careerPortalUrl": "javascript:alert(1)"}, "careerPortalUrl", "invalid_url"),
        ({"careerPortalUrl": "https://user:pw@exampletenant.keka.com/x"}, "careerPortalUrl",
         "invalid_url"),
        ({"careerPortalUrl": "https://10.0.0.8/careers/1"}, "careerPortalUrl", "invalid_url"),
        ({"jobLocations": [{"city": 5}]}, "jobLocations.city", "invalid_type"),
        ({"jobLocations": ["Pune"]}, "jobLocations", "invalid_type"),
        ({"jobType": ["Full Time"]}, "jobType", "invalid_type"),
        ({"description": {"html": "x"}}, "description", "invalid_type"),
    ],
)  # fmt: skip
def test_unexpected_values_are_failures_never_guesses(
    changes: dict[str, Any], field: str, kind: str
) -> None:
    job = load("jobs_complete.json")[0] | changes
    result = extract([job])
    assert result.records == ()
    (failure,) = result.failures
    assert (failure.errors[0].field, failure.errors[0].code) == (field, kind)


def test_missing_id_keeps_the_job_without_a_platform_id() -> None:
    job = load("jobs_complete.json")[0]
    del job["id"]
    (record,) = extract([job]).records
    assert record.external_id is None  # Phase 1 identity then falls back to the apply URL


def test_non_list_responses_are_invalid() -> None:
    for payload in ({"data": []}, None, "jobs"):
        result = extract(payload)
        assert result.response_type is None and result.failures[0].kind.value == "invalid_response"
    assert extract([42]).failures[0].kind.value == "invalid_type"


@pytest.mark.parametrize(
    "overrides",
    [
        {"listed_statuses": set()},
        {"listed_statuses": {"1"}},
        {"listed_statuses": {True}},
        {"tenant": "bad tenant/../x"},
        {"company_name": " "},
        {"source_url": "https://localhost/api/v1/hire/jobs"},
        {"collected_at": datetime(2026, 11, 16)},
    ],
)
def test_bad_configuration_raises(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        extract([], **overrides)


def test_input_is_not_mutated_and_output_is_deterministic() -> None:
    payload = load("jobs_unlisted.json") + load("jobs_multiple_locations.json")
    before = copy.deepcopy(payload)
    first, second = extract(payload), extract(payload)
    assert payload == before
    assert first == second


def test_long_descriptions_are_cut_to_the_snippet_limit() -> None:
    job = load("jobs_complete.json")[0] | {"description": "<p>" + "word " * 400 + "</p>"}
    (record,) = extract([job]).records
    assert len(record.description_snippet or "") <= 500
