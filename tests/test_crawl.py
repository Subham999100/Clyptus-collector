"""Crawl behaviour through the real Scrapy engine, with a scripted download handler (no network).

Each scenario runs ``tests/crawl_harness.py`` in its own subprocess, because the Twisted reactor
can start only once per process. Settings are the real ``scrapy_settings`` ones, except that
the download delay is zero (so the suite is fast) unless a test is about the delay.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from collector.spiders.run import USER_AGENT

ROOT = Path(__file__).parent.parent
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>"
HTML = {"headers": {"Content-Type": "text/html"}}


def gh(board: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


def lv(site: str) -> str:
    return f"https://api.lever.co/v0/postings/{site}?mode=json"


def ab(board: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{board}"


def src(platform: str, identifier: str) -> dict[str, str]:
    return {"platform": platform, "company_name": f"Company {identifier}", "identifier": identifier}


def crawl(
    tmp_path: Path,
    sources: list[dict[str, str]],
    responses: dict[str, dict[str, Any]],
    **env: str,
) -> dict[str, Any]:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"sources": sources, "responses": responses}), encoding="utf-8")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COLLECTOR_")}
    environment.update(COLLECTOR_DOWNLOAD_DELAY_SECONDS="0", COLLECTOR_AUTOTHROTTLE_ENABLED="false")
    environment.update(env)
    completed = subprocess.run(
        [sys.executable, "-c", "from tests.crawl_harness import main; main()", str(scenario)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    line = next(x for x in completed.stdout.splitlines() if x.startswith("RESULT:"))
    result = json.loads(line.removeprefix("RESULT:"))
    result["by_id"] = {r["identifier"]: r for r in result["results"]}
    result["stderr"] = completed.stderr
    return result


def attempts(result: dict[str, Any], url: str) -> int:
    return sum(1 for r in result["requests"] if r["url"] == url)


def test_mixed_sources_are_isolated_and_each_outcome_is_reported(tmp_path: Path) -> None:
    challenge_page = "https://jobs.example.org/challenge"
    ok_page = "https://jobs.example.org/ok"
    sources = [
        src("greenhouse", "gh-ok"),
        src("lever", "lv-403"),
        src("ashby", "ab-429"),
        {**src("jsonld", "x"), "identifier": challenge_page},
        src("greenhouse", "gh-bad"),
        src("lever", "lv-ok"),
        src("ashby", "ab-503"),
        {**src("jsonld", "x"), "identifier": ok_page},
        src("greenhouse", "gh-timeout"),
        src("greenhouse", "gh-empty"),
    ]
    responses = {
        gh("gh-ok"): {"fixture": "greenhouse/list_complete.json"},
        lv("lv-403"): {"status": 403, "body": "SECRET-PAYLOAD", **HTML},
        ab("ab-429"): {"status": 429, "body": "SECRET-PAYLOAD"},
        challenge_page: {"body": CHALLENGE, **HTML},
        gh("gh-bad"): {"body": "{broken SECRET-PAYLOAD"},
        lv("lv-ok"): {"fixture": "lever/postings_complete.json"},
        ab("ab-503"): {"status": 503, "body": "SECRET-PAYLOAD"},
        ok_page: {"fixture": "jsonld/single_job_posting.html", **HTML},
        gh("gh-timeout"): {"error": "timeout"},
        gh("gh-empty"): {"fixture": "greenhouse/list_empty.json"},
    }

    result = crawl(tmp_path, sources, responses)
    by_id = result["by_id"]

    # Healthy sources are unaffected by the failing ones around them.
    assert by_id["gh-ok"]["records"] == 1 and by_id["gh-ok"]["fetch_failures"] == []
    assert by_id["lv-ok"]["records"] == 1 and by_id["lv-ok"]["fetch_failures"] == []
    assert by_id[ok_page]["records"] == 1
    # A genuinely empty listing is a successful, empty extraction.
    empty = by_id["gh-empty"]
    assert (empty["records"], empty["extractions"], empty["fetch_failures"]) == (0, 1, [])
    # HTTP 403 and 429: blocked, recorded, not retried, and not an empty listing.
    for key, status in (("lv-403", 403), ("ab-429", 429)):
        blocked = by_id[key]
        assert blocked["blocked"] is True and blocked["extractions"] == 0
        assert blocked["fetch_failures"][0]["reason"] == f"HTTP {status}"
    assert attempts(result, lv("lv-403")) == 1
    assert attempts(result, ab("ab-429")) == 1
    # A 200 challenge page is blocked as well, never an empty listing.
    challenge = by_id[challenge_page]
    assert challenge["blocked"] is True and challenge["extractions"] == 0
    assert challenge["fetch_failures"][0]["reason"] == "access challenge page"
    # Malformed JSON is an extraction failure, not a crash and not a transport failure.
    bad = by_id["gh-bad"]
    assert bad["extraction_failures"] == ["invalid_response"] and bad["fetch_failures"] == []
    # Server errors and timeouts are retried RETRY_TIMES (2) times, then reported (not blocked).
    for url, key, reason in (
        (ab("ab-503"), "ab-503", "HTTP 503"),
        (gh("gh-timeout"), "gh-timeout", "TimeoutError"),
    ):
        assert attempts(result, url) == 3
        (failure,) = by_id[key]["fetch_failures"]
        assert failure["reason"] == reason and failure["blocked"] is False
    # Each healthy source's initial request exists exactly once (no duplicate start requests).
    for url in (gh("gh-ok"), lv("lv-ok"), gh("gh-empty"), gh("gh-bad")):
        assert attempts(result, url) == 1
    # Politeness: descriptive UA and the configured timeout on every request, robots.txt asked.
    assert {r["user_agent"] for r in result["requests"]} == {USER_AGENT}
    assert {r["timeout"] for r in result["requests"]} == {20}
    assert attempts(result, "https://boards-api.greenhouse.io/robots.txt") >= 1
    # Nothing from any response body reaches the logs or the reports.
    assert "SECRET-PAYLOAD" not in result["stderr"]
    assert "SECRET-PAYLOAD" not in json.dumps(result["results"])
    # One request at a time, and no listening server (telnet console, remote control) opened.
    assert result["max_in_flight"] == 1
    assert "listening on" not in result["stderr"].lower()


def test_robots_txt_denial_is_honoured_and_isolated(tmp_path: Path) -> None:
    result = crawl(
        tmp_path,
        [src("greenhouse", "gh-denied"), src("lever", "lv-fine")],
        {
            "https://boards-api.greenhouse.io/robots.txt": {
                "body": "User-agent: *\nDisallow: /\n",
                "headers": {"Content-Type": "text/plain"},
            },
            lv("lv-fine"): {"fixture": "lever/postings_complete.json"},
        },
    )

    assert attempts(result, gh("gh-denied")) == 0  # never requested
    denied = result["by_id"]["gh-denied"]
    assert denied["extractions"] == 0
    assert [f["reason"] for f in denied["fetch_failures"]] == ["IgnoreRequest"]
    assert result["by_id"]["lv-fine"]["records"] == 1


def test_unsafe_redirect_destinations_are_never_requested(tmp_path: Path) -> None:
    """Every case redirects the source's listing URL somewhere; the destination is scripted to
    answer with a valid listing, so a request to it would show up as records or a request."""
    secret = "SECRET-QUERY"
    unsafe = {  # source identifier -> redirect destination
        "downgrade": f"http://api.lever.co/v0/postings/downgrade?mode=json&t={secret}",
        "subdomain": f"https://sub.api.lever.co/v0/postings/subdomain?t={secret}",
        "other-host": f"https://evil.example.net/x?t={secret}",
        "other-port": f"https://api.lever.co:8443/v0/postings/other-port?t={secret}",
        "credentials": "https://user:SECRET-PW@api.lever.co/v0/postings/credentials-final",
        "localhost": f"https://localhost/x?t={secret}",
        "loopback": f"https://127.0.0.1/x?t={secret}",
        "ipv6-loopback": f"https://[::1]/x?t={secret}",
        "private": f"https://10.0.0.5/x?t={secret}",
        "metadata": f"https://169.254.169.254/latest?t={secret}",
        "file": "file:///etc/passwd",
        "ftp": "ftp://api.lever.co/x",
    }
    ok_listing = {"fixture": "lever/postings_complete.json"}
    responses: dict[str, dict[str, Any]] = {}
    for name, target in unsafe.items():
        responses[lv(name)] = {"status": 302, "headers": {"Location": target}}
        responses[target] = ok_listing  # a request to the destination would succeed
        responses[target.replace("user:SECRET-PW@", "")] = ok_listing
    # Valid same-host https redirect (default port spelled out) and a bounded chain.
    responses[lv("fine")] = {"status": 301, "headers": {"Location": lv("fine-final")}}
    responses[lv("fine-final")] = ok_listing
    responses[lv("fine-443")] = {
        "status": 301,
        "headers": {"Location": "https://API.lever.co:443/v0/postings/fine-443-final?mode=json"},
    }
    # Scrapy lower-cases the host of the redirected request, so that is the URL fetched.
    responses["https://api.lever.co:443/v0/postings/fine-443-final?mode=json"] = ok_listing
    hop = "https://api.lever.co/v0/postings/chain?mode=json&hop="
    responses[lv("chain")] = {"status": 302, "headers": {"Location": hop + "1"}}
    for n in range(1, 9):
        responses[f"{hop}{n}"] = {"status": 302, "headers": {"Location": f"{hop}{n + 1}"}}
    sources = [src("lever", name) for name in [*unsafe, "fine", "fine-443", "chain"]]

    result = crawl(tmp_path, sources, responses)
    by_id = result["by_id"]
    requested = [r["url"] for r in result["requests"]]

    # Nothing but HTTPS to the one configured host and port ever reached the downloader.
    parts = [urlsplit(u) for u in requested]
    assert {(p.scheme, p.hostname, p.port or 443, p.username) for p in parts} == {
        ("https", "api.lever.co", 443, None)
    }
    assert not any(r["credentials"] for r in result["requests"])
    # Each rejected redirect is a failed source: reported, never a (silent or empty) success.
    for name in unsafe:
        outcome = by_id[name]
        assert outcome["records"] == 0 and outcome["extractions"] == 0, name
        assert len(outcome["fetch_failures"]) == 1 and outcome["blocked"] is False, name
    # Reports and logs carry neither query strings nor credentials.
    assert secret not in json.dumps(result["results"]) + result["stderr"]
    assert "SECRET-PW" not in json.dumps(result["results"]) + result["stderr"]
    # Valid same-host https redirects are followed.
    for name in ("fine", "fine-443"):
        assert by_id[name]["records"] == 1 and by_id[name]["fetch_failures"] == [], name
    # The chain is bounded by REDIRECT_MAX_TIMES (3): the original request plus 3 hops.
    assert sum(1 for u in requested if "postings/chain" in u) == 4
    assert by_id["chain"]["extractions"] == 0 and len(by_id["chain"]["fetch_failures"]) == 1


def test_sources_are_crawled_one_at_a_time_with_the_download_delay_between(tmp_path: Path) -> None:
    boards = ["gh-a", "gh-b", "gh-c"]
    result = crawl(
        tmp_path,
        [src("greenhouse", board) for board in boards],
        {gh(board): {"fixture": "greenhouse/list_empty.json"} for board in boards},
        COLLECTOR_DOWNLOAD_DELAY_SECONDS="0.4",
    )

    assert result["max_in_flight"] == 1  # never two requests at once, even on one shared host
    requests = result["requests"]
    listings = [i for i, r in enumerate(requests) if r["url"] in {gh(b) for b in boards}]
    assert len(listings) == 3
    for before in listings[:-1]:
        gap = requests[before + 1]["start"] - requests[before]["end"]
        assert gap >= 0.35  # the delay between one source's last request and the next's first


def test_importing_the_runner_does_not_install_the_reactor() -> None:
    code = (
        "import sys, collector.spiders.run, collector.cli;"
        "assert 'twisted.internet.reactor' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(("variable", "value", "expected"), [("COLLECTOR_RETRY_TIMES", "0", 1)])
def test_collector_environment_reaches_the_crawl(
    tmp_path: Path, variable: str, value: str, expected: int
) -> None:
    result = crawl(
        tmp_path,
        [src("ashby", "ab-503")],
        {ab("ab-503"): {"status": 503, "body": ""}},
        **{variable: value},
    )
    assert attempts(result, ab("ab-503")) == expected
