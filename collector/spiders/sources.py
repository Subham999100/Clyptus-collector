"""Explicitly configured crawl sources: the only things the spiders are allowed to fetch.

Phase 4 crawls nothing it was not told about: every source names its platform, the company and
one identifier (a board token / site slug / board name, or, for ``jsonld``, one public HTTPS
page URL). There is no ATS detection, no discovery and no link following. Validation happens
*before* any request exists, so a configured identifier can never turn into an arbitrary host or
an unsafe request path. Importing or calling this module never touches the network; the loader
only reads the file it is given.
"""

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from collector.adapters.common import public_url

PLATFORMS = ("greenhouse", "lever", "ashby", "jsonld")

# The same rule the adapters enforce for board/site identifiers: safe to embed in a URL path
# (no separators, spaces, quoting or traversal characters, and it cannot start with a dot).
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_REQUIRED_COLUMNS = ("platform", "company_name", "identifier")


@dataclass(frozen=True)
class Source:
    """One explicitly configured source.

    ``identifier`` is the platform's board token (Greenhouse), site slug (Lever) or board name
    (Ashby), or the one normalized public HTTPS page URL to fetch (``jsonld``).
    """

    platform: str
    company_name: str
    identifier: str
    company_website: str | None = None


def check_source(
    platform: str,
    company_name: str,
    identifier: str,
    company_website: str | None = None,
) -> Source:
    """Validate one source; raise ``ValueError`` if any part of it is unusable."""
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {', '.join(PLATFORMS)}")
    if not isinstance(company_name, str) or not company_name.strip():
        raise ValueError("company_name must be a non-blank string")
    if any(unicodedata.category(ch) == "Cc" for ch in company_name.strip()):
        raise ValueError("company_name must not contain control characters")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("identifier must be a non-blank string")
    identifier = identifier.strip()
    if platform == "jsonld":
        try:
            identifier = public_url(identifier)
        except ValueError as exc:
            raise ValueError(f"jsonld page URL: {exc}") from exc
        if urlsplit(identifier).scheme != "https":
            raise ValueError("jsonld page URL must use https")
    elif not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"{platform} identifier must match [A-Za-z0-9][A-Za-z0-9._-]*")
    website = None
    if company_website is not None and str(company_website).strip():
        try:
            website = public_url(company_website)
        except ValueError as exc:
            raise ValueError(f"company_website: {exc}") from exc
    return Source(platform, company_name.strip(), identifier, website)


def load_sources(path: str | Path) -> list[Source]:
    """Read sources from a CSV file with the header
    ``platform,company_name,identifier,company_website`` (the website column is optional).

    Blank lines are skipped. Raises ``ValueError`` (naming the line) for a malformed row, a row
    with extra fields, a duplicate source, a missing column, or a file with no sources; raises
    ``OSError`` if the file cannot be read. A duplicate is the same platform and identifier
    (compared case-insensitively, except for ``jsonld`` page URLs); it is rejected, never
    silently merged.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    try:
        fields = reader.fieldnames or []
        missing = [column for column in _REQUIRED_COLUMNS if column not in fields]
        if missing:
            raise ValueError(f"{path}: sources file lacks column(s): {', '.join(missing)}")
        sources: list[Source] = []
        first_line: dict[tuple[str, str], int] = {}
        for row in reader:
            line = reader.line_num
            extra = row.pop(None, None)  # DictReader keeps surplus fields under the key None
            if not extra and not any((value or "").strip() for value in row.values()):
                continue  # a blank line is not a source
            if extra:
                raise ValueError(f"{path}, line {line}: row has more fields than the header")
            try:
                source = check_source(
                    (row.get("platform") or "").strip().lower(),
                    row.get("company_name") or "",
                    row.get("identifier") or "",
                    row.get("company_website"),
                )
            except ValueError as exc:
                raise ValueError(f"{path}, line {line}: {exc}") from exc
            key = (
                source.platform,
                source.identifier if source.platform == "jsonld" else source.identifier.casefold(),
            )
            if key in first_line:
                raise ValueError(
                    f"{path}, line {line}: duplicate source (first configured on line "
                    f"{first_line[key]})"
                )
            first_line[key] = line
            sources.append(source)
    except csv.Error as exc:
        raise ValueError(f"{path}, line {reader.line_num}: malformed CSV ({exc})") from exc
    if not sources:
        raise ValueError(f"{path}: no sources configured")
    return sources
