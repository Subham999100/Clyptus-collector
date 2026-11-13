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
            await asyncio.sleep(LATENCY)
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
            deferred = task.deferLater(reactor, LATENCY, _reply, request, time.monotonic())

            def done(result: Any) -> Any:
                IN_FLIGHT["now"] -= 1
                return result

            return deferred.addBoth(done)


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
        handler = "tests.crawl_harness.FakeHandler"
        config["DOWNLOAD_HANDLERS"] = {"https": handler, "http": handler}
        return config

    run.scrapy_settings = scripted_settings  # type: ignore[assignment]

    sources = [Source(**fields) for fields in SCENARIO["sources"]]
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
                        "fetch_failures": [
                            {"url": f.url, "reason": f.reason, "blocked": f.blocked}
                            for f in r.fetch_failures
                        ],
                        "blocked": r.blocked,
                    }
                    for r in results
                ],
                "requests": REQUESTS,
                "max_in_flight": IN_FLIGHT["max"],
            }
        )
    )
