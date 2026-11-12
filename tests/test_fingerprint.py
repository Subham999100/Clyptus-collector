import re
from typing import Any

import pytest

from collector.utils.fingerprint import FINGERPRINT_FIELDS, content_fingerprint
from tests.conftest import make_record

BASE: dict[str, Any] = {
    "title": "Test Engineer",
    "company_name": "Example Corp",
    "location": "Testville",
    "description_snippet": "Snippet",
    "salary_text": "negotiable",
    "employment_type": "Full-time",
    "work_type": "remote",
}


def test_is_sha256_hex_and_deterministic() -> None:
    value = content_fingerprint(BASE)
    assert re.fullmatch(r"[0-9a-f]{64}", value)
    assert content_fingerprint(dict(BASE)) == value


def test_known_value_is_stable_across_runs() -> None:
    # Guards against accidental changes to the serialization (and against builtin hash()).
    assert content_fingerprint(BASE) == (
        "93aab3dfc130e770b6f670ae90750aad8a1edf293dd976b59e0a12ddbfb60e5a"
    )


@pytest.mark.parametrize("field", FINGERPRINT_FIELDS)
def test_each_field_affects_the_fingerprint(field: str) -> None:
    assert content_fingerprint({**BASE, field: "something else"}) != content_fingerprint(BASE)
    assert content_fingerprint({**BASE, field: None}) != content_fingerprint(BASE)


def test_salary_change_affects_fingerprint() -> None:
    a = make_record(salary_text="10 LPA").model_dump()
    b = make_record(salary_text="12 LPA").model_dump()
    assert content_fingerprint(a) != content_fingerprint(b)


def test_field_boundaries_are_preserved() -> None:
    a = content_fingerprint({**BASE, "title": "a b", "company_name": "c"})
    b = content_fingerprint({**BASE, "title": "a", "company_name": "b c"})
    c = content_fingerprint({**BASE, "title": "a", "company_name": "b", "location": "c"})
    assert len({a, b, c}) == 3


def test_whitespace_and_unicode_normalized() -> None:
    noisy = {**BASE, "title": "  Test \n Engineer\t", "location": "Café"}
    clean = {**BASE, "title": "Test Engineer", "location": "Café"}
    assert content_fingerprint(noisy) == content_fingerprint(clean)


def test_blank_equals_missing() -> None:
    assert content_fingerprint({**BASE, "location": "  "}) == content_fingerprint(
        {**BASE, "location": None}
    )


def test_case_is_significant() -> None:
    assert content_fingerprint({**BASE, "title": "TEST ENGINEER"}) != content_fingerprint(BASE)


def test_observation_timestamps_urls_and_other_fields_are_ignored() -> None:
    a = make_record(location="Testville").model_dump()
    b = make_record(
        location="Testville",
        collected_at="2030-01-01T00:00:00Z",
        apply_url="https://other.example.com/x",
        posted_date="2026-01-01",
        posted_date_source="jsonld",
        external_id="zzz",
    ).model_dump()
    assert content_fingerprint(a) == content_fingerprint(b)
    assert content_fingerprint({**BASE, "last_seen_at": "x"}) == content_fingerprint(BASE)
