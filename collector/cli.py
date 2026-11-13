"""Command-line interface. ``run`` crawls configured sources (Phase 4); the rest are
clearly-labelled placeholders for later phases."""

from pathlib import Path
from typing import Annotated

import typer

from collector.pipelines.ingest import CollectionError, run_collection
from collector.settings import get_settings
from collector.spiders.run import SourceResult, crawl_sources
from collector.spiders.sources import Source, load_sources

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
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="SQLite database file (created if missing, never overwritten). "
            "Default: COLLECTOR_SQLITE_PATH, else data/collector.db.",
        ),
    ] = None,
) -> None:
    """Crawl the configured job sources and store the jobs in the SQLite database.

    Records are de-duplicated, postings older than the date window are excluded, and the rest
    are stored (unknown-date and future-dated ones too, flagged in the report). Each run is
    recorded; nothing already stored is ever deleted.

    Exit codes: 0 for a clean run, 1 when any fetch failure, access block (HTTP 403/429 or a
    challenge page) or extraction failure occurred (a blocked source is skipped; the other
    sources still run and are stored), 2 for an unusable sources file, 3 when the database
    could not be used or the run failed.
    """
    try:
        configured = load_sources(sources)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    database = db if db is not None else get_settings().sqlite_path
    crawled: list[SourceResult] = []

    def crawl(to_crawl: list[Source]) -> list[SourceResult]:
        crawled.extend(crawl_sources(to_crawl))
        return crawled

    try:
        summary = run_collection(database, configured, crawl)
    except CollectionError as exc:
        _echo_sources(crawled)
        where = f" (run {exc.run_id} marked FAILED)" if exc.run_id is not None else ""
        typer.echo(f"error: {exc}{where}", err=True)
        raise typer.Exit(code=3) from exc
    _echo_sources(crawled)

    typer.echo(
        f"TOTAL: {summary.unique} unique records "
        f"({summary.duplicates} duplicates); dates: "
        f"{summary.eligible} eligible, "
        f"{summary.unknown_date} unknown, "
        f"{summary.old_date} old, "
        f"{summary.future_date} future"
    )
    typer.echo(
        f"RUN {summary.run_id} {summary.status}: {summary.jobs_new} new, "
        f"{summary.jobs_updated} updated, {summary.jobs_unchanged} unchanged "
        f"({summary.jobs_seen} stored this run; {summary.old_date} old excluded); "
        f"{summary.sources_failed} of {summary.sources} sources failed "
        f"({summary.sources_blocked} blocked)"
    )
    if summary.future_date:
        typer.echo(f"warning: {summary.future_date} future-dated posting(s) stored; review them")
    if summary.status != "SUCCEEDED" or summary.errors:
        raise typer.Exit(code=1)


def _echo_sources(results: list[SourceResult]) -> None:
    """One summary line per source, then its fetch failures (reasons only, never payloads)."""
    for result in results:
        source_failures = sum(len(e.failures) for e in result.extractions)
        source_warnings = sum(len(e.warnings) for e in result.extractions)
        source_skipped = sum(len(e.skipped) for e in result.extractions)
        typer.echo(
            f"{result.source.platform} {result.source.company_name}: "
            f"{len(result.records)} records, {source_failures} failures, "
            f"{source_warnings} warnings, {source_skipped} skipped, "
            f"{len(result.fetch_failures)} fetch errors" + (" (BLOCKED)" if result.blocked else "")
        )
        for fetch_failure in result.fetch_failures:
            label = "blocked" if fetch_failure.blocked else "fetch error"
            typer.echo(f"  {label}: {fetch_failure.reason} ({fetch_failure.url})", err=True)


@app.command()
def export() -> None:
    """Export collected jobs to Excel (not implemented)."""
    _not_implemented("export")


@app.command()
def report() -> None:
    """Show a report of the last crawl (not implemented)."""
    _not_implemented("report")
