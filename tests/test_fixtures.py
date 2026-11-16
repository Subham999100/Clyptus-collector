"""Structural checks on tests/fixtures, driven by tests/fixtures/manifest.json.

The manifest is the single description of each fixture (scenario, provenance, expected counts,
documented source fields). Nothing here extracts jobs: that is later-phase production code.
"""

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from selectolax.parser import HTMLParser

from collector.pipelines.store import SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"
PACKAGE = Path(__file__).parent.parent / "collector"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
BY_PATH = {entry["path"]: entry for entry in MANIFEST}
JSON_ENTRIES = [e for e in MANIFEST if e["path"].endswith(".json")]
HTML_ENTRIES = [e for e in MANIFEST if e["path"].endswith(".html")]
JSONLD_ENTRIES = [e for e in MANIFEST if e["platform"] == "jsonld"]

REQUIRED_API_SCENARIOS = {
    "complete",
    "missing_optional_fields",
    "multiple_locations",
    "remote",
    "html_description",
    "missing_posted_date",
    "empty",
    "invalid",
}
REQUIRED_SCENARIOS = {
    "greenhouse": REQUIRED_API_SCENARIOS,
    "lever": REQUIRED_API_SCENARIOS,
    "ashby": REQUIRED_API_SCENARIOS | {"unlisted_job"},
    # Phase 7: Keka documents no work-type or usable date field, so no remote/date scenarios.
    "keka": {
        "complete",
        "multiple_jobs",
        "missing_optional_fields",
        "multiple_locations",
        "html_description",
        "empty",
        "invalid",
        "hidden_or_unpublished",
    },
    "jsonld": {
        "single_job_posting",
        "multiple_job_postings",
        "graph",
        "missing_posted_date",
        "invalid",
        "mixed_schema_objects",
    },
    "generic": {
        "listing_list",
        "listing_cards",
        "empty",
        "js_rendered_shell",
        "detail_microdata",
        "detail_plain_html",
        # Phase 8: generic HTML extraction (collector/adapters/html.py).
        "single_job",
        "detail_page",
        "paginated",
        "missing_optional_fields",
        "missing_required_fields",
        "malformed_html",
        "duplicate_job_links",
        "invalid_job_url",
        "unknown_posting_date",
        "invalid_posting_date",
        "broken_pagination",
        "cross_domain_links",
    },
}
ALLOWED_SCENARIOS = set().union(*REQUIRED_SCENARIOS.values()) | {"remote"}
REQUIRED_JSONLD_VARIANTS = {
    "single",
    "multiple",
    "graph",
    "missing_date_posted",
    "invalid",
    "mixed",
}


# --- helpers ------------------------------------------------------------------------------


def read(entry: dict[str, Any]) -> str:
    return (FIXTURES / entry["path"]).read_text(encoding="utf-8")


