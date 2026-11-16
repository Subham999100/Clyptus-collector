"""Command-line interface. ``run`` crawls configured sources into SQLite (Phase 4), ``export``
writes the stored jobs to Excel (Phase 5), ``discover`` and ``sources`` turn a company list into
reviewed, approved crawler sources (Phase 6), ``daily`` runs the unattended daily collection and
dated export (Phase 9); ``report`` is a clearly-labelled placeholder."""

from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from collector.companies import registry
from collector.companies.directory import get_directory
from collector.companies.input import CompanyList, load_companies
from collector.export.excel import ExportError, export_jobs
from collector.pipelines import daily as daily_run
from collector.pipelines.ingest import CollectionError, run_collection
from collector.settings import get_settings
from collector.spiders.pages import crawl_discovery
from collector.spiders.run import SourceResult, crawl_sources
from collector.spiders.sources import Source, load_sources
from collector.utils.lock import LockBusy, database_lock

STALE_LISTED = 20  # stale candidates printed by `daily`; the count covers all of them

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
            help="CSV of configured sources: platform,company_name,identifier,company_website "
            "(and html_config for html sources).",
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
    could not be used or the run failed, 5 when another run (``run`` or ``daily``) is using the
    same database (nothing is done).
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
        with database_lock(database):
            summary = run_collection(database, configured, crawl)
    except LockBusy as exc:
        typer.echo(f"error: {exc}; nothing was done", err=True)
        raise typer.Exit(code=5) from exc
    except OSError as exc:
        typer.echo(f"error: cannot create the database lock: {exc.strerror}", err=True)
        raise typer.Exit(code=3) from exc
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
def daily(
    sources: Annotated[
        Path,
        typer.Option(
            "--sources",
            help="CSV of approved sources (as for `run`, e.g. from `sources export`).",
        ),
    ],
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="SQLite database (created if missing). Default: COLLECTOR_SQLITE_PATH, "
            "else data/collector.db.",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help="Directory for the dated workbook. Default: COLLECTOR_OUTPUT_DIR, else output/.",
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace today's workbook if it already exists."),
    ] = False,
) -> None:
    """Unattended daily collection: crawl the approved sources, store the jobs and write
    jobs_<YYYY-MM-DD>.xlsx (business date, COLLECTOR_TIMEZONE). Meant for cron or a systemd
    timer; it never prompts.

    One run per database at a time (a second one exits with code 5 without doing anything).
    Runs left RUNNING by an interrupted process are marked FAILED first. If some sources fail,
    the workbook is named jobs_<date>_partial_run<id>.xlsx; if none succeeds, no workbook is
    written. Jobs are never closed; jobs missing from the last two successful runs are listed
    as stale candidates for review. The workbook holds all open jobs within the date window,
    not only today's.

    Exit codes: 0 complete; 1 partial (some sources failed; partial workbook written); 2
    unusable sources file or today's workbook already exists (nothing crawled); 3 collection
    failed (database unusable, crawl failed or no source usable; no workbook); 4 the export
    failed after the jobs were stored; 5 another run is using the database; 130 interrupted.
    """
    try:
        configured = load_sources(sources)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    settings = get_settings()
    database = db if db is not None else settings.sqlite_path
    folder = output_dir if output_dir is not None else settings.output_dir
    crawled: list[SourceResult] = []

    def crawl(to_crawl: list[Source]) -> list[SourceResult]:
        crawled.extend(crawl_sources(to_crawl))
        return crawled

    try:
        report_ = daily_run.run_daily(configured, database, folder, crawl, overwrite=overwrite)
    except daily_run.OutputExists as exc:
        typer.echo(f"error: {exc} (use --overwrite to replace it); nothing was done", err=True)
        raise typer.Exit(code=2) from exc
    except LockBusy as exc:
        typer.echo(f"error: {exc}; nothing was done", err=True)
        raise typer.Exit(code=5) from exc
    except OSError as exc:
        typer.echo(f"error: cannot create the database lock: {exc.strerror}", err=True)
        raise typer.Exit(code=3) from exc
    except CollectionError as exc:
        _echo_sources(crawled)
        where = f" (run {exc.run_id} marked FAILED)" if exc.run_id is not None else ""
        typer.echo(f"DAILY FAILED: {exc}{where}; no workbook written", err=True)
        raise typer.Exit(code=3) from exc
    except KeyboardInterrupt as exc:
        typer.echo("DAILY INTERRUPTED: the run was marked FAILED; no workbook written", err=True)
        raise typer.Exit(code=130) from exc
    _echo_sources(crawled)
    _echo_daily(report_, crawled)
    if report_.export_error is not None:
        raise typer.Exit(code=4)
    codes = {
        daily_run.DailyOutcome.COMPLETE: 0,
        daily_run.DailyOutcome.PARTIAL: 1,
        daily_run.DailyOutcome.FAILED: 3,
    }
    raise typer.Exit(code=codes[report_.outcome])


