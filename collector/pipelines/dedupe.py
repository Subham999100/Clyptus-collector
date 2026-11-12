"""Exact duplicate detection within one collection run, by stable job identity."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from collector.models import JobRecord
from collector.utils.identity import job_key


@dataclass(frozen=True)
class Duplicate:
    """A record dropped because ``first`` (same ``job_key``) was already accepted."""

    job_key: str
    record: JobRecord
    first: JobRecord


@dataclass
class DedupeResult:
    accepted: list[JobRecord] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)


class Deduper:
    """Stateful per-run deduper. The first record for a key wins; later ones are reported.

    Matching is exact on ``job_key`` only (no fuzzy matching, no title comparison), so distinct
    jobs that share a title are never merged. Use one instance per collection run.
    """

    def __init__(self) -> None:
        self._first_seen: dict[str, JobRecord] = {}

    def add(self, record: JobRecord) -> Duplicate | None:
        """Register ``record``; return a ``Duplicate`` if its key was already seen, else None."""
        key = job_key(record)
        first = self._first_seen.setdefault(key, record)
        if first is record:
            return None
        return Duplicate(job_key=key, record=record, first=first)


def dedupe(records: Iterable[JobRecord]) -> DedupeResult:
    """Split ``records`` into the first accepted record per identity and reported duplicates."""
    deduper = Deduper()
    result = DedupeResult()
    for record in records:
        duplicate = deduper.add(record)
        if duplicate is None:
            result.accepted.append(record)
        else:
            result.duplicates.append(duplicate)
    return result