def json_items(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The job objects of a JSON fixture, according to its documented container."""
    data = json.loads(read(entry))
    return {"jobs": lambda: data["jobs"], "array": lambda: data, "object": lambda: [data]}[
        entry["container"]
    ]()


def lookup(item: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    """Follow a dotted path; returns (key exists, value)."""
    value: Any = item
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def parse_jsonld(html: str) -> tuple[list[dict[str, Any]], int, bool]:
    """(objects found in valid blocks, number of unparseable blocks, whether @graph was used).

    Strict ``json`` parsing on purpose: a block that strict JSON rejects counts as invalid.
    """
    objects: list[dict[str, Any]] = []
    invalid = 0
    saw_graph = False
    for node in HTMLParser(html).css("script[type='application/ld+json']"):
        try:
            data = json.loads(node.text())
        except ValueError:
            invalid += 1
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and "@graph" in obj:
                saw_graph = True
                objects.extend(o for o in obj["@graph"] if isinstance(o, dict))
            elif isinstance(obj, dict):
                objects.append(obj)
    return objects, invalid, saw_graph


def job_postings(html: str) -> list[dict[str, Any]]:
    return [o for o in parse_jsonld(html)[0] if o.get("@type") == "JobPosting"]


def check_provenance(entry: dict[str, Any]) -> list[str]:
    """Rule violations for an entry's provenance label (empty list means it is consistent)."""
    problems = []
    if entry.get("provenance") not in ("synthetic", "captured"):
        problems.append("provenance must be 'synthetic' or 'captured'")
    elif entry["provenance"] == "synthetic":
        if "captured_at" in entry:
            problems.append("synthetic fixture must not claim a capture date")
    else:
        for key in ("captured_at", "source_url"):
            if not entry.get(key):
                problems.append(f"captured fixture needs {key}")
    return problems


# --- manifest and scenarios ---------------------------------------------------------------


def test_manifest_lists_every_fixture_file_exactly_once() -> None:
    on_disk = {p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*") if p.is_file()} - {
        "manifest.json"
    }
    listed = [e["path"] for e in MANIFEST]
    assert len(listed) == len(set(listed)), "duplicate manifest entries"
    assert set(listed) == on_disk


@pytest.mark.parametrize(
    "path", sorted(p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*.json"))
)
def test_json_fixture_parses(path: str) -> None:
    json.loads((FIXTURES / path).read_text(encoding="utf-8"))


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["path"])
def test_every_fixture_has_an_explicit_scenario(entry: dict[str, Any]) -> None:
    assert entry["scenario"] in ALLOWED_SCENARIOS
    assert entry["description"].strip()
    assert entry["source_ref"].strip()
    assert entry["platform"] in REQUIRED_SCENARIOS
    assert entry["path"].startswith(f"{entry['platform']}/")
    assert isinstance(entry["expected_listing_count"], int)


def test_each_platform_covers_its_required_scenarios() -> None:
    scenarios: dict[str, set[str]] = defaultdict(set)
    for entry in MANIFEST:
        scenarios[entry["platform"]].add(entry["scenario"])
    for platform, required in REQUIRED_SCENARIOS.items():
        assert required <= scenarios[platform], f"{platform} lacks {required - scenarios[platform]}"


# --- listing counts and documented fields -------------------------------------------------


@pytest.mark.parametrize("entry", JSON_ENTRIES, ids=lambda e: e["path"])
def test_json_listing_count_matches_manifest(entry: dict[str, Any]) -> None:
    assert len(json_items(entry)) == entry["expected_listing_count"]


@pytest.mark.parametrize("entry", JSONLD_ENTRIES, ids=lambda e: e["path"])
def test_jsonld_posting_and_invalid_block_counts_match_manifest(entry: dict[str, Any]) -> None:
    html = read(entry)
    assert len(job_postings(html)) == entry["expected_listing_count"]
    assert parse_jsonld(html)[1] == entry.get("invalid_blocks", 0)


@pytest.mark.parametrize(
    "entry", [e for e in HTML_ENTRIES if e["platform"] == "generic"], ids=lambda e: e["path"]
)
def test_generic_listing_count_matches_manifest(entry: dict[str, Any]) -> None:
    found = HTMLParser(read(entry)).css(entry["listing_selector"])
    assert len(found) == entry["expected_listing_count"]


def check_fields(entry: dict[str, Any], items: list[dict[str, Any]]) -> None:
    per_item_absent = entry.get("absent_fields_per_item")
    for index, item in enumerate(items):
        for field in entry.get("source_fields", []):
            assert lookup(item, field)[0], f"{entry['path']}[{index}] lacks {field}"
        absent = entry.get("absent_fields", []) + (
            per_item_absent[index] if per_item_absent else []
        )
        for field in absent:
            assert not lookup(item, field)[0], f"{entry['path']}[{index}] unexpectedly has {field}"
        for field in entry.get("null_or_empty_fields", []):
            exists, value = lookup(item, field)
            assert exists and value in (None, "", []), f"{entry['path']}[{index}].{field}"


@pytest.mark.parametrize("entry", JSON_ENTRIES, ids=lambda e: e["path"])
def test_documented_source_fields_exist_in_json_fixtures(entry: dict[str, Any]) -> None:
    check_fields(entry, json_items(entry))


@pytest.mark.parametrize("entry", JSONLD_ENTRIES, ids=lambda e: e["path"])
def test_documented_source_fields_exist_in_jsonld_fixtures(entry: dict[str, Any]) -> None:
    postings = job_postings(read(entry))
    assert postings, "fixture has no parseable JobPosting"
    check_fields(entry, postings)


def test_invalid_scenarios_really_lack_what_the_manifest_says() -> None:
    """Guards against an 'invalid' fixture that is actually valid."""
    invalid = [e for e in JSON_ENTRIES if e["scenario"] == "invalid"]
    assert len(invalid) == 4  # greenhouse, lever, ashby, keka
    assert all(e.get("absent_fields_per_item") for e in invalid)


# --- HTML structure -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry", [e for e in HTML_ENTRIES if e.get("required_selectors")], ids=lambda e: e["path"]
)
def test_html_fixtures_contain_their_intended_structures(entry: dict[str, Any]) -> None:
    tree = HTMLParser(read(entry))
    for required in entry["required_selectors"]:
        count = len(tree.css(required["selector"]))
        assert count >= required["min_count"], f"{required['selector']}: {count}"


