"""Explicitly configured crawl sources: the only things the spiders are allowed to fetch.

Phase 4 crawls nothing it was not told about: every source names its platform, the company and
one identifier (a board token / site slug / board name, or, for ``jsonld`` and ``html``, one
public HTTPS page URL). Validation happens *before* any request exists, so a configured
identifier can never turn into an arbitrary host or an unsafe request path. Importing or calling
this module never touches the network; the loader only reads the files it is given.

``html`` sources (Phase 8, generic HTML) also need a reviewed selector configuration: a JSON file
named in the optional ``html_config`` column (relative to the sources file). It holds the
selectors (``collector.adapters.html.parse_html_config``) and a ``review`` block recording who
confirmed that the page belongs to the company and who approved collection; only an explicit
``APPROVED`` decision is accepted. An ``html`` source may not point at a job board or ATS the
collector recognizes: supported boards have their own platform, and generic HTML is no way
around another ATS's access rules. ``PLATFORMS`` lists the one-request platforms that discovery
can export; ``SOURCE_PLATFORMS`` adds ``html``, which is only ever configured by hand.
"""

import csv
import io
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from collector.adapters.common import public_url
from collector.adapters.html import HtmlConfig, parse_html_config

PLATFORMS = ("greenhouse", "lever", "ashby", "jsonld")
HTML = "html"
SOURCE_PLATFORMS = (*PLATFORMS, HTML)
REVIEW_KEYS = ("verified_by", "ownership_evidence", "authorization", "authorized_by", "note")
MAX_REVIEW_TEXT = 500

# The same rule the adapters enforce for board/site identifiers: safe to embed in a URL path
# (no separators, spaces, quoting or traversal characters, and it cannot start with a dot).
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_REQUIRED_COLUMNS = ("platform", "company_name", "identifier")


@dataclass(frozen=True)
class Source:
    """One explicitly configured source.

    ``identifier`` is the platform's board token (Greenhouse), site slug (Lever) or board name
    (Ashby), or the one normalized public HTTPS page URL to fetch (``jsonld``; for ``html`` the
    first listing page). ``html`` is the reviewed selector configuration of an ``html`` source.
    """

    platform: str
    company_name: str
    identifier: str
    company_website: str | None = None
    html: HtmlConfig | None = None


def check_source(
    platform: str,
    company_name: str,
    identifier: str,
    company_website: str | None = None,
    html: HtmlConfig | None = None,
) -> Source:
    """Validate one source; raise ``ValueError`` if any part of it is unusable."""
    if platform not in SOURCE_PLATFORMS:
        raise ValueError(f"platform must be one of {', '.join(SOURCE_PLATFORMS)}")
    if not isinstance(company_name, str) or not company_name.strip():
        raise ValueError("company_name must be a non-blank string")
    if any(unicodedata.category(ch) == "Cc" for ch in company_name.strip()):
        raise ValueError("company_name must not contain control characters")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("identifier must be a non-blank string")
    identifier = identifier.strip()
    if platform in ("jsonld", HTML):
        try:
            identifier = public_url(identifier)
        except ValueError as exc:
            raise ValueError(f"{platform} page URL: {exc}") from exc
        if urlsplit(identifier).scheme != "https":
            raise ValueError(f"{platform} page URL must use https")
    elif not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"{platform} identifier must match [A-Za-z0-9][A-Za-z0-9._-]*")
    website = None
    if company_website is not None and str(company_website).strip():
        try:
            website = public_url(company_website)
        except ValueError as exc:
            raise ValueError(f"company_website: {exc}") from exc
    if platform == HTML:
        if not isinstance(html, HtmlConfig):
            raise ValueError("html sources need a reviewed selector configuration (html_config)")
        _check_not_a_known_board(identifier)
    elif html is not None:
        raise ValueError("a selector configuration is only used by html sources")
    return Source(platform, company_name.strip(), identifier, website, html)


def _check_not_a_known_board(url: str) -> None:
    """Platform priority: a recognized job board is never crawled as generic HTML."""
    from collector.detect.platforms import detect_board  # imported here: it imports this module

    board = detect_board(url)
    if board is None:
        return
    if board.supported:
        raise ValueError(f"the page is a {board.platform} board: use the {board.platform} platform")
    raise ValueError(
        f"the page belongs to {board.platform}, which the collector does not support; generic "
        "HTML is not a way around its access requirements"
    )


def load_html_config(path: str | Path) -> HtmlConfig:
    """Read an ``html`` source's reviewed selector configuration (a JSON object).

    Besides the selectors it must hold a ``review`` object with ``verified_by`` and
    ``ownership_evidence`` (who confirmed the page belongs to the company, and how),
    ``authorization`` (must be ``"APPROVED"``), ``authorized_by`` and ``note`` (who approved
    collection, and on what basis). Raises ``ValueError`` (``OSError`` if unreadable).
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data: Any = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"not valid JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object")
    selectors = dict(data)
    _check_review(selectors.pop("review", None))
    return parse_html_config(selectors)


def _check_review(review: Any) -> None:
    if not isinstance(review, dict):
        raise ValueError("needs a review object recording verification and authorization")
    unknown = sorted(set(review) - set(REVIEW_KEYS))
    if unknown:
        raise ValueError(f"unknown review key(s): {', '.join(unknown)}")
    for key in REVIEW_KEYS:
        value = review.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"review.{key} is required")
        if len(value) > MAX_REVIEW_TEXT or any(unicodedata.category(c) == "Cc" for c in value):
            raise ValueError(f"review.{key} is too long or contains control characters")
    if review["authorization"].strip() != "APPROVED":
        raise ValueError("collection is not approved (review.authorization must be APPROVED)")


def load_sources(path: str | Path) -> list[Source]:
    """Read sources from a CSV file with the header
    ``platform,company_name,identifier,company_website`` (the website column is optional), plus
    an optional ``html_config`` column: for an ``html`` source, the path of its reviewed
    selector configuration (see ``load_html_config``), relative to the sources file.

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
                html = _row_html_config(path, row.get("html_config"))
                source = check_source(
                    (row.get("platform") or "").strip().lower(),
                    row.get("company_name") or "",
                    row.get("identifier") or "",
                    row.get("company_website"),
                    html,
                )
            except ValueError as exc:
                raise ValueError(f"{path}, line {line}: {exc}") from exc
            key = (
                source.platform,
                source.identifier
                if source.platform in ("jsonld", HTML)
                else source.identifier.casefold(),
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


def _row_html_config(sources_file: Path, value: str | None) -> HtmlConfig | None:
    if value is None or not value.strip():
        return None
    try:
        return load_html_config(sources_file.parent / value.strip())
    except OSError as exc:
        raise ValueError(f"html_config cannot be read ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise ValueError(f"html_config: {exc}") from exc