def _echo_daily(report_: "daily_run.DailyReport", results: list[SourceResult]) -> None:
    """The daily summary: only counters the run and database actually provide."""
    summary = report_.ingest
    skipped = sum(len(e.skipped) for result in results for e in result.extractions)
    warnings = sum(len(e.warnings) for result in results for e in result.extractions)
    echo = typer.echo
    echo(
        f"DAILY {report_.business_date.isoformat()} {report_.outcome.value} "
        f"(run {summary.run_id} {summary.status})"
    )
    for run_id in report_.recovered_runs:
        echo(f"  recovered: run {run_id} was left RUNNING by an interrupted process; FAILED")
    echo(
        f"  started {report_.started_at}, finished {report_.finished_at} "
        f"({report_.duration_seconds:.0f} s)"
    )
    echo(
        f"  sources: {summary.sources} configured, {report_.sources_completed} completed, "
        f"{summary.sources_failed} failed ({summary.sources_blocked} blocked)"
    )
    echo(
        f"  jobs this run: {summary.jobs_new} new, {summary.jobs_updated} updated, "
        f"{summary.jobs_unchanged} unchanged ({summary.jobs_seen} stored); "
        f"{summary.extraction_failures} invalid, {skipped} skipped, "
        f"{summary.duplicates} duplicates, {warnings} warnings"
    )
    echo(
        f"  posted dates: {summary.eligible} eligible, {summary.unknown_date} unknown, "
        f"{summary.future_date} future, {summary.old_date} old (not stored)"
    )
    echo(
        f"  stale candidates: {len(report_.stale)} (open jobs missing from the last "
        f"{daily_run.STALE_AFTER_SUCCESSFUL_RUNS} successful runs; for review only)"
    )
    for candidate in report_.stale[:STALE_LISTED]:
        echo(
            f"    {candidate.company_name} | {candidate.title} | {candidate.platform} | "
            f"last seen {candidate.last_seen_at}"
        )
    if len(report_.stale) > STALE_LISTED:
        echo(f"    ... and {len(report_.stale) - STALE_LISTED} more")
    echo("  closed jobs: none (automatic closing is not supported by database schema v2)")
    if report_.workbook is not None:
        book = report_.workbook
        echo(
            f"  workbook: {book.path} ({book.rows} rows: every open job within the date "
            f"window, not only today's; {book.eligible} eligible, {book.unknown_date} unknown "
            f"date, {book.future_date} future date)"
        )
        if report_.outcome is daily_run.DailyOutcome.PARTIAL:
            echo("  note: some sources failed, so the workbook is named as partial")
    elif report_.export_error is not None:
        echo(f"EXPORT FAILED: {report_.export_error}; the collected jobs are stored", err=True)
    else:
        echo("  workbook: none (no source could be collected)")
    if summary.fetch_failures or summary.extraction_failures:
        echo(
            f"  errors: {summary.fetch_failures} fetch failures, "
            f"{summary.extraction_failures} extraction failures"
        )


@app.command()
def export(
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="Existing SQLite database to read (never modified). "
            "Default: COLLECTOR_SQLITE_PATH, else data/collector.db.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Excel file to write. Default: jobs.xlsx in COLLECTOR_OUTPUT_DIR, else output/.",
        ),
    ] = None,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace the output file if it already exists.")
    ] = False,
) -> None:
    """Export stored jobs to an Excel workbook (one "Jobs" sheet).

    Open jobs are exported, except postings older than the date window. Jobs without a posted
    date or with a future date are included and labelled in the "Date Status" column. No
    crawling happens and the database is opened read-only.

    Exit codes: 0 on success, 2 when the output file exists (without --overwrite) or cannot be
    written, 3 when the database is missing or unusable.
    """
    settings = get_settings()
    database = db if db is not None else settings.sqlite_path
    target = output if output is not None else settings.output_dir / "jobs.xlsx"
    try:
        summary = export_jobs(database, target, overwrite=overwrite)
    except ExportError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3 if exc.database else 2) from exc
    typer.echo(f"Export completed: {summary.rows} jobs written to {summary.path}")
    typer.echo(
        f"  {summary.eligible} eligible, {summary.unknown_date} unknown date, "
        f"{summary.future_date} future date (review); excluded: {summary.old_date} old, "
        f"{summary.closed} closed"
    )


@app.command()
def report() -> None:
    """Show a report of the last crawl (not implemented)."""
    _not_implemented("report")


# --- Phase 6: company-first source discovery --------------------------------------------------

sources_app = typer.Typer(
    help="Review discovered job sources, record collection decisions and export approved sources.",
    no_args_is_help=True,
)
app.add_typer(sources_app, name="sources")

