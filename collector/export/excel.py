"""Excel export of stored jobs: one ``Jobs`` worksheet, built for people, not machines.

Reads the SQLite database **read-only** (it is opened with ``mode=ro``; nothing is initialized,
migrated or written) and streams rows in batches into an openpyxl write-only workbook, so
neither the database nor the workbook is held in memory as a whole.

Which jobs
    OPEN jobs only (CLOSED ones are excluded). Each job's stored ``posted_date`` is classified
    with the existing ``check_date_window`` (business timezone and window from ``Settings``):
    OLD_DATE jobs are excluded; ELIGIBLE (including the one-day future tolerance), UNKNOWN_DATE
    and FUTURE_DATE jobs are exported, with the outcome spelled out in the "Date Status" column.
    A missing posted date is never replaced by any other date.

Order (deterministic)
    Known posted dates first, newest first; then jobs without a posted date; ties broken by the
    stable ``job_key``. Unchanged data therefore always exports in the same order.

Safety
    Every scraped string is written as a literal text cell (never a formula) and additionally
    marked "treat as text" (Excel's quote prefix), so content such as ``=HYPERLINK(...)``,
    ``+1``, ``-1`` or ``@SUM`` is shown, never evaluated, even after the cell is edited.
    Characters that are illegal in XLSX are dropped and text is cut at Excel's 32,767-character
    cell limit. URLs are shown only if they pass the collector's public-URL check (http(s), no
    credentials, public host); any other stored URL is left blank. The application URL is
    clickable.

Memory
    Rows are streamed, so memory does not grow with the number of rows, except for hyperlinks:
    openpyxl keeps each one until it writes them after the rows. Measured on Windows: about
    62 MB peak working set for 80,000 rows without links; each clickable link adds about 2 KB
    until the file is saved, which is why only the application URL is a link.

Output
    Written to a temporary file next to the target and moved into place only when complete; an
    existing file is replaced only when ``overwrite`` is set.
"""

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.styles.cell_style import StyleArray
from openpyxl.utils import get_column_letter

from collector.adapters.common import public_url
from collector.models import JobRecord
from collector.pipelines.date_window import DateOutcome, DateWarning, check_date_window
from collector.pipelines.store import SCHEMA_VERSION
from collector.settings import get_settings

SHEET_NAME = "Jobs"
BATCH_SIZE = 1000
EXCEL_TEXT_LIMIT = 32767  # characters per cell
EXCEL_URL_LIMIT = 2000  # longer hyperlinks make Excel report the file as damaged

# (header, database column, column width)
COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("Company Name", "company_name", 28),
    ("Job Title", "title", 42),
    ("Location", "location", 26),
    ("Work Type", "work_type", 12),
    ("Employment Type", "employment_type", 18),
    ("Salary", "salary_text", 20),
    ("Posted Date", "posted_date", 13),
    ("Posted Date Source", "posted_date_source", 18),
    ("Date Status", "date_status", 26),
    ("Job Description", "description_snippet", 70),
    ("Application URL", "apply_url", 45),
    ("Source URL", "source_url", 45),
    ("Platform", "platform", 13),
    ("First Seen", "first_seen_at", 18),
    ("Last Seen", "last_seen_at", 18),
)
HEADERS = tuple(header for header, _, _ in COLUMNS)

_SELECT = """
    SELECT company_name, title, location, work_type, employment_type, salary_text,
           posted_date, posted_date_source, description_snippet, apply_url, source_url,
           platform, first_seen_at, last_seen_at
    FROM jobs
    WHERE status = 'OPEN'
    ORDER BY posted_date IS NULL, posted_date DESC, job_key
"""

_DATE_SOURCES = {
    "platform": "Platform",
    "jsonld": "JSON-LD",
    "page_text": "Page text",
    "unknown": "Unknown",
}
_PLATFORMS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "jsonld": "JSON-LD",
}
_WORK_TYPES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}

_ORANGE = "F26B21"
_HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor=_ORANGE)
_HEADER_ALIGNMENT = Alignment(vertical="center", wrap_text=True)
_HEADER_BORDER = Border(bottom=Side(style="thin", color="C2410C"))
_BODY_FONT = Font(name="Calibri", size=11)
_LINK_FONT = Font(name="Calibri", size=11, color="0563C1", underline="single")
_TOP = Alignment(vertical="top")
_WRAP = Alignment(vertical="top", wrap_text=True)


class ExportError(Exception):
    """An expected, user-facing failure (missing database, existing output, ...). The message
    is safe to show: it names paths and causes, never stored job content. ``database`` tells a
    database problem from an output problem."""

    def __init__(self, message: str, *, database: bool = False) -> None:
        super().__init__(message)
        self.database = database