def test_jsonld_fixtures_cover_the_required_structural_variants() -> None:
    claimed = set().union(*(set(e["variants"]) for e in JSONLD_ENTRIES))
    assert REQUIRED_JSONLD_VARIANTS <= claimed

    # ...and every claim is true of the file it is attached to.
    checks = {
        "single": lambda objs, invalid, graph, postings: len(postings) == 1 and not graph,
        "multiple": lambda objs, invalid, graph, postings: len(postings) >= 2,
        "graph": lambda objs, invalid, graph, postings: graph and len(postings) >= 1,
        "missing_date_posted": lambda objs, invalid, graph, postings: any(
            "datePosted" not in p for p in postings
        ),
        "invalid": lambda objs, invalid, graph, postings: invalid >= 1,
        "mixed": lambda objs, invalid, graph, postings: any(
            o.get("@type") != "JobPosting" for o in objs
        ),
    }
    for entry in JSONLD_ENTRIES:
        objs, invalid, graph = parse_jsonld(read(entry))
        postings = [o for o in objs if o.get("@type") == "JobPosting"]
        for variant in entry["variants"]:
            assert checks[variant](objs, invalid, graph, postings), f"{entry['path']}: {variant}"


# --- safety and provenance ----------------------------------------------------------------

SECRET = re.compile(
    r"(authorization|bearer\s+\w|api[_-]?key|passwd|password|secret|set-cookie|\bcookie\b"
    r"|BEGIN [A-Z ]*PRIVATE|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{16,}|xox[bp]-)",
    re.IGNORECASE,
)
TRACKING = re.compile(r"(utm_|gclid|fbclid|msclkid|_hsenc|_hsmi|mc_eid|mc_cid)", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["path"])
def test_fixture_has_no_secrets_tracking_ids_or_personal_emails(entry: dict[str, Any]) -> None:
    text = read(entry)
    assert not SECRET.search(text), "credential-like text"
    assert not TRACKING.search(text), "tracking identifier"
    for domain in EMAIL.findall(text):
        assert domain.endswith((".example", "example.com", "example.org", "example.net"))


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["path"])
def test_every_fixture_is_labelled_synthetic_or_captured_consistently(
    entry: dict[str, Any],
) -> None:
    assert check_provenance(entry) == []
    if entry["provenance"] == "synthetic" and entry["path"].endswith(".html"):
        assert "SYNTHETIC FIXTURE" in read(entry).split("-->", 1)[0]  # visible in the file itself


def test_provenance_rules_reject_mislabelled_entries() -> None:
    assert check_provenance({"provenance": "synthetic"}) == []
    assert check_provenance({"provenance": "synthetic", "captured_at": "2026-01-01"})
    assert check_provenance({"provenance": "captured"})  # needs captured_at and source_url
    assert check_provenance({"provenance": "captured", "captured_at": "2026-01-01"})
    assert (
        check_provenance(
            {"provenance": "captured", "captured_at": "2026-01-01", "source_url": "https://x"}
        )
        == []
    )
    assert check_provenance({"provenance": "real"})
    assert check_provenance({})


def test_no_fixture_is_claimed_to_be_a_captured_response() -> None:
    """Phase 2 has no permitted captured samples: every fixture must say it is synthetic."""
    assert {e["provenance"] for e in MANIFEST} == {"synthetic"}


# --- scope guard --------------------------------------------------------------------------


def test_no_extraction_code_or_schema_change_was_added() -> None:
    # Phase 3 added the Greenhouse, Lever, Ashby and JSON-LD adapters and their shared helpers;
    # Phase 4 added the Scrapy spiders, Phase 5 the Excel export. Any other extraction code still
    # needs its own step.
    allowed = {
        "adapters": [
            "__init__.py",
            "ashby.py",
            "common.py",
            "greenhouse.py",
            "html.py",  # Phase 8: generic HTML extraction from reviewed selectors
            "json_ld.py",
            "keka.py",  # Phase 7: offline Keka Hire adapter
            "lever.py",
        ],
        # Phase 6B: pages.py fetches discovery pages through the same protected spider base.
        "spiders": ["__init__.py", "pages.py", "platforms.py", "run.py", "sources.py"],
        "export": ["__init__.py", "excel.py"],  # Phase 5: Excel export
        "detect": ["__init__.py", "platforms.py"],  # Phase 6: URL/page platform detection
    }
    for package in ("adapters", "spiders", "detect", "middlewares", "export"):
        files = sorted(p.name for p in (PACKAGE / package).glob("*.py"))
        assert files == allowed.get(package, ["__init__.py"]), (
            f"{package} must stay as approved until a later phase"
        )
    assert SCHEMA_VERSION == 2
