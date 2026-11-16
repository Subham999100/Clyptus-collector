"""Discovery results, review and collection-authorization decisions, kept in one JSON file.

The state file (default ``data/discovery.json``, setting ``COLLECTOR_DISCOVERY_STATE_PATH``)
holds two independent sections:

- ``companies``: one discovery record per company (keyed by ``company_key``) with its status,
  source, evidence and timestamps. Written only by discovery and manual review.
- ``authorizations``: one explicit collection decision per company (APPROVED / RESTRICTED /
  PENDING, who decided, why, when, and for which source). Written only by ``authorize``;
  discovery never touches it. A company without a decision is PENDING.

Rules:

- A ``VERIFIED`` record is never replaced by a weaker or different result. A re-check that does
  not confirm the same source leaves the record as it is and notes the outcome in ``recheck``.
- An approval is bound to the source that was approved: if the verified source later changes,
  the company is PENDING again. RESTRICTED applies to the company whatever its source.
- Only VERIFIED + APPROVED records on a supported platform that pass ``check_source`` are
  exported, in the exact format ``load_sources`` reads; a source claimed by more than one
  company is ambiguous and exported for none of them.

The file is replaced atomically (temp file, fsync, ``os.replace``), never edited in place, so it
is always either the old or the new complete version. A batch saves at least every two seconds,
at its end and when interrupted, and a rerun continues with the companies not yet recorded.
The file is not locked: run one discovery process per state file at a time.
"""

import csv
import io
import json
import os
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from collector.companies.discovery import (
    Directory,
    Discovery,
    Fetch,
    Status,
    Steps,
    discover,
    discovery_steps,
)
from collector.companies.input import Company, company_key
from collector.spiders.sources import PLATFORMS, Source, check_source, load_sources

# Runs (key, steps) jobs online and reports each result (or the exception) to the callback.
Driver = Callable[[Sequence[tuple[str, Steps]], Callable[[str, Discovery | Exception], None]], None]

FORMAT = "clyptus-collector-discovery"
VERSION = 1
MAX_BATCH_SIZE = 1000
MAX_NOTE_LENGTH = 500
SOURCE_COLUMNS = ("platform", "company_name", "identifier", "company_website")
REVIEW_COLUMNS = (
    "company_name",
    "discovery_status",
    "authorization_status",
    "platform",
    "identifier",
    "reason",
)

_RECORD_KEYS = frozenset(
    {
        "company_name",
        "input",
        "status",
        "official_website",
        "careers_url",
        "platform",
        "identifier",
        "evidence",
        "reason",
        "failed",
        "discovered_at",
        "checked_at",
        "last_verified_at",
        "recheck",
    }
)


class Authorization(StrEnum):
    APPROVED = "APPROVED"
    PENDING = "PENDING"
    RESTRICTED = "RESTRICTED"


class StateError(Exception):
    """The state file exists but cannot be read, parsed or written."""


@dataclass
class State:
    companies: dict[str, dict[str, Any]] = field(default_factory=dict)
    authorizations: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchResult:
    processed: tuple[tuple[Company, dict[str, Any]], ...]  # each company with its stored record
    kept_verified: int  # verified records kept after a re-check that did not confirm them
    remaining: int  # companies still due after this batch
    state: State  # the state after the batch (also when it was a dry run and nothing was saved)


@dataclass(frozen=True)
class ReviewRow:
    company_name: str
    discovery_status: str
    authorization_status: str
    platform: str
    identifier: str
    reason: str


@dataclass(frozen=True)
class SourceExport:
    sources: tuple[Source, ...]
    review: tuple[ReviewRow, ...]


@dataclass(frozen=True)
class Coverage:
    companies: int  # unique companies counted
    processed: int
    verified: int
    review_required: int
    not_found: int
    unsupported: int
    approved: int
    restricted: int
    pending: int
    crawler_ready: int
    failures: int

    @property
    def discovery_coverage(self) -> float:
        return self.verified / self.companies if self.companies else 0.0

    @property
    def crawler_ready_coverage(self) -> float:
        return self.crawler_ready / self.companies if self.companies else 0.0


