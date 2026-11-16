"""DNS / SSRF protection at the connection boundary. Offline.

Two layers, neither of which only calls a validation helper:

* Twisted's real ``HostnameEndpoint`` (the endpoint Scrapy's HTTP handler connects through)
  driven by the collector's real resolver on an in-memory reactor: the recorded ``connectTCP``
  calls are the addresses a connection would actually be opened to.
* A real Scrapy crawl (real HTTP/1.1 handler, real endpoints, real resolver) in a subprocess
  where only the system DNS answer and the final TCP connect are scripted (see
  ``tests/crawl_harness.py``, network mode). Every connection attempt is recorded and refused.
"""

import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scrapy.crawler import Crawler
from scrapy.settings import Settings as ScrapySettings
from scrapy.settings import default_settings
from twisted.internet import defer
from twisted.internet.endpoints import HostnameEndpoint
from twisted.internet.interfaces import IReactorPluggableNameResolver
from twisted.internet.protocol import Factory
from twisted.internet.testing import MemoryReactorClock
from zope.interface import directlyProvides

from collector.settings import Settings
from collector.spiders.platforms import GreenhouseSpider
from collector.spiders.run import (
    PublicOnlyResolver,
    SourceOnlyMiddleware,
    UnsafeDestination,
    is_public_address,
    scrapy_settings,
)
from collector.spiders.sources import Source
from tests.test_crawl import crawl, lv, src

ROOT = Path(__file__).parent.parent

PUBLIC_V4 = "104.18.0.1"
PUBLIC_V6 = "2606:4700:4700::1111"

# --- the address policy -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        (PUBLIC_V4, True),
        ("8.8.8.8", True),
        (PUBLIC_V6, True),
        ("::ffff:8.8.8.8", True),  # IPv4-mapped: judged as the IPv4 address it maps to
        ("10.0.0.5", False),  # private
        ("172.16.4.4", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),  # loopback
        ("127.1.2.3", False),
        ("::1", False),
        ("169.254.169.254", False),  # link-local (cloud metadata)
        ("fe80::1", False),
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),  # unspecified
        ("::", False),
        ("224.0.0.1", False),  # multicast (Python's is_global accepts it)
        ("ff02::1", False),
        ("240.0.0.1", False),  # reserved
        ("255.255.255.255", False),
        ("192.0.2.1", False),  # documentation
        ("2001:db8::1", False),
        ("fd00::1", False),  # unique-local IPv6
        ("fec0::1", False),  # deprecated site-local IPv6
        ("::ffff:10.0.0.1", False),  # IPv4-mapped private
        ("::ffff:127.0.0.1", False),
        ("::ffff:169.254.169.254", False),
        ("64:ff9b::a00:1", False),  # NAT64 embedding 10.0.0.1
        ("2002:7f00:1::", False),  # 6to4 embedding 127.0.0.1
        ("not-an-ip", False),  # fail closed
        ("", False),
        ("10.0.0.5%eth0", False),
    ],
)
def test_only_public_unicast_addresses_are_permitted(address: str, public: bool) -> None:
    assert is_public_address(address) is public


# --- the connection boundary: Twisted's real endpoint + the collector's resolver ------------------


class ScriptedResolver(PublicOnlyResolver):
    """The real resolver with only the system lookup scripted: one answer (a list of address
    strings, or an exception) per lookup, the last one repeating."""

    def __init__(self, reactor: Any, answers: list[Any]) -> None:
        super().__init__(reactor)
        self.answers = answers
        self.lookups = 0

    def _lookup(self, host: str) -> Any:
        self.lookups += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return defer.fail(answer) if isinstance(answer, Exception) else defer.succeed(list(answer))


