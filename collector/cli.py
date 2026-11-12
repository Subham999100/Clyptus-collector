"""Command-line interface. Phase 0: every command is a clearly-labelled placeholder."""

import typer

app = typer.Typer(
    name="collector",
    help="Collect publicly available job postings and export them for the Clyptus Job Portal.",
    no_args_is_help=True,
    add_completion=False,
)


def _not_implemented(command: str) -> None:
    """Report that a command has no functionality yet, and fail. Does no work."""
    typer.echo(f"'collector {command}' is not implemented yet (Phase 0 only).", err=True)
    raise typer.Exit(code=1)


@app.command()
def run() -> None:
    """Crawl company career sites (not implemented)."""
    _not_implemented("run")


@app.command()
def export() -> None:
    """Export collected jobs to Excel (not implemented)."""
    _not_implemented("export")


@app.command()
def report() -> None:
    """Show a report of the last crawl (not implemented)."""
    _not_implemented("report")
