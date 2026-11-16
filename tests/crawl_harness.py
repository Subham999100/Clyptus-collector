"""Offline crawl harness: runs the real ``crawl_sources`` (real Scrapy engine, middlewares and
settings) with a scripted download handler instead of the network.

Run in a subprocess by ``tests/test_crawl.py`` (the Twisted reactor can start once per process)::

    python -c "from tests.crawl_harness import main; main()" scenario.json

The scenario JSON holds ``sources`` (``Source`` fields) and ``responses`` (URL -> scripted
reply: ``status``, ``headers``, ``body`` or ``fixture``; or ``error`` naming a transport error).
Any URL not scripted answers 404, and is listed under ``requests`` like every other one. Sockets
are blocked, so a real connection attempt cannot succeed. The result is printed as JSON.
"""

import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

from scrapy.core.downloader.handlers import DownloadHandlers
from scrapy.http import Headers, Request, Response
from scrapy.responsetypes import responsetypes

FIXTURES = Path(__file__).parent / "fixtures"
LATENCY = 0.05  # simulated network time, so overlapping requests would overlap in time

SCENARIO: dict[str, Any] = {}
REQUESTS: list[dict[str, Any]] = []
IN_FLIGHT = {"now": 0, "max": 0}
_MODERN = hasattr(DownloadHandlers, "download_request_async")  # coroutine-style handlers


def _reply(request: Request, started: float) -> Response:
    from twisted.internet import error

    REQUESTS.append(
        {
            "url": request.url,
            "user_agent": request.headers.get("User-Agent", b"").decode(),
            "timeout": request.meta.get("download_timeout"),
            "proxy": request.meta.get("proxy"),
            "credentials": bool(request.meta.get("http_user")) or "@" in request.url,
            "start": started,
            "end": time.monotonic(),
        }
    )
    scripted = SCENARIO["responses"].get(request.url, {"status": 404, "body": ""})
    if "error" in scripted:
        raise {"timeout": error.TimeoutError, "dns": error.DNSLookupError}[scripted["error"]]()
    body = scripted.get("body", "")
    if "fixture" in scripted:
        body = (FIXTURES / scripted["fixture"]).read_text(encoding="utf-8")
    headers = Headers(scripted.get("headers", {}))
    if "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    data = body.encode("utf-8")
    cls = responsetypes.from_args(headers=headers, url=request.url, body=data)
    return cls(
        url=request.url, status=scripted.get("status", 200), headers=headers, body=data,
        request=request,
    )  # fmt: skip


def _latency(request: Request) -> float:
    """Simulated network time; a scripted reply may set its own ``delay`` (seconds)."""
    return float(SCENARIO["responses"].get(request.url, {}).get("delay", LATENCY))


class FakeHandler:
    lazy = False

    @classmethod
    def from_crawler(cls, crawler: Any) -> "FakeHandler":
        return cls()

    def _enter(self) -> None:
        IN_FLIGHT["now"] += 1
        IN_FLIGHT["max"] = max(IN_FLIGHT["max"], IN_FLIGHT["now"])

    if _MODERN:

        async def download_request(self, request: Request, spider: Any = None) -> Response:
            self._enter()
            started = time.monotonic()
            await asyncio.sleep(_latency(request))
            try:
                return _reply(request, started)
            finally:
                IN_FLIGHT["now"] -= 1

        async def close(self) -> None:
            return None

    else:

        def download_request(self, request: Request, spider: Any = None) -> Any:
            from twisted.internet import reactor, task

            self._enter()
            deferred = task.deferLater(
                reactor, _latency(request), _reply, request, time.monotonic()
            )

            def done(result: Any) -> Any:
                IN_FLIGHT["now"] -= 1
                return result

            return deferred.addBoth(done)


CONNECTS: list[dict[str, Any]] = []  # TCP connections the real downloader tried to open
LOOKUPS: list[str] = []  # hostnames sent to the (scripted) system resolver


def _script_the_network(run: Any) -> None:
    """Network mode: Scrapy's real HTTP/1.1 handler, real Twisted ``HostnameEndpoint`` and the
    collector's real resolver, with only the two outermost edges replaced: the system DNS lookup
    answers from the scenario (``network.lookups``: host -> one answer list per lookup, the last
    one repeating) and every TCP connect is recorded and refused, so nothing leaves the process.
    """
    from twisted.internet import defer, error
    from twisted.internet import endpoints as tx_endpoints

    answers = {host: list(calls) for host, calls in SCENARIO["network"]["lookups"].items()}

    def lookup(host: str) -> list[str]:
        LOOKUPS.append(host)
        if host not in answers:
            raise OSError("no such host")  # a resolution failure
        calls = answers[host]
        return calls.pop(0) if len(calls) > 1 else calls[0]

    run._system_lookup = lookup  # looked up at call time by PublicOnlyResolver._lookup
    if SCENARIO["network"].get("stock_resolver"):  # Scrapy's resolver calls gethostbyname
        import socket

        socket.gethostbyname = lambda host: lookup(host)[0]  # type: ignore[assignment]

    def refuse(self: Any, protocolFactory: Any) -> Any:
        CONNECTS.append({"host": self._host, "port": self._port})
        return defer.fail(error.ConnectionRefusedError())

    tx_endpoints.TCP4ClientEndpoint.connect = refuse  # type: ignore[method-assign]
    tx_endpoints.TCP6ClientEndpoint.connect = refuse  # type: ignore[method-assign]