def attempt_connection(*answers: Any, times: int = 1) -> tuple[list[str], list[Any], int]:
    """Open ``times`` connections to a hostname through ``HostnameEndpoint``. Returns the
    addresses the reactor was asked to connect to, the failures, and the number of lookups."""
    reactor = MemoryReactorClock()
    directlyProvides(reactor, IReactorPluggableNameResolver)
    resolver = ScriptedResolver(reactor, list(answers))
    reactor.nameResolver = resolver  # type: ignore[attr-defined]
    failures: list[Any] = []
    for _ in range(times):
        endpoint = HostnameEndpoint(reactor, "api.lever.co", 443)
        endpoint.connect(Factory()).addErrback(failures.append)
        reactor.advance(1)
    return [str(client[0]) for client in reactor.tcpClients], failures, resolver.lookups


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6, "::ffff:8.8.8.8"])
def test_a_public_address_is_connected_to(address: str) -> None:
    connected, failures, _ = attempt_connection([address])
    assert connected == [address] and failures == []


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "127.0.0.1", "::1", "169.254.169.254", "fe80::1", "fd00::1", "224.0.0.1",
     "240.0.0.1", "0.0.0.0", "::ffff:10.0.0.1", "64:ff9b::a00:1"],
)  # fmt: skip
def test_a_hostname_resolving_to_a_prohibited_address_is_never_connected_to(address: str) -> None:
    connected, failures, lookups = attempt_connection([address])

    assert connected == []  # nothing was handed to the reactor to connect to
    assert lookups == 1 and len(failures) == 1
    assert address not in str(failures[0])  # the resolved address is not leaked


@pytest.mark.parametrize(
    "answer",
    [
        [PUBLIC_V4, "10.0.0.5"],  # mixed IPv4
        ["10.0.0.5", PUBLIC_V4],  # private first
        [PUBLIC_V4, "fd00::1"],  # public IPv4 + private IPv6
        [PUBLIC_V6, "127.0.0.1"],
        [PUBLIC_V4, "not-an-ip"],  # anything unparseable poisons the answer
    ],
)
def test_one_prohibited_address_among_public_ones_rejects_the_whole_answer(
    answer: list[str],
) -> None:
    connected, failures, _ = attempt_connection(answer)
    assert connected == [] and len(failures) == 1  # not even the public address is used


def test_every_public_address_of_a_dual_stack_answer_may_be_used() -> None:
    connected, failures, _ = attempt_connection([PUBLIC_V4, PUBLIC_V6])
    assert failures == [] and connected
    assert set(connected) <= {PUBLIC_V4, PUBLIC_V6}


@pytest.mark.parametrize("failure", [OSError("secret-resolver-detail"), TimeoutError(), []])
def test_a_resolution_failure_fails_closed_without_leaking_detail(failure: Any) -> None:
    connected, failures, _ = attempt_connection(failure)
    assert connected == [] and len(failures) == 1
    assert "secret-resolver-detail" not in str(failures[0])


def test_dns_rebinding_is_refused_at_the_moment_of_connection() -> None:
    """The name resolves to a public address first and to an internal one afterwards (the
    attack). Every connection resolves afresh and is validated, so only the first connects."""
    connected, failures, lookups = attempt_connection([PUBLIC_V4], ["10.0.0.9"], times=3)

    assert connected == [PUBLIC_V4]  # the later, rebound answers never reached the reactor
    assert lookups == 3 and len(failures) == 2  # not cached, so each one was checked again


def test_the_hostname_is_left_alone_for_tls_validation() -> None:
    reactor = MemoryReactorClock()
    directlyProvides(reactor, IReactorPluggableNameResolver)
    reactor.nameResolver = ScriptedResolver(reactor, [[PUBLIC_V4]])  # type: ignore[attr-defined]

    HostnameEndpoint(reactor, "api.lever.co", 443).connect(Factory())
    reactor.advance(1)

    # The reactor connects to the address; the hostname is still what TLS (SNI and certificate
    # verification) is configured with by the layers above (see the TLS test below).
    assert [client[:2] for client in reactor.tcpClients] == [(PUBLIC_V4, 443)]


# --- TLS: certificates and hostnames are verified -----------------------------------------------


def tls_options(config: dict[str, Any]) -> Any:
    """The TLS options Scrapy's real HTTP handler would use for api.lever.co:443."""
    from scrapy.core.downloader.handlers.http11 import HTTP11DownloadHandler

    crawler = Crawler(GreenhouseSpider, ScrapySettings(config))
    try:
        crawler._apply_settings()
    except RuntimeError:  # newer Scrapy needs a reactor for this; the handler needs no more
        pass
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        handler = HTTP11DownloadHandler.from_crawler(crawler)
    return handler._contextFactory.creatorForNetloc(b"api.lever.co", 443)


def test_the_collector_verifies_certificates_and_hostnames_unlike_scrapys_default() -> None:
    from twisted.internet._sslverify import ClientTLSOptions

    stock = tls_options({})
    configured = tls_options(scrapy_settings(Settings()))

    # Scrapy's default uses its own non-verifying subclass; ours is Twisted's verifying class.
    assert type(stock) is not ClientTLSOptions and "Scrapy" in type(stock).__name__
    assert type(configured) is ClientTLSOptions
    assert configured._hostname == "api.lever.co"  # checked against the certificate and SNI


