"""Phase 6: discovery state, batches, review/authorization, approved-source export, coverage and
the ``discover`` / ``sources`` CLI. Offline; every company and URL is synthetic."""

import json
import socket
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from collector.cli import app
from collector.companies import registry
from collector.companies.discovery import FetchError, Page
from collector.companies.input import Company, company_key, load_companies
from collector.companies.registry import Authorization, StateError
from collector.pipelines.store import SCHEMA_VERSION
from collector.spiders.sources import Source, load_sources

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
HEADER = "company_name,official_website,careers_url\n"
runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("COLLECTOR_"):
            monkeypatch.delenv(name)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def no_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    import scrapy.crawler

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("crawler started")

    monkeypatch.setattr(scrapy.crawler.CrawlerProcess, "__init__", fail)
    monkeypatch.setattr(scrapy.crawler.CrawlerRunner, "__init__", fail)
    monkeypatch.setattr("collector.cli.crawl_sources", fail)


def make(
    name: str, line: int = 2, careers: str | None = None, website: str | None = None
) -> Company:
    return Company(name, company_key(name), line, website, careers)


def synthetic(count: int) -> list[Company]:
    """Name-only synthetic companies: offline discovery reports them NOT_FOUND."""
    return [make(f"Synthetic Company {i:04d}", i + 2) for i in range(count)]


def board_company(name: str, token: str) -> Company:
    return make(
        name, careers=f"https://boards.greenhouse.io/{token}", website="https://www.example.com/"
    )


def verified_state(tmp_path: Path, *companies: Company) -> Path:
    """Discover the companies offline, then confirm each candidate by (synthetic) review."""
    path = tmp_path / "state.json"
    registry.run_batch(list(companies), path, batch_size=len(companies), now=NOW)
    for entry in companies:
        registry.confirm_source(path, entry.name, by="QA reviewer", note="test evidence", now=NOW)
    return path


# --- batches ----------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [10, 20, 50])
def test_batches_follow_list_order_without_repeats(tmp_path: Path, size: int) -> None:
    companies = synthetic(120)
    path = tmp_path / "state.json"
    seen: list[str] = []
    while True:
        result = registry.run_batch(companies, path, batch_size=size, now=NOW)
        if not result.processed:
            break
        assert len(result.processed) <= size
        seen.extend(company.name for company, _ in result.processed)
    assert seen == [company.name for company in companies]  # each exactly once, in order
    assert len(registry.load_state(path).companies) == 120


def test_batch_size_is_bounded(tmp_path: Path) -> None:
    for size in (0, registry.MAX_BATCH_SIZE + 1):
        with pytest.raises(ValueError, match="batch size"):
            registry.run_batch(synthetic(1), tmp_path / "s.json", batch_size=size)


def test_large_input_is_processed_quickly_in_bounded_memory(tmp_path: Path) -> None:
    rows = "".join(f"Synthetic Company {i:05d},,\n" for i in range(5000))
    companies_csv = tmp_path / "companies.csv"
    companies_csv.write_text(HEADER + rows, encoding="utf-8")
    started = time.perf_counter()
    tracemalloc.start()
    listed = load_companies(companies_csv)
    result = registry.run_batch(listed.companies, tmp_path / "state.json", batch_size=1000, now=NOW)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.perf_counter() - started
    assert len(listed.companies) == 5000
    assert len(result.processed) == 1000 and result.remaining == 4000
    assert peak < 200 * 1024 * 1024
    assert elapsed < 120  # generous; the measured figure is reported separately