# --- the state file ---------------------------------------------------------------------------


def load_state(path: str | Path) -> State:
    """Read the state file; a missing file is an empty state. Raises ``StateError``."""
    path = Path(path)
    if not path.exists():
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"{path}: cannot read the discovery state ({type(exc).__name__})") from exc
    if (
        not isinstance(data, dict)
        or data.get("format") != FORMAT
        or data.get("version") != VERSION
        or not isinstance(data.get("companies"), dict)
        or not isinstance(data.get("authorizations"), dict)
    ):
        raise StateError(f"{path}: not a version {VERSION} discovery state file")
    for record in data["companies"].values():
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_KEYS
            or record["status"] not in set(Status)
        ):
            raise StateError(f"{path}: a discovery record is malformed")
    for decision in data["authorizations"].values():
        if not isinstance(decision, dict) or decision.get("decision") not in set(Authorization):
            raise StateError(f"{path}: an authorization record is malformed")
    return State(data["companies"], data["authorizations"])


def save_state(path: str | Path, state: State) -> None:
    """Atomically replace the state file. Raises ``StateError``."""
    document = {
        "format": FORMAT,
        "version": VERSION,
        "companies": state.companies,
        "authorizations": state.authorizations,
    }
    try:
        _atomic_write(
            Path(path), lambda out: json.dump(document, out, ensure_ascii=False, indent=1)
        )
    except OSError as exc:
        raise StateError(f"{path}: cannot write the discovery state ({exc.strerror})") from exc