# --- settings and proxies ---


def test_the_resolver_and_tls_settings_follow_the_installed_scrapy() -> None:
    config = scrapy_settings(Settings())
    resolver = "collector.spiders.run.PublicOnlyResolver"

    if hasattr(default_settings, "TWISTED_DNS_RESOLVER"):
        assert config["TWISTED_DNS_RESOLVER"] == resolver and "DNS_RESOLVER" not in config
    else:
        assert config["DNS_RESOLVER"] == resolver and "TWISTED_DNS_RESOLVER" not in config
    assert config["DNSCACHE_ENABLED"] is False  # every connection is validated afresh
    if hasattr(default_settings, "DOWNLOAD_VERIFY_CERTIFICATES"):
        assert config["DOWNLOAD_VERIFY_CERTIFICATES"] is True
    else:
        assert str(config["DOWNLOADER_CLIENTCONTEXTFACTORY"]).endswith("BrowserLikeContextFactory")


def test_proxies_are_disabled_so_they_cannot_bypass_the_destination_checks() -> None:
    middlewares = scrapy_settings(Settings())["DOWNLOADER_MIDDLEWARES"]
    assert middlewares["scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware"] is None  # type: ignore[index]


def test_a_request_that_asks_for_a_proxy_is_refused_before_the_network() -> None:
    spider = GreenhouseSpider(Source("greenhouse", "C", "b"))
    middleware = SourceOnlyMiddleware(SimpleNamespace(spider=spider))
    request = SimpleNamespace(url=spider.listing_url(), meta={"proxy": "http://proxy.example:8080"})

    with pytest.raises(UnsafeDestination, match="proxy"):
        middleware.process_request(request)  # type: ignore[arg-type]
    request.meta = {}
    assert middleware.process_request(request) is None  # type: ignore[arg-type]


def test_proxy_environment_variables_do_not_route_any_request(tmp_path: Path) -> None:
    proxies = {
        name: "http://127.0.0.1:9"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy")
    }
    result = crawl(
        tmp_path,
        [src("lever", "lv-ok")],
        {lv("lv-ok"): {"fixture": "lever/postings_complete.json"}},
        **proxies,
    )

    assert result["by_id"]["lv-ok"]["records"] == 1
    assert {request["proxy"] for request in result["requests"]} == {None}


# --- a real Scrapy crawl: robots.txt, retries and many sources ---