def _discover(scripted_settings: Any) -> None:
    """Discovery mode: ``discovery.companies`` (company-list CSV text) run through
    ``registry.run_batch`` with the real ``crawl_discovery`` driver. ``discovery.wikidata`` maps
    a company name to its scripted SPARQL result (keyed here by the URL the spider requests)."""
    import scrapy

    from collector.adapters.common import public_url
    from collector.companies import registry
    from collector.companies.directory import WikidataDirectory
    from collector.companies.input import load_companies
    from collector.spiders import pages

    pages.scrapy_settings = scripted_settings  # type: ignore[assignment]
    config = SCENARIO["discovery"]
    directory = WikidataDirectory() if config.get("wikidata") is not None else None
    for name, result in (config.get("wikidata") or {}).items():
        url = scrapy.Request(public_url(WikidataDirectory().query_url(name))).url
        SCENARIO["responses"][url] = {"body": json.dumps(result)}
    folder = Path(config["folder"])
    companies_csv = folder / "companies.csv"
    companies_csv.write_text(config["companies"], encoding="utf-8")
    state = folder / "state.json"
    result = registry.run_batch(
        load_companies(companies_csv).companies,
        state,
        batch_size=config.get("batch_size", 10),
        directory=directory,
        driver=pages.crawl_discovery,
    )
    print(
        "RESULT:"
        + json.dumps(
            {
                "records": {company.name: record for company, record in result.processed},
                "authorizations": registry.load_state(state).authorizations,
                "requests": [r["url"] for r in REQUESTS],
            }
        )
    )


def main() -> None:
    SCENARIO.update(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))

    real_connect = socket.socket.connect

    def loopback_only(self: socket.socket, address: Any) -> Any:
        # The reactor's own wake-up socket connects to loopback; anything else is a real attempt.
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            raise OSError("network access attempted")
        return real_connect(self, address)

    socket.socket.connect = loopback_only  # type: ignore[method-assign]

    from collector.spiders import run
    from collector.spiders.sources import Source

    original = run.scrapy_settings

    def scripted_settings(settings: Any = None) -> dict[str, object]:
        config = original(settings)
        if "network" not in SCENARIO:  # network mode keeps Scrapy's real HTTP handler
            handler = "tests.crawl_harness.FakeHandler"
            config["DOWNLOAD_HANDLERS"] = {"https": handler, "http": handler}
        elif SCENARIO["network"].get("stock_resolver"):  # control: Scrapy's own resolver
            config.pop("DNS_RESOLVER", None)
            config.pop("TWISTED_DNS_RESOLVER", None)
        return config

    run.scrapy_settings = scripted_settings  # type: ignore[assignment]
    if "network" in SCENARIO:
        _script_the_network(run)
    if "cli" in SCENARIO:  # Phase 9: the real CLI (e.g. `collector daily`), scripted network
        from collector.cli import app

        try:
            app(args=SCENARIO["cli"], prog_name="collector")
            code = 0
        except SystemExit as exit_:
            code = exit_.code if isinstance(exit_.code, int) else 1
        print("EXIT:" + json.dumps(code))
        print("REQUESTS:" + json.dumps([r["url"] for r in REQUESTS]))
        return
    if "discovery" in SCENARIO:  # Phase 6B: company discovery through the protected crawler
        _discover(scripted_settings)
        return

    from collector.adapters.html import parse_html_config

    sources = [
        Source(**{**fields, "html": parse_html_config(fields["html"])})
        if "html" in fields
        else Source(**fields)
        for fields in SCENARIO["sources"]
    ]
    if "db" in SCENARIO:  # crawl and store in a SQLite database (the whole Phase 4 pipeline)
        import dataclasses
        from datetime import datetime

        from collector.pipelines.ingest import run_collection

        now = datetime.fromisoformat(SCENARIO["now"])
        summary = run_collection(SCENARIO["db"], sources, run.crawl_sources, now=now)
        print("SUMMARY:" + json.dumps(dataclasses.asdict(summary)))
        print("REQUESTS:" + json.dumps(REQUESTS))
        return
    results = run.crawl_sources(sources)
    print(
        "RESULT:"
        + json.dumps(
            {
                "results": [
                    {
                        "identifier": r.source.identifier,
                        "records": len(r.records),
                        "extraction_failures": [
                            f.kind.value for e in r.extractions for f in e.failures
                        ],
                        "extractions": len(r.extractions),
                        "titles": [record.title for record in r.records],
                        "skipped": [s.reason for e in r.extractions for s in e.skipped],
                        "warnings": [w.message for e in r.extractions for w in e.warnings],
                        "fetch_failures": [
                            {"url": f.url, "reason": f.reason, "blocked": f.blocked}
                            for f in r.fetch_failures
                        ],
                        "blocked": r.blocked,
                    }
                    for r in results
                ],
                "requests": REQUESTS,
                "connects": CONNECTS,
                "lookups": LOOKUPS,
                "max_in_flight": IN_FLIGHT["max"],
            }
        )
    )
