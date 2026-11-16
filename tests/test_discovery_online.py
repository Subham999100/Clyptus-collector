"""Phase 6B: website lookup, online discovery and the protected Scrapy fetch path.

Offline. Directory results are synthetic SPARQL JSON, websites are reserved ``example.*``
domains and board tokens are made up. The Scrapy scenarios run the real engine, middlewares and
settings in a subprocess with a scripted download handler and sockets blocked.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from collector.companies import registry
from collector.companies.directory import WikidataDirectory, get_directory, parse_candidates
from collector.companies.discovery import (
    Candidate,
    FetchError,
    Page,
    Redirected,
    Status,
    discover,
    drive,
)
from collector.companies.input import Company, company_key

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
WIKIDATA = WikidataDirectory()
HOME = "https://www.example.com/"
CAREERS = "https://www.example.com/careers"


def company(name: str = "Acme Example", website: str | None = None, line: int = 2) -> Company:
    return Company(name, company_key(name), line, website)


def sparql(*entries: dict[str, Any]) -> str:
    """A SPARQL JSON result: one binding per (item, label, alias, website) combination."""
    bindings = []
    for entry in entries:
        item = {"type": "uri", "value": f"http://www.wikidata.org/entity/{entry['qid']}"}
        for website in entry.get("websites", []):
            for alias in entry.get("aliases", [None]):
                binding = {"item": item, "website": {"type": "uri", "value": website}}
                if entry.get("label"):
                    binding["label"] = {"type": "literal", "value": entry["label"]}
                if alias:
                    binding["alias"] = {"type": "literal", "value": alias}
                bindings.append(binding)
    return json.dumps(
        {"head": {"vars": ["item", "label", "alias", "website"]}, "results": {"bindings": bindings}}
    )


class FakeWeb:
    def __init__(self, pages: dict[str, Page | Exception]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url: str) -> Page:
        self.calls.append(url)
        page = self.pages.get(url, FetchError("HTTP 404"))
        if isinstance(page, Exception):
            raise page
        return page


def lookup(name: str, result: str) -> tuple[str, Page]:
    url = WIKIDATA.query_url(name)
    return url, Page(url, result)


def html(title: str, body: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


# --- the directory ----------------------------------------------------------------------------


def test_query_targets_the_public_endpoint_and_escapes_the_name() -> None:
    url = WIKIDATA.query_url('Acme "Quoted" \\ Example')
    parts = urlsplit(url)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "query.wikidata.org", "/sparql")
    params = parse_qs(parts.query)
    assert params["format"] == ["json"]
    (query,) = params["query"]
    assert 'mwapi:search "Acme \\"Quoted\\" \\\\ Example";' in query
    assert "wdt:P856" in query  # the official-website property


def test_only_exact_label_or_alias_matches_with_https_websites_are_candidates() -> None:
    result = sparql(
        {"qid": "Q900", "label": "Acme Example", "websites": ["https://www.example.com/"]},
        {"qid": "Q901", "label": "Acme Example Labs", "websites": ["https://labs.example.org/"]},
        {"qid": "Q902", "label": "ACME  example", "websites": ["http://old.example.net/"]},
        {"qid": "Q903", "label": "Acme Example", "websites": ["https://localhost/"]},
        {
            "qid": "Q904",
            "label": "Acme",
            "aliases": ["Acme Ex"],
            "websites": ["https://x.example/"],
        },
    )
    assert parse_candidates(result, "acme example") == (
        Candidate("https://www.example.com/", ("Acme Example",), "Wikidata Q900"),
    )


def test_an_acronym_matches_only_as_a_listed_alias() -> None:
    result = sparql(
        {
            "qid": "Q77",
            "label": "Acme Example Corporation",
            "aliases": ["AXC", "Acme Corp"],
            "websites": ["https://www.example.com/"],
        }
    )
    assert parse_candidates(result, "AXC") == (
        Candidate("https://www.example.com/", ("Acme Example Corporation", "AXC"), "Wikidata Q77"),
    )
    assert parse_candidates(result, "AX") == ()  # no partial or fuzzy matching


@pytest.mark.parametrize(
    "text", ["{not json", "[]", '{"results": {}}', '{"results": {"bindings": 3}}']
)
def test_unusable_directory_responses_are_fetch_errors(text: str) -> None:
    with pytest.raises(FetchError):
        parse_candidates(text, "Acme Example")


def test_directory_is_optional_and_configurable() -> None:
    assert get_directory("none") is None
    assert isinstance(get_directory("wikidata"), WikidataDirectory)
    with pytest.raises(ValueError, match="unknown website directory"):
        get_directory("paid-search")


# --- discovery with a directory -----------------------------------------------------------------


def test_name_only_company_is_verified_from_an_exact_directory_match() -> None:
    url, result = lookup(
        "Acme Example",
        sparql({"qid": "Q900", "label": "Acme Example", "websites": [HOME]}),
    )
    web = FakeWeb(
        {
            url: result,
            HOME: Page(
                HOME, html("Acme Example", '<a href="https://jobs.lever.co/acme-ex">Careers</a>')
            ),
        }
    )
    found = discover(company(), web, directory=WIKIDATA, now=NOW)
    assert (found.status, found.platform, found.identifier) == (Status.VERIFIED, "lever", "acme-ex")
    assert found.official_website == HOME
    assert found.evidence[0] == (
        "official website listed by wikidata (Wikidata Q900) under exactly this name"
    )
    assert web.calls == [url, HOME]  # the job board itself is never fetched


def test_acronym_identity_is_confirmed_through_the_directory_entry_name() -> None:
    url, result = lookup(
        "AXC",
        sparql(
            {
                "qid": "Q77",
                "label": "Acme Example Corporation",
                "aliases": ["AXC"],
                "websites": [HOME],
            }
        ),
    )
    careers = '<a href="https://boards.greenhouse.io/acmeexample">Join us</a>'
    named = FakeWeb({url: result, HOME: Page(HOME, html("Acme Example Corporation", careers))})
    assert discover(company("AXC"), named, directory=WIKIDATA, now=NOW).status == Status.VERIFIED

    other = FakeWeb({url: result, HOME: Page(HOME, html("Unrelated Example Shop", careers))})
    found = discover(company("AXC"), other, directory=WIKIDATA, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert found.reason == "official website does not name the company"


def test_similarly_named_entries_on_different_domains_need_review() -> None:
    url, result = lookup(
        "Acme Example",
        sparql(
            {"qid": "Q900", "label": "Acme Example", "websites": [HOME]},
            {"qid": "Q901", "aliases": ["Acme Example"], "label": "Acme Example GmbH",
             "websites": ["https://www.example.de/"]},
        ),
    )  # fmt: skip
    web = FakeWeb({url: result})
    found = discover(company(), web, directory=WIKIDATA, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert found.reason == (
        "website lookup (wikidata) found official websites on 2 different domains "
        "(Wikidata Q900, Wikidata Q901)"
    )
    assert found.official_website is None and web.calls == [url]  # no site was fetched


def test_no_exact_match_is_not_found_and_lookup_failure_is_a_failure() -> None:
    url, result = lookup(
        "Acme Example", sparql({"qid": "Q1", "label": "Acme Example Holdings", "websites": [HOME]})
    )
    found = discover(company(), FakeWeb({url: result}), directory=WIKIDATA, now=NOW)
    assert found.status == Status.NOT_FOUND and found.official_website is None
    assert "found no entry with exactly this name" in (found.reason or "")

    failed = discover(
        company(), FakeWeb({url: FetchError("HTTP 503")}), directory=WIKIDATA, now=NOW
    )
    assert (failed.status, failed.failed) == (Status.REVIEW_REQUIRED, True)
    assert failed.reason == "website lookup (wikidata) failed (HTTP 503)"


def test_no_lookup_offline_or_when_a_website_was_supplied() -> None:
    offline = discover(company(), None, directory=WIKIDATA, now=NOW)
    assert offline.status == Status.NOT_FOUND
    assert offline.reason == "no official website supplied; website lookup needs online mode"

    web = FakeWeb({HOME: Page(HOME, html("Acme Example"))})
    discover(company(website=HOME), web, directory=WIKIDATA, now=NOW)
    assert web.calls == [HOME]  # the supplied website is used; nothing is looked up


# --- redirects as the protected fetcher reports them ------------------------------------------


def test_same_domain_redirect_is_requested_as_its_own_fetch() -> None:
    apex = "https://example.com/"
    web = FakeWeb(
        {
            apex: Redirected(HOME),
            HOME: Page(
                HOME, html("Acme Example", '<a href="https://jobs.lever.co/acme-ex">Jobs</a>')
            ),
        }
    )
    found = discover(company(website=apex), web, now=NOW)
    assert found.status == Status.VERIFIED and web.calls == [apex, HOME]


def test_redirects_off_the_official_domain_are_judged_not_fetched() -> None:
    elsewhere = FakeWeb({HOME: Redirected("https://www.example.org/")})
    found = discover(company(website=HOME), elsewhere, now=NOW)
    assert found.reason == "official website redirected to a different domain"
    assert elsewhere.calls == [HOME]

    to_board = FakeWeb(
        {
            HOME: Page(HOME, html("Acme Example", '<a href="/careers">Careers</a>')),
            CAREERS: Redirected("https://jobs.ashbyhq.com/AcmeExample"),
        }
    )
    found = discover(company(website=HOME), to_board, now=NOW)
    assert (found.status, found.platform, found.identifier) == (
        Status.VERIFIED,
        "ashby",
        "AcmeExample",
    )
    assert found.careers_url == CAREERS
    assert "official careers page redirects to the ashby board" in found.evidence
    assert to_board.calls == [HOME, CAREERS]

    unrelated = FakeWeb(
        {
            HOME: Page(HOME, html("Acme Example", '<a href="/careers">Careers</a>')),
            CAREERS: Redirected("https://jobs.example.org/acme"),
        }
    )
    found = discover(company(website=HOME), unrelated, now=NOW)
    assert found.reason == "careers page redirected to a different domain"


def test_only_one_same_domain_redirect_is_followed() -> None:
    apex = "https://example.com/"
    web = FakeWeb({apex: Redirected(HOME), HOME: Redirected("https://shop.example.com/")})
    found = discover(company(website=apex), web, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED and web.calls == [apex, HOME]


def test_plain_http_careers_links_are_not_fetched() -> None:
    web = FakeWeb(
        {
            HOME: Page(
                HOME, html("Acme Example", '<a href="http://www.example.com/careers">Careers</a>')
            )
        }
    )
    found = discover(company(website=HOME), web, now=NOW)
    assert found.status == Status.NOT_FOUND and web.calls == [HOME]


def test_offline_drive_never_fetches() -> None:
    with pytest.raises(RuntimeError):
        drive(iter(["https://www.example.com/"]), None)  # type: ignore[arg-type]


# --- batches through a driver -------------------------------------------------------------------


def fake_driver(web: FakeWeb, stop_after: int | None = None):  # noqa: ANN201
    """Runs the steps like crawl_discovery does, with the fake web instead of Scrapy."""

    def run(jobs: Any, on_done: Any) -> None:
        for done, (key, steps) in enumerate(jobs):
            if stop_after is not None and done == stop_after:
                raise KeyboardInterrupt
            try:
                result: Any = drive(steps, web)
            except Exception as exc:  # noqa: BLE001
                result = exc
            on_done(key, result)

    return run


@pytest.mark.parametrize("size", [10, 20, 50, 100])
def test_online_batches_of_name_only_companies(tmp_path: Path, size: int) -> None:
    names = [f"Synthetic Company {i:04d}" for i in range(150)]
    pages: dict[str, Page | Exception] = {}
    for i, name in enumerate(names):
        site = f"https://www.example.com/c{i}/"
        url, result = lookup(name, sparql({"qid": f"Q{i + 1}", "label": name, "websites": [site]}))
        pages[url] = result
        pages[site] = Page(site, html(name, f'<a href="https://jobs.lever.co/c{i}">Careers</a>'))
    web = FakeWeb(pages)
    companies = [company(name, line=i + 2) for i, name in enumerate(names)]
    path = tmp_path / "state.json"

    first = registry.run_batch(
        companies, path, batch_size=size, directory=WIKIDATA, driver=fake_driver(web), now=NOW
    )
    assert [c.name for c, _ in first.processed] == names[:size]
    assert {r["status"] for _, r in first.processed} == {"VERIFIED"}
    assert first.remaining == 150 - size
    calls = len(web.calls)
    second = registry.run_batch(
        companies[:size], path, batch_size=size, directory=WIKIDATA, driver=fake_driver(web)
    )
    assert second.processed == () and len(web.calls) == calls  # unchanged: nothing re-fetched
    state = registry.load_state(path)
    assert state.authorizations == {}  # discovery never approves anything
    assert (
        registry.coverage(state).pending == size and registry.approved_sources(state).sources == ()
    )


def test_interrupted_online_batch_keeps_finished_companies(tmp_path: Path) -> None:
    names = [f"Synthetic Company {i}" for i in range(5)]
    path = tmp_path / "state.json"
    web = FakeWeb({})  # every lookup fails: recorded as failures, not crashes
    with pytest.raises(KeyboardInterrupt):
        registry.run_batch(
            [company(n, line=i + 2) for i, n in enumerate(names)],
            path,
            batch_size=5,
            directory=WIKIDATA,
            driver=fake_driver(web, stop_after=3),
        )
    saved = registry.load_state(path).companies
    assert len(saved) == 3 and all(r["failed"] for r in saved.values())


def test_cli_online_mode_uses_the_protected_driver_and_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from collector.cli import app

    seen: dict[str, Any] = {}

    def driver(jobs: Any, on_done: Any) -> None:
        seen["jobs"] = [key for key, _ in jobs]
        for key, _ in jobs:
            on_done(key, FetchError("offline test"))

    monkeypatch.setattr("collector.cli.crawl_discovery", driver)
    companies = tmp_path / "companies.csv"
    companies.write_text("company_name\nAcme Example\n", encoding="utf-8")
    state = tmp_path / "state.json"
    args = ["discover", "--companies", str(companies), "--state", str(state)]

    result = CliRunner().invoke(app, [*args, "--online", "--provider", "wikidata"])
    assert result.exit_code == 0, result.output
    assert "MODE: online" in result.output and "website lookup: wikidata" in result.output
    assert seen["jobs"] == ["acme example"]
    assert "discovery failed (FetchError)" in result.output

    bad = CliRunner().invoke(app, [*args, "--provider", "paid-search"])
    assert bad.exit_code == 2 and "unknown website directory" in bad.output


# --- the real Scrapy engine (subprocess, scripted responses, sockets blocked) ---------------


def run_discovery(
    tmp_path: Path, discovery: dict[str, Any], responses: dict[str, Any]
) -> dict[str, Any]:
    scenario = tmp_path / "scenario.json"
    discovery = {"folder": str(tmp_path), **discovery}
    scenario.write_text(
        json.dumps({"discovery": discovery, "responses": responses}), encoding="utf-8"
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COLLECTOR_")}
    environment.update(COLLECTOR_DOWNLOAD_DELAY_SECONDS="0", COLLECTOR_AUTOTHROTTLE_ENABLED="false")
    completed = subprocess.run(
        [sys.executable, "-c", "from tests.crawl_harness import main; main()", str(scenario)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    line = next(x for x in completed.stdout.splitlines() if x.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


HTML = {"Content-Type": "text/html"}


def test_protected_crawl_discovers_a_board_without_fetching_it(tmp_path: Path) -> None:
    secret = "SECRET-TOKEN"
    result = run_discovery(
        tmp_path,
        {
            "companies": "company_name,official_website\n"
            "Acme Example,\n"  # name only: looked up
            "Blocked Example,https://www.example.org\n"
            "Robots Example,https://www.example.net\n"
            "Private Example,https://shop.example.net\n",
            "wikidata": {
                "Acme Example": json.loads(
                    sparql(
                        {
                            "qid": "Q900",
                            "label": "Acme Example",
                            "websites": ["https://example.com/"],
                        }
                    )
                )
            },
        },
        {
            "https://example.com/": {"status": 301, "headers": {"Location": HOME}},
            HOME: {"headers": HTML, "body": html("Acme Example", '<a href="/careers">Careers</a>')},
            CAREERS: {
                "status": 302,
                "headers": {"Location": f"https://boards.greenhouse.io/acmeexample?t={secret}"},
            },
            "https://www.example.org/": {"status": 403, "headers": HTML, "body": "denied"},
            "https://www.example.net/robots.txt": {
                "headers": {"Content-Type": "text/plain"},
                "body": "User-agent: *\nDisallow: /\n",
            },
            "https://shop.example.net/": {
                "status": 302,
                "headers": {"Location": "https://10.0.0.5/"},
            },
        },
    )
    records = result["records"]
    acme = records["Acme Example"]
    assert (acme["status"], acme["platform"], acme["identifier"]) == (
        "VERIFIED",
        "greenhouse",
        "acmeexample",
    )
    assert acme["official_website"] == "https://example.com/"
    assert acme["careers_url"] == CAREERS
    blocked = records["Blocked Example"]
    assert (blocked["status"], blocked["failed"]) == ("REVIEW_REQUIRED", True)
    assert blocked["reason"] == "official website could not be fetched (blocked: HTTP 403)"
    robots = records["Robots Example"]
    assert robots["reason"] == "official website could not be fetched (refused by robots.txt)"
    private = records["Private Example"]
    assert private["reason"] == "official website redirected to a different domain"

    requested = result["requests"]
    hosts = {urlsplit(url).hostname for url in requested}
    # Redirect targets on other hosts (the job board, a private address) were never requested,
    # robots.txt was read for every host, and a disallowed page was never fetched.
    assert hosts == {
        "query.wikidata.org",
        "example.com",
        "www.example.com",
        "www.example.org",
        "www.example.net",
        "shop.example.net",
    }
    assert "https://www.example.net/" not in requested
    for host in hosts:
        assert f"https://{host}/robots.txt" in requested
    assert all(urlsplit(url).scheme == "https" for url in requested)
    assert result["authorizations"] == {}  # never approved automatically
    assert secret not in json.dumps(records)