def test_repeated_runs_do_not_rediscover_until_the_input_changes(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    companies = synthetic(3)
    registry.run_batch(companies, path, batch_size=10, now=NOW)
    assert registry.run_batch(companies, path, batch_size=10, now=NOW).processed == ()
    changed = [companies[0], make(companies[1].name, 3, website="https://www.example.org/")]
    again = registry.run_batch(changed, path, batch_size=10, now=NOW)
    assert [company.name for company, _ in again.processed] == [companies[1].name]
    later = registry.run_batch(
        companies, path, batch_size=10, now=NOW + timedelta(days=31), recheck_after=timedelta(30)
    )
    assert len(later.processed) == 3


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = registry.run_batch(synthetic(2), tmp_path / "state.json", batch_size=5, dry_run=True)
    assert len(result.processed) == 2 and len(result.state.companies) == 2
    assert list(tmp_path.iterdir()) == []


def test_interrupted_batch_resumes_after_the_last_saved_company(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    companies = synthetic(6)
    real = registry.discover
    calls: list[str] = []

    def interrupted(company: Company, fetch: object = None, **kwargs: object):  # noqa: ANN202
        calls.append(company.name)
        if len(calls) == 4:
            raise KeyboardInterrupt  # the process is stopped mid-batch
        return real(company, fetch, **kwargs)

    monkeypatch.setattr(registry, "discover", interrupted)
    with pytest.raises(KeyboardInterrupt):
        registry.run_batch(companies, path, batch_size=6, now=NOW)
    saved = registry.load_state(path)  # still a valid file
    assert list(saved.companies) == [c.key for c in companies[:3]]
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]  # no temp file left

    monkeypatch.setattr(registry, "discover", real)
    resumed = registry.run_batch(companies, path, batch_size=6, now=NOW)
    assert [c.name for c, _ in resumed.processed] == [c.name for c in companies[3:]]


def test_progress_can_be_saved_after_every_company(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    real = registry.discover
    stored_before: list[int] = []

    def observing(company: Company, fetch: object = None, **kwargs: object):  # noqa: ANN202
        on_disk = registry.load_state(path).companies if path.exists() else {}
        stored_before.append(len(on_disk))
        return real(company, fetch, **kwargs)

    monkeypatch.setattr(registry, "discover", observing)
    registry.run_batch(synthetic(4), path, batch_size=4, now=NOW, save_interval=0)
    assert stored_before == [0, 1, 2, 3]  # each company was on disk before the next started
    assert len(registry.load_state(path).companies) == 4


def test_one_failing_company_does_not_stop_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    real = registry.discover

    def flaky(company: Company, fetch: object = None, **kwargs: object):  # noqa: ANN202
        if company.name.endswith("0001"):
            raise RuntimeError("secret detail https://user:pw@internal.example/x")
        return real(company, fetch, **kwargs)

    monkeypatch.setattr(registry, "discover", flaky)
    result = registry.run_batch(synthetic(3), path, batch_size=3, now=NOW)
    statuses = [(record["status"], record["failed"]) for _, record in result.processed]
    assert statuses == [("NOT_FOUND", False), ("REVIEW_REQUIRED", True), ("NOT_FOUND", False)]
    failed = result.processed[1][1]
    assert failed["reason"] == "discovery failed (RuntimeError)"  # type only, never the message
    assert "secret" not in path.read_text(encoding="utf-8")

    monkeypatch.setattr(registry, "discover", real)
    assert registry.run_batch(synthetic(3), path, batch_size=3, now=NOW).processed == ()
    retried = registry.run_batch(synthetic(3), path, batch_size=3, now=NOW, retry_failed=True)
    assert [(r["status"], r["failed"]) for _, r in retried.processed] == [("NOT_FOUND", False)]


# --- persistence and the verified-record rule -------------------------------------------------


def test_state_file_round_trips_and_keeps_authorizations_separate(tmp_path: Path) -> None:
    acme = board_company("Acme Example", "acmeexample")
    path = verified_state(tmp_path, acme)
    registry.authorize(path, "acme example", Authorization.APPROVED, by="Ops", note="written OK")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == registry.FORMAT and data["version"] == registry.VERSION
    record = data["companies"]["acme example"]
    assert record["status"] == "VERIFIED"
    assert (record["platform"], record["identifier"]) == ("greenhouse", "acmeexample")
    assert record["evidence"][-1] == "confirmed by manual review (QA reviewer): test evidence"
    assert record["discovered_at"] == record["last_verified_at"] == "2026-11-16T09:00:00+00:00"
    assert "decision" not in record  # authorization is not part of the discovery record
    assert data["authorizations"]["acme example"]["decision"] == "APPROVED"

    # Re-running discovery never touches the authorization section.
    registry.run_batch([acme], path, batch_size=1, now=NOW, recheck_after=timedelta(0))
    assert json.loads(path.read_text(encoding="utf-8"))["authorizations"] == data["authorizations"]


def test_verified_record_is_not_replaced_by_an_uncertain_recheck(tmp_path: Path) -> None:
    acme = board_company("Acme Example", "acmeexample")
    path = verified_state(tmp_path, acme)
    before = registry.load_state(path).companies["acme example"]

    def down(url: str) -> Page:
        raise FetchError("HTTP 503")

    later = NOW + timedelta(days=40)
    result = registry.run_batch(
        [acme], path, batch_size=1, fetch=down, now=later, recheck_after=timedelta(30)
    )
    assert result.kept_verified == 1
    after = registry.load_state(path).companies["acme example"]
    assert after["status"] == "VERIFIED"
    assert (after["platform"], after["identifier"]) == (before["platform"], before["identifier"])
    assert after["evidence"] == before["evidence"]
    assert after["last_verified_at"] == before["last_verified_at"]
    assert after["recheck"] == {
        "checked_at": "2026-12-26T09:00:00+00:00",
        "status": "REVIEW_REQUIRED",
        "reason": "official website could not be fetched (HTTP 503)",
    }


def test_verified_record_is_refreshed_by_a_recheck_that_confirms_it(tmp_path: Path) -> None:
    acme = board_company("Acme Example", "acmeexample")
    path = verified_state(tmp_path, acme)
    home = "<title>Acme Example</title><a href='https://boards.greenhouse.io/acmeexample'>Jobs</a>"

    def site(url: str) -> Page:
        return Page(url, home)

    later = NOW + timedelta(days=40)
    registry.run_batch(
        [acme], path, batch_size=1, fetch=site, now=later, recheck_after=timedelta(30)
    )
    record = registry.load_state(path).companies["acme example"]
    assert record["status"] == "VERIFIED" and record["recheck"] is None
    assert record["last_verified_at"] == "2026-12-26T09:00:00+00:00"
    assert record["discovered_at"] == "2026-11-16T09:00:00+00:00"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"format": "other", "version": 1, "companies": {}, "authorizations": {}}),
        json.dumps(
            {"format": registry.FORMAT, "version": 2, "companies": {}, "authorizations": {}}
        ),
        json.dumps(
            {
                "format": registry.FORMAT,
                "version": 1,
                "companies": {"x": {"status": "VERIFIED"}},
                "authorizations": {},
            }
        ),
        json.dumps(
            {
                "format": registry.FORMAT,
                "version": 1,
                "companies": {},
                "authorizations": {"x": {"decision": "YES"}},
            }
        ),
    ],
)
def test_unusable_state_files_are_refused_not_overwritten(tmp_path: Path, content: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StateError):
        registry.run_batch(synthetic(1), path, batch_size=1)
    assert path.read_text(encoding="utf-8") == content