def _atomic_write(
    path: Path, write: Callable[[TextIO], None], check: Callable[[Path], None] | None = None
) -> None:
    """Write to a temp file next to ``path``, optionally ``check`` it, then replace ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
            write(out)
            out.flush()
            os.fsync(out.fileno())
        if check is not None:
            check(Path(temp))
        _replace(temp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp)
        raise


def _replace(temp: str, path: Path, attempts: int = 20) -> None:
    """``os.replace``, retried briefly: on Windows a sync client, indexer or virus scanner can
    hold the target open for a moment right after it was written (``PermissionError``)."""
    for attempt in range(attempts):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _input(company: Company) -> dict[str, Any]:
    """What the company list said; a change makes the company due for discovery again."""
    return {
        "official_website": company.official_website,
        "careers_url": company.careers_url,
        "conflict": company.conflict is not None,
        "similar": company.similar is not None,
    }


# --- batches ----------------------------------------------------------------------------------


def is_due(
    company: Company,
    record: dict[str, Any] | None,
    now: datetime,
    *,
    recheck_after: timedelta | None = None,
    retry_failed: bool = False,
) -> bool:
    """Whether a company needs discovery: never processed, its input changed, a failed attempt
    is being retried, or (with ``recheck_after``) its last check is at least that old."""
    if record is None or record["input"] != _input(company):
        return True
    if retry_failed and record["failed"]:
        return True
    if recheck_after is not None:
        return now - datetime.fromisoformat(record["checked_at"]) >= recheck_after
    return False


def run_batch(
    companies: Sequence[Company],
    state_path: str | Path,
    *,
    batch_size: int,
    fetch: Fetch | None = None,
    now: datetime | None = None,
    recheck_after: timedelta | None = None,
    retry_failed: bool = False,
    dry_run: bool = False,
    save_interval: float = 2.0,
    directory: Directory | None = None,
    driver: Driver | None = None,
) -> BatchResult:
    """Discover the next ``batch_size`` due companies, in list order.

    Pages come from ``driver`` when given (it runs every company's discovery steps online, e.g.
    ``collector.spiders.pages.crawl_discovery``), else from the synchronous ``fetch`` (``None``:
    offline). ``directory`` looks up websites for companies listed without one (online only).

    Progress is saved whenever ``save_interval`` seconds have passed since the last save (with
    page fetching, that is after nearly every company), when the batch ends, and when it is
    interrupted; a hard kill loses at most the last ``save_interval`` seconds of work, which a
    rerun simply repeats. A failure for one company is recorded for it and the batch continues.
    ``dry_run`` computes
    the same results without writing anything. Raises ``ValueError`` for a bad batch size and
    ``StateError`` when the state file is unusable.
    """
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    now = now or datetime.now(UTC)
    state = load_state(state_path)
    due = [
        company
        for company in companies
        if is_due(
            company,
            state.companies.get(company.key),
            now,
            recheck_after=recheck_after,
            retry_failed=retry_failed,
        )
    ]
    batch = due[:batch_size]
    by_key = {company.key: company for company in batch}
    results: dict[str, dict[str, Any]] = {}
    kept = 0
    unsaved = 0
    last_save = time.monotonic()

    def record(key: str, outcome: Discovery | Exception) -> None:
        nonlocal kept, unsaved, last_save
        company = by_key[key]
        found = outcome
        if not isinstance(found, Discovery):  # one company's failure must not stop the batch
            found = Discovery(
                Status.REVIEW_REQUIRED,
                company.official_website,
                company.careers_url,
                None,
                None,
                (),
                f"discovery failed ({type(outcome).__name__})",
                failed=True,
            )
        stored, was_kept = _merge(state.companies.get(key), company, found, now)
        kept += was_kept
        state.companies[key] = results[key] = stored
        unsaved += 1
        if not dry_run and time.monotonic() - last_save >= save_interval:
            save_state(state_path, state)
            unsaved, last_save = 0, time.monotonic()

    try:
        if driver is not None:  # e.g. the protected Scrapy crawl
            online = [
                (c.key, discovery_steps(c, online=True, directory=directory, now=now))
                for c in batch
            ]
            driver(online, record)
        else:
            for company in batch:
                try:
                    outcome: Discovery | Exception = discover(
                        company, fetch, directory=directory, now=now
                    )
                except Exception as exc:  # noqa: BLE001 - recorded for this company
                    outcome = exc
                record(company.key, outcome)
    finally:
        # Also on an interruption (Ctrl+C, an error): keep every company finished so far.
        if not dry_run and unsaved:
            save_state(state_path, state)
    processed = tuple((c, results[c.key]) for c in batch if c.key in results)
    return BatchResult(processed, kept, len(due) - len(processed), state)


def _merge(
    old: dict[str, Any] | None, company: Company, found: Discovery, now: datetime
) -> tuple[dict[str, Any], bool]:
    stamp = _stamp(now)
    new = {
        "company_name": company.name,
        "input": _input(company),
        "status": found.status.value,
        "official_website": found.official_website,
        "careers_url": found.careers_url,
        "platform": found.platform,
        "identifier": found.identifier,
        "evidence": list(found.evidence),
        "reason": found.reason,
        "failed": found.failed,
        "discovered_at": old["discovered_at"] if old else stamp,
        "checked_at": stamp,
        "last_verified_at": stamp if found.status == Status.VERIFIED else None,
        "recheck": None,
    }
    if old is None or old["status"] != Status.VERIFIED:
        return new, False
    if found.status == Status.VERIFIED and (found.platform, found.identifier) == (
        old["platform"],
        old["identifier"],
    ):
        return new, False
    kept = dict(old)
    kept["input"] = new["input"]
    kept["checked_at"] = stamp
    kept["recheck"] = {
        "checked_at": stamp,
        "status": found.status.value,
        "reason": found.reason or "re-check found a different source",
    }
    return kept, True


# --- review and authorization -----------------------------------------------------------------


def _check_text(value: str, name: str) -> str:
    text = " ".join((value or "").split())
    if not text:
        raise ValueError(f"{name} is required")
    if len(text) > MAX_NOTE_LENGTH:
        raise ValueError(f"{name} is longer than {MAX_NOTE_LENGTH} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise ValueError(f"{name} contains control characters")
    return text


def _record(state: State, company_name: str) -> tuple[str, dict[str, Any]]:
    key = company_key(company_name)
    record = state.companies.get(key)
    if record is None:
        raise LookupError("no discovery record for that company; run discovery first")
    return key, record


def _source(record: dict[str, Any]) -> Source:
    """The record's crawler source; ``ValueError`` if it has no complete supported one."""
    if record["platform"] not in PLATFORMS or not record["identifier"]:
        raise ValueError("no supported platform and identifier")
    return check_source(
        record["platform"],
        record["company_name"],
        record["identifier"],
        record["official_website"],
    )