StateOption = Annotated[
    Path | None,
    typer.Option(
        "--state",
        help="Discovery state file. Default: COLLECTOR_DISCOVERY_STATE_PATH, "
        "else data/discovery.json.",
    ),
]


def _state_path(state: Path | None) -> Path:
    return state if state is not None else get_settings().discovery_state_path


def _companies(path: Path) -> CompanyList:
    """Load the company list, reporting skipped rows; exit 2 if it is unusable or empty."""
    try:
        listed = load_companies(path)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for issue in listed.rejected:
        typer.echo(f"  line {issue.line}: rejected: {issue.reason}", err=True)
    for issue in listed.duplicates:
        typer.echo(f"  line {issue.line}: duplicate: {issue.reason}", err=True)
    if not listed.companies:
        typer.echo("error: the companies file lists no usable companies", err=True)
        raise typer.Exit(code=2)
    return listed


def _echo_coverage(state: registry.State, listed: CompanyList | None) -> None:
    keys = [company.key for company in listed.companies] if listed is not None else None
    cov = registry.coverage(state, keys)
    if listed is not None:
        typer.echo(
            f"INPUT: {listed.rows} rows, {len(listed.companies)} unique companies, "
            f"{len(listed.duplicates)} duplicates, {len(listed.rejected)} rejected"
        )
    typer.echo(
        f"DISCOVERY: {cov.processed} of {cov.companies} companies processed; "
        f"{cov.verified} verified, {cov.review_required} review required, "
        f"{cov.not_found} not found, {cov.unsupported} unsupported; "
        f"{cov.failures} discovery failures"
    )
    typer.echo(
        f"AUTHORIZATION: {cov.approved} approved, {cov.pending} pending, "
        f"{cov.restricted} restricted"
    )
    typer.echo(
        f"COVERAGE: discovery {cov.verified}/{cov.companies} "
        f"({cov.discovery_coverage:.1%}), crawler-ready {cov.crawler_ready}/{cov.companies} "
        f"({cov.crawler_ready_coverage:.1%})"
    )


@app.command()
def discover(
    companies: Annotated[
        Path,
        typer.Option(
            "--companies", help="CSV of companies: company_name,official_website,careers_url."
        ),
    ],
    state: StateOption = None,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            min=1,
            max=registry.MAX_BATCH_SIZE,
            help="How many due companies to process in this run.",
        ),
    ] = 10,
    recheck_after_days: Annotated[
        int | None,
        typer.Option(
            "--recheck-after-days",
            min=0,
            help="Also re-check companies last checked at least this many days ago.",
        ),
    ] = None,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="Retry companies whose discovery failed.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the results without saving them.")
    ] = False,
    online: Annotated[
        bool,
        typer.Option(
            "--online",
            help="Fetch official pages (and run the website lookup) through the protected "
            "crawler. Use only for companies you are authorized to contact.",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Website lookup for companies listed without a website: none or wikidata. "
            "Default: COLLECTOR_DISCOVERY_PROVIDER, else none. Used only with --online.",
        ),
    ] = None,
) -> None:
    """Discover job sources for the next batch of listed companies, in file order.

    Offline (the default): no page is fetched and no lookup is made. Supplied careers URLs are
    classified (Greenhouse, Lever, Ashby, JSON-LD candidate or unsupported) and kept as
    REVIEW_REQUIRED candidates; companies without enough input are NOT_FOUND.

    With --online: official websites (from the list, or from an exact-name directory match) and
    one careers page per company are fetched through the protected Scrapy crawler (robots.txt,
    HTTPS, public addresses, TLS, delay); job boards are never fetched. Sources confirmed on the
    official site become VERIFIED; everything else stays for review.

    Results are saved while the batch runs, so an interrupted run resumes where it stopped.
    Nothing is collected and no collection is ever authorized here.

    Exit codes: 0 when the batch was processed, 1 when it was processed but input rows were
    rejected, 2 for an unusable companies file or option, 3 for an unusable state file.
    """
    listed = _companies(companies)
    path = _state_path(state)
    name = provider if provider is not None else get_settings().discovery_provider
    try:
        directory = get_directory(name.strip().lower())
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if online:
        lookup = f"website lookup: {directory.name}" if directory else "no website lookup"
        typer.echo(
            "MODE: online (protected crawler: robots.txt, HTTPS, public addresses, TLS; "
            f"{lookup}; job boards are not fetched)"
            + ("; DRY RUN, nothing saved" if dry_run else "")
        )
    else:
        typer.echo(
            "MODE: offline (no pages fetched, no website search); candidate sources stay "
            "REVIEW_REQUIRED until reviewed" + ("; DRY RUN, nothing saved" if dry_run else "")
        )
    try:
        result = registry.run_batch(
            listed.companies,
            path,
            batch_size=batch_size,
            recheck_after=None if recheck_after_days is None else timedelta(recheck_after_days),
            retry_failed=retry_failed,
            dry_run=dry_run,
            directory=directory,
            driver=crawl_discovery if online else None,
        )
    except registry.StateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    if not result.processed:
        typer.echo("No companies are due for discovery (see --recheck-after-days, --retry-failed).")
    for company, record in result.processed:
        platform = f" [{record['platform']}]" if record["platform"] else ""
        detail = record["reason"] or (record["recheck"] or {}).get("reason")
        kept = " (kept; re-check did not confirm)" if record["recheck"] else ""
        typer.echo(
            f"{record['status']}{kept}: {company.name}{platform}"
            + (f" - {detail}" if detail else "")
        )
    counts = Counter(record["status"] for _, record in result.processed)
    typer.echo(
        f"BATCH: {len(result.processed)} processed ("
        + ", ".join(f"{counts[s]} {s.lower()}" for s in registry.Status)
        + f"); {result.kept_verified} verified kept; {result.remaining} still due"
    )
    _echo_coverage(result.state, listed)
    if listed.rejected:
        raise typer.Exit(code=1)


