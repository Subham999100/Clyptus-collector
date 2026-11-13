"""Command-line interface. ``run`` crawls configured sources (Phase 4); the rest are
clearly-labelled placeholders for later phases."""

from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from collector.pipelines.date_window import DateOutcome, check_date_window
from collector.pipelines.dedupe import dedupe
from collector.spiders.run import crawl_sources
from collector.spiders.sources import load_sources

app = typer.Typer(
    name="collector",
    help="Collect publicly available job postings and export them for the Clyptus Job Portal.",
    no_args_is_help=True,
    add_completion=False,
)


def _not_implemented(command: str) -> None:
    """Report that a command has no functionality yet, and fail. Does no work."""
    typer.echo(f"'collector {command}' is not implemented yet.", err=True)
    raise typer.Exit(code=1)


@app.command()
def run(
    sources: Annotated[
        Path,
        typer.Option(
            "--sources",
            help="CSV of configured sources: platform,company_name,identifier,company_website.",
        ),
    ],
) -> None:
    """Crawl the explicitly configured job sources and print a per-source summary.

    Exit codes: 0 for a clean run, 1 when any fetch failure, access block (HTTP 403/429 or a
    challenge page) or extraction failure occurred, 2 for an unusable sources file. A blocked
    source is reported and skipped; the other sources still run. Nothing is stored or exported
    yet (later phases).
    """
    try:
        configured = load_sources(sources)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    results = crawl_sources(configured)

    records = []
    failures = warnings = skipped = fetch_errors = 0
    for result in results:
        source_records = result.records
        records.extend(source_records)
        source_failures = sum(len(e.failures) for e in result.extractions)
        source_warnings = sum(len(e.warnings) for e in result.extractions)
        source_skipped = sum(len(e.skipped) for e in result.extractions)
        failures += source_failures
        warnings += source_warnings
        skipped += source_skipped
        fetch_errors += len(result.fetch_failures)
        typer.echo(
            f"{result.source.platform} {result.source.company_name}: "
            f"{len(source_records)} records, {source_failures} failures, "
            f"{source_warnings} warnings, {source_skipped} skipped, "
            f"{len(result.fetch_failures)} fetch errors" + (" (BLOCKED)" if result.blocked else "")
        )
        for fetch_failure in result.fetch_failures:
            label = "blocked" if fetch_failure.blocked else "fetch error"
            typer.echo(f"  {label}: {fetch_failure.reason} ({fetch_failure.url})", err=True)

    deduped = dedupe(records)
    outcomes = Counter(check_date_window(record).outcome for record in deduped.accepted)
    typer.echo(
        f"TOTAL: {len(deduped.accepted)} unique records "
        f"({len(deduped.duplicates)} duplicates); dates: "
        f"{outcomes[DateOutcome.ELIGIBLE]} eligible, "
        f"{outcomes[DateOutcome.UNKNOWN_DATE]} unknown, "
        f"{outcomes[DateOutcome.OLD_DATE]} old, "
        f"{outcomes[DateOutcome.FUTURE_DATE]} future"
    )
    if failures or fetch_errors:
        raise typer.Exit(code=1)


@app.command()
def export() -> None:
    """Export collected jobs to Excel (not implemented)."""
    _not_implemented("export")


@app.command()
def report() -> None:
    """Show a report of the last crawl (not implemented)."""
    _not_implemented("report")