@dataclass(frozen=True)
class ExportSummary:
    path: Path
    rows: int  # jobs written to the workbook
    eligible: int
    unknown_date: int
    future_date: int  # exported, flagged for review
    old_date: int  # excluded
    closed: int  # excluded


def date_status(
    record_date: date | None, reference: datetime | None, window_days: int, tz: tzinfo
) -> tuple[DateOutcome, str]:
    """The existing date-window outcome for a stored posted date, and its worksheet label.
    (Only ``posted_date`` is read by the date window, so no other field is needed.)"""
    record = JobRecord.model_construct(posted_date=record_date)
    result = check_date_window(record, reference, window_days, tz)
    if result.outcome is DateOutcome.ELIGIBLE:
        if result.warning is DateWarning.FUTURE_WITHIN_TOLERANCE:
            return result.outcome, "Eligible (dated 1 day ahead)"
        return result.outcome, "Eligible"
    if result.outcome is DateOutcome.UNKNOWN_DATE:
        return result.outcome, "Unknown date - review"
    if result.outcome is DateOutcome.FUTURE_DATE:
        return result.outcome, "Future date - review"
    return result.outcome, "Old"


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise ExportError(f"database not found: {db_path}", database=True)
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ExportError(
            f"cannot open database {db_path}: {type(exc).__name__}", database=True
        ) from exc
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise ExportError(f"{db_path} is not a collector database", database=True) from exc
    if version != SCHEMA_VERSION:
        conn.close()
        raise ExportError(
            f"{db_path} has database schema version {version}; this export needs version "
            f"{SCHEMA_VERSION}",
            database=True,
        )
    return conn


def _rows(conn: sqlite3.Connection) -> Iterator[tuple[Any, ...]]:
    """Stored OPEN jobs in export order, fetched in batches (never all at once)."""
    cursor = conn.execute(_SELECT)
    try:
        while batch := cursor.fetchmany(BATCH_SIZE):
            yield from batch
    finally:
        cursor.close()


def _clean(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value)[:EXCEL_TEXT_LIMIT]


def _style(
    ws: Any,
    *,
    font: Font = _BODY_FONT,
    alignment: Alignment = _TOP,
    number_format: str | None = None,
    as_text: bool = False,
) -> StyleArray:
    """A cell style built once and copied onto every cell that uses it (building styles cell
    by cell is what makes large exports slow)."""
    cell = WriteOnlyCell(ws, None)
    cell.font = font
    cell.alignment = alignment
    if number_format:
        cell.number_format = number_format
    if as_text:
        cell._style.quotePrefix = 1  # Excel's "treat as text": editing cannot make a formula
    return cell._style


def _styles(ws: Any) -> dict[str, StyleArray]:
    return {
        "text": _style(ws, as_text=True),
        "wrap": _style(ws, alignment=_WRAP, as_text=True),
        "link": _style(ws, font=_LINK_FONT, as_text=True),
        "date": _style(ws, number_format="yyyy-mm-dd"),
        "moment": _style(ws, number_format="yyyy-mm-dd hh:mm"),
    }


def _cell(ws: Any, value: Any, style: StyleArray) -> WriteOnlyCell:
    text = isinstance(value, str)
    cell = WriteOnlyCell(ws, _clean(value) if text else value)
    if text:
        cell.data_type = "s"  # literal text: openpyxl would otherwise turn "=..." into a formula
    cell._style = StyleArray(style)
    return cell


def _link(
    ws: Any, url: str | None, styles: dict[str, StyleArray], *, clickable: bool = True
) -> WriteOnlyCell:
    """A public http(s) URL as text, clickable if ``clickable``. Anything else (a credential-
    bearing or non-public URL) is left blank rather than shown, so it is never exposed."""
    if url is None:
        return _cell(ws, None, styles["text"])
    try:
        safe = public_url(url)
    except ValueError:
        return _cell(ws, None, styles["text"])
    if not clickable or len(safe) > EXCEL_URL_LIMIT:  # (Excel cannot open longer links)
        return _cell(ws, safe, styles["text"])
    cell = _cell(ws, safe, styles["link"])
    cell.hyperlink = safe
    return cell


def _moment(stamp: str | None, tz: tzinfo) -> datetime | None:
    """A stored UTC timestamp in the business timezone (Excel has no time zones)."""
    if not stamp:
        return None
    return datetime.fromisoformat(stamp).astimezone(tz).replace(tzinfo=None)