@sources_app.command("status")
def sources_status(
    companies: Annotated[
        Path | None,
        typer.Option("--companies", help="Count coverage over this company list only."),
    ] = None,
    state: StateOption = None,
) -> None:
    """Show discovery, authorization and coverage counts. Exit 2/3 for unusable files."""
    listed = _companies(companies) if companies is not None else None
    try:
        current = registry.load_state(_state_path(state))
    except registry.StateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _echo_coverage(current, listed)


def _decision_error(exc: Exception) -> NoReturn:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(code=3 if isinstance(exc, registry.StateError) else 2) from exc


@sources_app.command("verify")
def sources_verify(
    company: Annotated[str, typer.Argument(help="Company name as listed.")],
    by: Annotated[str, typer.Option("--by", help="Who reviewed the evidence.")],
    evidence: Annotated[
        str, typer.Option("--evidence", help="What confirms the source belongs to the company.")
    ],
    state: StateOption = None,
) -> None:
    """Confirm a REVIEW_REQUIRED candidate source after manual review (makes it VERIFIED).

    Only the recorded candidate can be confirmed; this does not approve collection.
    Exit codes: 0 confirmed, 2 refused, 3 unusable state file.
    """
    try:
        record = registry.confirm_source(_state_path(state), company, by=by, note=evidence)
    except (LookupError, ValueError, registry.StateError) as exc:
        _decision_error(exc)
    typer.echo(
        f"VERIFIED: {record['company_name']} [{record['platform']}] (authorization unchanged)"
    )


@sources_app.command("authorize")
def sources_authorize(
    company: Annotated[str, typer.Argument(help="Company name as listed.")],
    decision: Annotated[
        registry.Authorization,
        typer.Option("--decision", case_sensitive=False, help="The collection decision."),
    ],
    by: Annotated[str, typer.Option("--by", help="Who made the decision.")],
    note: Annotated[
        str, typer.Option("--note", help="The basis for the decision (e.g. written permission).")
    ],
    state: StateOption = None,
) -> None:
    """Record an explicit collection decision for a discovered company.

    APPROVED requires a VERIFIED source on a supported platform and applies to that source only.
    Exit codes: 0 recorded, 2 refused, 3 unusable state file.
    """
    try:
        entry = registry.authorize(_state_path(state), company, decision, by=by, note=note)
    except (LookupError, ValueError, registry.StateError) as exc:
        _decision_error(exc)
    typer.echo(f"{entry['decision']}: recorded for the company's current source")


@sources_app.command("export")
def sources_export(
    output: Annotated[
        Path, typer.Option("--output", help="Approved crawler sources CSV to write.")
    ],
    review_output: Annotated[
        Path | None,
        typer.Option(
            "--review-output",
            help="CSV of companies not exported, with reasons. Default: <output>_review.csv.",
        ),
    ] = None,
    state: StateOption = None,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace output files that already exist.")
    ] = False,
) -> None:
    """Write the VERIFIED and APPROVED sources in the format `collector run --sources` reads,
    plus a review file listing every other company and why it was left out. Nothing is crawled.

    Exit codes: 0 sources written, 1 no approved sources (only the review file is written),
    2 output exists (without --overwrite) or cannot be written, 3 unusable state file.
    """
    review = review_output or output.with_name(f"{output.stem}_review.csv")
    try:
        export = registry.approved_sources(registry.load_state(_state_path(state)))
    except registry.StateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    try:
        registry.write_source_files(export, output, review, overwrite=overwrite)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Review list: {len(export.review)} companies not exported, written to {review}")
    if not export.sources:
        typer.echo("No approved sources: no sources file was written.", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"Sources export completed: {len(export.sources)} approved sources written to {output}"
    )