def confirm_source(
    state_path: str | Path, company_name: str, *, by: str, note: str, now: datetime | None = None
) -> dict[str, Any]:
    """Record a reviewer's confirmation that a REVIEW_REQUIRED record's source belongs to the
    company, making it VERIFIED. Only the recorded candidate can be confirmed; nothing new is
    entered here. Does not authorize collection."""
    by, note = _check_text(by, "reviewer"), _check_text(note, "evidence")
    state = load_state(state_path)
    _, record = _record(state, company_name)
    if record["status"] != Status.REVIEW_REQUIRED:
        raise ValueError(
            f"only REVIEW_REQUIRED records can be confirmed (this is {record['status']})"
        )
    try:
        _source(record)
    except ValueError as exc:
        raise ValueError(f"the record has no source that could be confirmed ({exc})") from exc
    stamp = _stamp(now or datetime.now(UTC))
    record["status"] = Status.VERIFIED.value
    record["evidence"] = [*record["evidence"], f"confirmed by manual review ({by}): {note}"]
    record["reason"] = None
    record["last_verified_at"] = stamp
    record["recheck"] = None
    save_state(state_path, state)
    return record


def authorize(
    state_path: str | Path,
    company_name: str,
    decision: Authorization,
    *,
    by: str,
    note: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record an explicit collection decision. APPROVED needs a VERIFIED, supported source and
    is bound to it; RESTRICTED and PENDING can be recorded for any discovered company."""
    by, note = _check_text(by, "decided-by"), _check_text(note, "note")
    state = load_state(state_path)
    key, record = _record(state, company_name)
    if decision == Authorization.APPROVED:
        if record["status"] != Status.VERIFIED:
            raise ValueError(f"only VERIFIED sources can be approved (this is {record['status']})")
        try:
            _source(record)
        except ValueError as exc:
            raise ValueError(f"the verified record is not a usable crawler source ({exc})") from exc
    entry = {
        "decision": Authorization(decision).value,
        "platform": record["platform"],
        "identifier": record["identifier"],
        "by": by,
        "note": note,
        "decided_at": _stamp(now or datetime.now(UTC)),
    }
    state.authorizations[key] = entry
    save_state(state_path, state)
    return entry


def authorization_status(record: dict[str, Any], decision: dict[str, Any] | None) -> Authorization:
    """The decision in force for a record. An approval counts only for the verified source it
    was given for; anything else is PENDING."""
    if decision is None:
        return Authorization.PENDING
    if decision["decision"] == Authorization.RESTRICTED:
        return Authorization.RESTRICTED
    if (
        decision["decision"] == Authorization.APPROVED
        and record["status"] == Status.VERIFIED
        and (decision["platform"], decision["identifier"])
        == (record["platform"], record["identifier"])
    ):
        return Authorization.APPROVED
    return Authorization.PENDING


# --- crawler sources --------------------------------------------------------------------------


def approved_sources(state: State) -> SourceExport:
    """Split the records into crawler sources and a review list (with the reason for each)."""
    candidates: list[tuple[dict[str, Any], Authorization, Source]] = []
    review: list[ReviewRow] = []

    def hold(record: dict[str, Any], auth: Authorization, reason: str) -> None:
        review.append(
            ReviewRow(
                record["company_name"],
                record["status"],
                auth.value,
                record["platform"] or "",
                record["identifier"] or "",
                reason,
            )
        )

    for key, record in state.companies.items():
        auth = authorization_status(record, state.authorizations.get(key))
        if record["status"] != Status.VERIFIED:
            hold(record, auth, record["reason"] or f"discovery status is {record['status']}")
        elif auth != Authorization.APPROVED:
            hold(record, auth, f"collection is not approved ({auth.value})")
        else:
            try:
                candidates.append((record, auth, _source(record)))
            except ValueError as exc:
                hold(record, auth, f"not a usable crawler source ({exc})")

    def source_key(source: Source) -> tuple[str, str]:  # the duplicate rule of load_sources
        identifier = source.identifier
        return source.platform, identifier if source.platform == "jsonld" else identifier.casefold()

    claims = Counter(source_key(source) for _, _, source in candidates)
    sources: list[Source] = []
    for record, auth, source in candidates:
        if claims[source_key(source)] > 1:
            hold(record, auth, "the same source is claimed by another company")
        else:
            sources.append(source)
    return SourceExport(tuple(sources), tuple(review))


def write_source_files(
    export: SourceExport, output: str | Path, review_output: str | Path, *, overwrite: bool = False
) -> None:
    """Write the crawler sources CSV (only when there is at least one source) and the review CSV.

    The sources file is read back with ``load_sources`` before it replaces anything, so a file
    that the collector would reject is never written. Raises ``FileExistsError`` (without
    ``overwrite``), ``OSError`` or ``ValueError``.
    """
    output, review_output = Path(output), Path(review_output)
    if output.resolve() == review_output.resolve():
        raise ValueError("the sources and review files must be different")
    targets = [review_output] + ([output] if export.sources else [])
    if not overwrite:
        for target in targets:
            if target.exists():
                raise FileExistsError(f"{target} already exists (use --overwrite to replace it)")

    def write_review(out: TextIO) -> None:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(REVIEW_COLUMNS)
        writer.writerows(
            [r.company_name, r.discovery_status, r.authorization_status]
            + [r.platform, r.identifier, r.reason]
            for r in export.review
        )

    if export.sources:

        def write_sources(out: TextIO) -> None:
            out.write(sources_csv(export.sources))

        def check(temp: Path) -> None:
            if load_sources(temp) != list(export.sources):
                raise ValueError("the written sources file does not read back identically")

        _atomic_write(output, write_sources, check)
    _atomic_write(review_output, write_review)


def sources_csv(sources: Sequence[Source]) -> str:
    """Sources in the exact CSV format ``collector run --sources`` reads."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SOURCE_COLUMNS)
    for source in sources:
        writer.writerow(
            [source.platform, source.company_name, source.identifier, source.company_website or ""]
        )
    return buffer.getvalue()


# --- coverage ---------------------------------------------------------------------------------


def coverage(state: State, keys: Sequence[str] | None = None) -> Coverage:
    """Counts over the given company keys (all stored records when ``keys`` is ``None``).
    Coverage rates divide by the number of unique companies counted, processed or not."""
    keys = list(state.companies) if keys is None else list(dict.fromkeys(keys))
    records = {key: state.companies[key] for key in keys if key in state.companies}
    statuses = Counter(record["status"] for record in records.values())
    auths = Counter(
        authorization_status(record, state.authorizations.get(key))
        for key, record in records.items()
    )
    exported = approved_sources(State(records, state.authorizations)).sources
    return Coverage(
        companies=len(keys),
        processed=len(records),
        verified=statuses[Status.VERIFIED],
        review_required=statuses[Status.REVIEW_REQUIRED],
        not_found=statuses[Status.NOT_FOUND],
        unsupported=statuses[Status.UNSUPPORTED],
        approved=auths[Authorization.APPROVED],
        restricted=auths[Authorization.RESTRICTED],
        pending=auths[Authorization.PENDING],
        crawler_ready=len(exported),
        failures=sum(1 for record in records.values() if record["failed"]),
    )