def _write(ws: Any, conn: sqlite3.Connection, reference: datetime | None) -> dict[str, int]:
    settings = get_settings()  # read once: the date window would otherwise reread it per row
    tz, window_days = settings.tz, settings.date_window_days
    if reference is None:
        reference = datetime.now(UTC)  # one reference moment for the whole export
    styles = _styles(ws)
    counts = {"rows": 0, "eligible": 0, "unknown_date": 0, "future_date": 0, "old_date": 0}
    for row in _rows(conn):
        (company, title, location, work_type, employment, salary, posted, posted_source,
         description, apply_url, source_url, platform, first_seen, last_seen) = row  # fmt: skip
        posted_date = date.fromisoformat(posted) if posted else None
        outcome, label = date_status(posted_date, reference, window_days, tz)
        if outcome is DateOutcome.OLD_DATE:
            counts["old_date"] += 1
            continue
        counts[outcome.value.lower()] += 1
        counts["rows"] += 1
        ws.append(
            [
                _cell(ws, company, styles["text"]),
                _cell(ws, title, styles["wrap"]),
                _cell(ws, location, styles["wrap"]),
                _cell(ws, _WORK_TYPES.get(work_type, work_type), styles["text"]),
                _cell(ws, employment, styles["text"]),
                _cell(ws, salary, styles["text"]),
                _cell(ws, posted_date, styles["date"]),
                _cell(ws, _DATE_SOURCES.get(posted_source, posted_source), styles["text"]),
                _cell(ws, label, styles["text"]),
                _cell(ws, description, styles["wrap"]),
                _link(ws, apply_url, styles),
                _link(ws, source_url, styles, clickable=False),
                _cell(ws, _PLATFORMS.get(platform, platform), styles["text"]),
                _cell(ws, _moment(first_seen, tz), styles["moment"]),
                _cell(ws, _moment(last_seen, tz), styles["moment"]),
            ]
        )
    return counts


def _workbook(conn: sqlite3.Connection, target: Path, reference: datetime | None) -> dict[str, int]:
    wb = Workbook(write_only=True)
    wb.properties.creator = "Clyptus Collector"
    wb.properties.title = "Clyptus Collector jobs"
    ws = wb.create_sheet(SHEET_NAME)
    ws.freeze_panes = "A2"  # set before any row: write-only sheets emit it first
    for index, (_, _, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    header = []
    for title in HEADERS:
        cell = WriteOnlyCell(ws, title)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGNMENT
        cell.border = _HEADER_BORDER
        header.append(cell)
    ws.append(header)
    try:
        counts = _write(ws, conn, reference)
        ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{counts['rows'] + 1}"
        wb.save(target)
    except BaseException:
        if not ws.closed:  # finish openpyxl's streaming writer so nothing is left dangling
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - the original failure is the one that matters
                pass
        raise
    return counts


def export_jobs(
    db_path: str | Path,
    output_path: str | Path,
    *,
    overwrite: bool = False,
    reference: datetime | None = None,
) -> ExportSummary:
    """Export the stored jobs to ``output_path`` and return what was written.

    ``reference`` (timezone-aware; default: now) is the moment the date window is evaluated at.
    Raises ``ExportError`` for a missing or unsupported database, or an output path that exists
    (without ``overwrite``) or is a directory. The database is never modified.
    """
    db_path, output = Path(db_path), Path(output_path)
    if output.is_dir():
        raise ExportError(f"output path is a directory: {output}")
    if output.exists() and not overwrite:
        raise ExportError(f"output file already exists: {output} (use --overwrite to replace it)")
    conn = _open_read_only(db_path)
    temporary: Path | None = None
    try:
        try:
            closed = conn.execute("SELECT COUNT(*) FROM jobs WHERE status != 'OPEN'").fetchone()[0]
        except sqlite3.Error as exc:
            raise ExportError(
                f"cannot read jobs from {db_path}: {type(exc).__name__}", database=True
            ) from exc
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(dir=output.parent, suffix=".xlsx.tmp")
        except OSError as exc:
            raise ExportError(f"cannot write to {output.parent}: {exc.strerror}") from exc
        os.close(handle)
        temporary = Path(name)
        try:
            counts = _workbook(conn, temporary, reference)
        except sqlite3.Error as exc:
            raise ExportError(
                f"cannot read jobs from {db_path}: {type(exc).__name__}", database=True
            ) from exc
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise ExportError(f"cannot write {output}: {reason}") from exc
        if output.exists() and not overwrite:  # appeared while exporting
            raise ExportError(f"output file already exists: {output}")
        try:
            os.replace(temporary, output)
        except OSError as exc:
            raise ExportError(f"cannot write {output}: {exc.strerror}") from exc
        temporary = None
    finally:
        conn.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return ExportSummary(
        path=output,
        rows=counts["rows"],
        eligible=counts["eligible"],
        unknown_date=counts["unknown_date"],
        future_date=counts["future_date"],
        old_date=counts["old_date"],
        closed=closed,
    )