# --- review and authorization -----------------------------------------------------------------


def test_discovered_sources_default_to_pending_and_are_never_exported(tmp_path: Path) -> None:
    path = verified_state(tmp_path, board_company("Acme Example", "acmeexample"))
    state = registry.load_state(path)
    assert registry.authorization_status(state.companies["acme example"], None) == "PENDING"
    export = registry.approved_sources(state)
    assert export.sources == ()
    assert export.review[0].reason == "collection is not approved (PENDING)"


def test_review_required_candidates_cannot_be_approved(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    registry.run_batch([board_company("Acme Example", "acmeexample")], path, batch_size=1)
    with pytest.raises(ValueError, match="only VERIFIED sources can be approved"):
        registry.authorize(path, "Acme Example", Authorization.APPROVED, by="Ops", note="ok")
    assert registry.load_state(path).authorizations == {}


def test_manual_confirmation_needs_a_complete_candidate(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    companies = [
        make("Name Only Example"),  # NOT_FOUND
        make(
            "Keka Example",
            careers="https://acme.keka.com/careers",
            website="https://www.example.com/",
        ),
        make(
            "Bad Token Example",
            careers="https://jobs.lever.co/",
            website="https://www.example.com/",
        ),
    ]
    registry.run_batch(companies, path, batch_size=3)
    with pytest.raises(ValueError, match="only REVIEW_REQUIRED"):
        registry.confirm_source(path, "Name Only Example", by="QA", note="x")
    with pytest.raises(ValueError, match="only REVIEW_REQUIRED"):
        registry.confirm_source(path, "Keka Example", by="QA", note="x")
    with pytest.raises(ValueError, match="no source that could be confirmed"):
        registry.confirm_source(path, "Bad Token Example", by="QA", note="x")
    with pytest.raises(LookupError):
        registry.confirm_source(path, "Unknown Example", by="QA", note="x")
    for by, note in (("", "x"), ("QA", "   "), ("QA", "a\x00b"), ("QA", "n" * 501)):
        with pytest.raises(ValueError):
            registry.authorize(
                path, "Name Only Example", Authorization.RESTRICTED, by=by, note=note
            )


def test_explicit_approval_exports_and_restriction_excludes(tmp_path: Path) -> None:
    acme = board_company("Acme Example", "acmeexample")
    beta = make("Beta Example", 3, "https://jobs.lever.co/beta-example", "https://www.example.org/")
    path = verified_state(tmp_path, acme, beta)
    registry.authorize(path, "Acme Example", Authorization.APPROVED, by="Ops", note="permission A")
    registry.authorize(path, "Beta Example", Authorization.APPROVED, by="Ops", note="permission B")
    registry.authorize(path, "Beta Example", Authorization.RESTRICTED, by="Legal", note="revoked")

    export = registry.approved_sources(registry.load_state(path))
    assert export.sources == (
        Source("greenhouse", "Acme Example", "acmeexample", "https://www.example.com/"),
    )
    assert [(r.company_name, r.authorization_status, r.reason) for r in export.review] == [
        ("Beta Example", "RESTRICTED", "collection is not approved (RESTRICTED)")
    ]


def test_approval_is_bound_to_the_approved_source(tmp_path: Path) -> None:
    acme = board_company("Acme Example", "acmeexample")
    path = verified_state(tmp_path, acme)
    registry.authorize(path, "Acme Example", Authorization.APPROVED, by="Ops", note="ok")
    state = registry.load_state(path)
    state.companies["acme example"]["identifier"] = "another-board"  # e.g. a later re-verification
    assert registry.approved_sources(state).sources == ()
    assert registry.coverage(state).pending == 1


def test_a_source_claimed_by_two_companies_is_exported_for_neither(tmp_path: Path) -> None:
    path = verified_state(
        tmp_path,
        board_company("Acme Example", "SharedBoard"),
        make(
            "Acme Example Two",
            3,
            "https://boards.greenhouse.io/sharedboard",
            "https://www.example.org/",
        ),
    )
    for name in ("Acme Example", "Acme Example Two"):
        registry.authorize(path, name, Authorization.APPROVED, by="Ops", note="ok")
    export = registry.approved_sources(registry.load_state(path))
    assert export.sources == ()
    assert {r.reason for r in export.review} == {"the same source is claimed by another company"}


# --- approved sources CSV ---------------------------------------------------------------------


def test_generated_sources_file_loads_with_the_collector_source_loader(tmp_path: Path) -> None:
    companies = [
        board_company('Acme, Example "Quoted"', "acmeexample"),
        make("Lever Example", 3, "https://jobs.lever.co/lever-example", "https://www.example.org/"),
        make(
            "Ashby Example", 4, "https://jobs.ashbyhq.com/AshbyExample", "https://www.example.net/"
        ),
        make("JsonLd Example", 5, "https://www.example.net/careers", "https://www.example.net/"),
    ]
    path = verified_state(tmp_path, *companies)
    for entry in companies:
        registry.authorize(path, entry.name, Authorization.APPROVED, by="Ops", note="ok")
    export = registry.approved_sources(registry.load_state(path))
    output, review = tmp_path / "approved.csv", tmp_path / "review.csv"
    registry.write_source_files(export, output, review)

    loaded = load_sources(output)
    assert loaded == list(export.sources)
    assert [(s.platform, s.identifier) for s in loaded] == [
        ("greenhouse", "acmeexample"),
        ("lever", "lever-example"),
        ("ashby", "AshbyExample"),
        ("jsonld", "https://www.example.net/careers"),
    ]
    assert review.read_text(encoding="utf-8").splitlines() == [",".join(registry.REVIEW_COLUMNS)]
    with pytest.raises(FileExistsError):
        registry.write_source_files(export, output, review)
    registry.write_source_files(export, output, review, overwrite=True)


def test_no_approved_sources_writes_only_the_review_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    registry.run_batch(synthetic(2), path, batch_size=2)
    export = registry.approved_sources(registry.load_state(path))
    output, review = tmp_path / "approved.csv", tmp_path / "review.csv"
    registry.write_source_files(export, output, review)
    assert not output.exists()
    lines = review.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and lines[1].startswith("Synthetic Company 0000,NOT_FOUND,PENDING,,,")


# --- coverage ---------------------------------------------------------------------------------


def test_coverage_counts_come_only_from_stored_results(tmp_path: Path) -> None:
    acme = board_company("Acme Example", "acmeexample")
    beta = make("Beta Example", 3, "https://jobs.lever.co/beta-example", "https://www.example.org/")
    keka = make("Keka Example", 4, "https://acme.keka.com/careers", "https://www.example.net/")
    path = tmp_path / "state.json"
    registry.run_batch([acme, beta, keka, *synthetic(2)], path, batch_size=10, now=NOW)
    registry.confirm_source(path, "Acme Example", by="QA", note="ok", now=NOW)
    registry.authorize(path, "Acme Example", Authorization.APPROVED, by="Ops", note="ok")
    registry.authorize(path, "Keka Example", Authorization.RESTRICTED, by="Ops", note="no")

    keys = [c.key for c in (acme, beta, keka, *synthetic(2), make("Not Yet Processed"))]
    cov = registry.coverage(registry.load_state(path), keys)
    assert (cov.companies, cov.processed) == (6, 5)
    assert (cov.verified, cov.review_required, cov.not_found, cov.unsupported) == (1, 1, 2, 1)
    assert (cov.approved, cov.restricted, cov.pending) == (1, 1, 3)
    assert cov.crawler_ready == 1 and cov.failures == 0
    assert cov.discovery_coverage == pytest.approx(1 / 6)
    assert cov.crawler_ready_coverage == pytest.approx(1 / 6)


# --- CLI ----------------------------------------------------------------------------------------


def write_companies(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "companies.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_cli_workflow_from_company_list_to_approved_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: None, no_crawl: None
) -> None:
    monkeypatch.chdir(tmp_path)
    companies = write_companies(
        tmp_path,
        HEADER
        + "Acme Example,https://www.example.com,https://boards.greenhouse.io/acmeexample\n"
        + "Beta Example,,\n"
        + "Keka Example,https://www.example.net,https://acme.keka.com/careers\n",
    )
    state = tmp_path / "state.json"

    result = runner.invoke(
        app, ["discover", "--companies", str(companies), "--state", str(state), "--batch-size", "2"]
    )
    assert result.exit_code == 0, result.output
    assert "MODE: offline" in result.output
    assert "REVIEW_REQUIRED: Acme Example [greenhouse]" in result.output
    assert "NOT_FOUND: Beta Example" in result.output
    assert "BATCH: 2 processed (0 verified, 1 review_required, 1 not_found, 0 unsupported)" in (
        result.output
    )
    assert "1 still due" in result.output
    assert "COVERAGE: discovery 0/3 (0.0%), crawler-ready 0/3 (0.0%)" in result.output

    result = runner.invoke(app, ["discover", "--companies", str(companies), "--state", str(state)])
    assert "UNSUPPORTED: Keka Example [keka]" in result.output

    approve = ["sources", "authorize", "Acme Example", "--decision", "approved"]
    approve += ["--by", "Ops", "--note", "written permission", "--state", str(state)]
    refused = runner.invoke(app, approve)
    assert refused.exit_code == 2 and "only VERIFIED" in refused.output

    verify = ["sources", "verify", "acme example", "--by", "QA", "--evidence", "careers page link"]
    result = runner.invoke(app, [*verify, "--state", str(state)])
    assert result.exit_code == 0 and "VERIFIED: Acme Example [greenhouse]" in result.output
    assert runner.invoke(app, approve).exit_code == 0

    output = tmp_path / "approved_sources.csv"
    result = runner.invoke(
        app, ["sources", "export", "--output", str(output), "--state", str(state)]
    )
    assert result.exit_code == 0, result.output
    assert "1 approved sources written" in result.output
    assert load_sources(output) == [
        Source("greenhouse", "Acme Example", "acmeexample", "https://www.example.com/")
    ]
    review = (tmp_path / "approved_sources_review.csv").read_text(encoding="utf-8")
    assert "Beta Example,NOT_FOUND,PENDING" in review and "Keka Example,UNSUPPORTED" in review

    status = runner.invoke(
        app, ["sources", "status", "--companies", str(companies), "--state", str(state)]
    )
    assert status.exit_code == 0
    assert "AUTHORIZATION: 1 approved, 2 pending, 0 restricted" in status.output
    assert "COVERAGE: discovery 1/3 (33.3%), crawler-ready 1/3 (33.3%)" in status.output

    # Nothing was crawled and no database or Excel file appeared.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "approved_sources.csv",
        "approved_sources_review.csv",
        "companies.csv",
        "state.json",
    ]


def test_cli_rejects_unusable_input_safely(tmp_path: Path, no_network: None) -> None:
    state = tmp_path / "state.json"

    def discover(text: str) -> object:
        path = write_companies(tmp_path, text)
        return runner.invoke(app, ["discover", "--companies", str(path), "--state", str(state)])

    empty = discover(HEADER)
    assert empty.exit_code == 2 and "no usable companies" in empty.output
    unknown = discover("company_name,secret_column\nAcme,x\n")
    assert unknown.exit_code == 2 and "unexpected column" in unknown.output
    leaky = discover(
        HEADER + "Acme Example,https://admin:hunter2@www.example.com,\nBeta Example,,\n"
    )
    assert leaky.exit_code == 1  # processed, but a row was rejected
    assert "line 2: rejected: official_website: contains credentials" in leaky.output
    assert "hunter2" not in leaky.output and "hunter2" not in state.read_text(encoding="utf-8")
    missing = runner.invoke(app, ["discover", "--companies", str(tmp_path / "missing.csv")])
    assert missing.exit_code == 2
    bad_size = runner.invoke(
        app, ["discover", "--companies", str(tmp_path / "companies.csv"), "--batch-size", "0"]
    )
    assert bad_size.exit_code == 2

    state.write_text("{broken", encoding="utf-8")
    broken = discover(HEADER + "Acme Example,,\n")
    assert broken.exit_code == 3 and "cannot read the discovery state" in broken.output
    assert state.read_text(encoding="utf-8") == "{broken"


def test_cli_export_with_nothing_approved_exits_1(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    registry.run_batch(synthetic(1), state, batch_size=1)
    output = tmp_path / "out.csv"
    result = runner.invoke(
        app, ["sources", "export", "--output", str(output), "--state", str(state)]
    )
    assert result.exit_code == 1 and "No approved sources" in result.output
    assert not output.exists() and (tmp_path / "out_review.csv").exists()
    again = runner.invoke(
        app, ["sources", "export", "--output", str(output), "--state", str(state)]
    )
    assert again.exit_code == 2 and "already exists" in again.output


def test_cli_lists_new_and_existing_commands() -> None:
    result = runner.invoke(app, ["--help"])
    for command in ("run", "export", "report", "discover", "sources"):
        assert command in result.output
    sources_help = runner.invoke(app, ["sources", "--help"])
    for command in ("status", "verify", "authorize", "export"):
        assert command in sources_help.output


def test_sqlite_schema_is_unchanged() -> None:
    assert SCHEMA_VERSION == 2