def crawl_network(
    tmp_path: Path, sources: list[dict[str, str]], lookups: dict[str, list[list[str]]], **flags: Any
) -> dict[str, Any]:
    scenario = tmp_path / "network.json"
    scenario.write_text(
        json.dumps({"sources": sources, "responses": {}, "network": {"lookups": lookups, **flags}}),
        encoding="utf-8",
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COLLECTOR_")}
    environment.update(
        COLLECTOR_DOWNLOAD_DELAY_SECONDS="0",
        COLLECTOR_AUTOTHROTTLE_ENABLED="false",
        COLLECTOR_RETRY_TIMES="1",
    )
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


def page(platform_host: str) -> dict[str, str]:
    return {"platform": "jsonld", "company_name": "J", "identifier": f"https://{platform_host}/p"}


@pytest.fixture(scope="module")
def mixed_crawl(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One crawl, many sources: every kind of DNS answer, all through Scrapy's real handler."""
    lookups = {
        "api.lever.co": [[PUBLIC_V4]],  # public IPv4
        "v6.example.org": [[PUBLIC_V6]],  # public IPv6
        "boards-api.greenhouse.io": [["10.0.0.5"]],  # private
        "api.ashbyhq.com": [["8.8.8.8", "169.254.169.254"]],  # mixed public + link-local
        "mapped.example.org": [["::ffff:10.0.0.1"]],  # IPv4-mapped private
        "loop.example.org": [["127.0.0.1"]],  # loopback
        "ula.example.org": [["fd00::1"]],  # private IPv6
        "rebind.example.org": [["104.18.0.2"], ["10.0.0.9"]],  # public, then rebound
        # "gone.example.org" has no entry: its resolution fails
    }
    sources = [
        src("lever", "pub"),
        page("v6.example.org"),
        src("greenhouse", "priv"),
        src("ashby", "mixed"),
        page("mapped.example.org"),
        page("loop.example.org"),
        page("ula.example.org"),
        page("rebind.example.org"),
        page("gone.example.org"),
    ]
    result = crawl_network(tmp_path_factory.mktemp("net"), sources, lookups)
    result["identifiers"] = {
        "pub": "pub",
        "v6": "https://v6.example.org/p",
        "priv": "priv",
        "mixed": "mixed",
        "mapped": "https://mapped.example.org/p",
        "loop": "https://loop.example.org/p",
        "ula": "https://ula.example.org/p",
        "rebind": "https://rebind.example.org/p",
        "gone": "https://gone.example.org/p",
    }
    return result


def test_the_real_downloader_connects_only_to_public_addresses(mixed_crawl: dict[str, Any]) -> None:
    connected = {c["host"] for c in mixed_crawl["connects"]}
    assert connected == {PUBLIC_V4, PUBLIC_V6, "104.18.0.2"}  # public IPv4, IPv6, pre-rebind
    assert all(c["port"] == 443 for c in mixed_crawl["connects"])


@pytest.mark.parametrize("name", ["priv", "mixed", "mapped", "loop", "ula", "gone"])
def test_sources_resolving_to_prohibited_or_failed_addresses_fail_without_a_connection(
    mixed_crawl: dict[str, Any], name: str
) -> None:
    outcome = mixed_crawl["by_id"][mixed_crawl["identifiers"][name]]

    assert outcome["extractions"] == 0 and outcome["records"] == 0  # never an empty success
    assert len(outcome["fetch_failures"]) == 1
    assert outcome["fetch_failures"][0]["reason"] in {"DNSLookupError", "CannotResolveHostError"}
    assert outcome["blocked"] is False


def test_the_rebinding_host_is_connected_to_only_before_it_rebinds(
    mixed_crawl: dict[str, Any],
) -> None:
    connections = [c["host"] for c in mixed_crawl["connects"]]
    assert connections.count("104.18.0.2") == 1  # the one answer that was public
    assert "10.0.0.9" not in connections  # the rebound answer was never connected to
    outcome = mixed_crawl["by_id"][mixed_crawl["identifiers"]["rebind"]]
    assert len(outcome["fetch_failures"]) == 1 and outcome["extractions"] == 0


def test_robots_txt_and_retries_are_validated_like_every_other_request(
    mixed_crawl: dict[str, Any],
) -> None:
    # A source that is refused is looked up for its robots.txt and its listing request, and again
    # for each retry: more lookups than one request, each answered (and refused) independently.
    lookups = mixed_crawl["lookups"]
    for host in ("boards-api.greenhouse.io", "api.ashbyhq.com", "loop.example.org"):
        assert lookups.count(host) >= 2, host
    # ...and not one of them produced a connection.
    assert not {c["host"] for c in mixed_crawl["connects"]} & {
        "10.0.0.5",
        "8.8.8.8",
        "169.254.169.254",
        "127.0.0.1",
    }


def test_public_sources_are_unaffected_by_the_blocked_ones(mixed_crawl: dict[str, Any]) -> None:
    # The public sources reached the (refused) TCP connect: they failed there, not at validation.
    for name in ("pub", "v6"):
        outcome = mixed_crawl["by_id"][mixed_crawl["identifiers"][name]]
        (failure,) = outcome["fetch_failures"]
        assert "Refused" in failure["reason"]
    assert len(mixed_crawl["results"]) == 9  # every source was attempted and reported


def test_no_resolved_address_or_detail_appears_in_reports_or_logs(
    mixed_crawl: dict[str, Any],
) -> None:
    text = json.dumps(mixed_crawl["results"]) + mixed_crawl["stderr"]
    for private in ("10.0.0.5", "10.0.0.9", "169.254.169.254", "127.0.0.1", "fd00::1", "::ffff"):
        assert private not in text, private
    assert "refusing to connect to boards-api.greenhouse.io" in mixed_crawl["stderr"]  # host only


def test_control_stock_scrapy_would_connect_to_a_private_address(tmp_path: Path) -> None:
    """With Scrapy's own resolver (ours removed) the same private answer IS connected to, which
    shows the scripted boundary is sensitive and what the protection prevents."""
    result = crawl_network(
        tmp_path,
        [src("greenhouse", "priv")],
        {"boards-api.greenhouse.io": [["10.0.0.5"]]},
        stock_resolver=True,
    )
    assert {c["host"] for c in result["connects"]} == {"10.0.0.5"}
