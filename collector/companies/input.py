"""The user-supplied company list: the only input to source discovery.

A UTF-8 CSV with the header ``company_name,official_website,careers_url``. Only
``company_name`` is required; the two URL columns are optional (whole column or per row). Each
row names one company the user wants covered; the URLs are what the user already knows and are
*claims to be checked*, never verified facts. Nothing is fetched here.

Structural problems (no ``company_name`` column, unknown or repeated columns, malformed CSV,
unreadable file) raise ``ValueError``/``OSError``. Problems in a single row are collected as
``InputIssue`` objects (line number and a reason that never echoes the value) and that row is
skipped, so one bad row does not block a 1,000-company list.

Duplicates are detected conservatively: two rows are the same company only when their names are
identical after Unicode (NFKC) normalization, case folding and whitespace collapsing. Repeats are
skipped; if a repeat supplies different URLs (including a URL where the first row had none) the
company is marked as conflicting and discovery leaves it for review. Names that differ only in
legal form or punctuation ("Acme Inc" / "ACME Ltd.") are never merged; both are marked as
similar so a reviewer confirms which is which.
"""

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from collector.adapters.common import public_url

COLUMNS = ("company_name", "official_website", "careers_url")
MAX_NAME_LENGTH = 200
MAX_URL_LENGTH = 2000

_FORMULA_PREFIXES = ("=", "+", "-", "@")
# Legal-form words dropped from the end of a name when looking for *similar* names only.
_LEGAL_FORMS = frozenset(
    {
        "ag",
        "bv",
        "co",
        "company",
        "corp",
        "corporation",
        "gmbh",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "llp",
        "ltd",
        "nv",
        "plc",
        "private",
        "pte",
        "pty",
        "pvt",
        "sa",
    }
)
_NON_WORD = re.compile(r"[^\w]+")


@dataclass(frozen=True)
class Company:
    """One company to discover. ``key`` identifies it (see ``company_key``); ``line`` is its first
    row in the input file. ``conflict``/``similar`` explain why its identity needs review."""

    name: str
    key: str
    line: int
    official_website: str | None = None
    careers_url: str | None = None
    conflict: str | None = None
    similar: str | None = None


@dataclass(frozen=True)
class InputIssue:
    line: int
    reason: str


@dataclass(frozen=True)
class CompanyList:
    companies: tuple[Company, ...]  # unique companies, in file order
    rejected: tuple[InputIssue, ...]  # unusable rows (skipped)
    duplicates: tuple[InputIssue, ...]  # repeated rows (skipped)
    rows: int  # non-blank data rows read


def company_key(name: str) -> str:
    """Identity of a company name: NFKC, case-folded, whitespace collapsed. Nothing else."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def name_tokens(name: str) -> tuple[str, ...]:
    """Words of a name with punctuation removed and trailing legal forms dropped
    ("Acme Pvt. Ltd." -> ("acme",)). Used for similarity and website-name checks only."""
    words = _NON_WORD.sub(" ", company_key(name).replace("_", " ")).split()
    while len(words) > 1 and words[-1] in _LEGAL_FORMS:
        words.pop()
    return tuple(words)


def check_company_name(value: str) -> str:
    """Return the trimmed name or raise ``ValueError`` (the message never contains the value)."""
    name = value.strip()
    if not name:
        raise ValueError("company_name is blank")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"company_name is longer than {MAX_NAME_LENGTH} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise ValueError("company_name contains control characters")
    if name.startswith(_FORMULA_PREFIXES):
        raise ValueError("company_name must not start with =, +, - or @")
    if not any(ch.isalnum() for ch in name):
        raise ValueError("company_name has no letters or digits")
    return name


def check_https_url(value: str | None, field: str) -> str | None:
    """A blank value is ``None``; anything else must be a public HTTPS URL (normalized)."""
    if value is None or not value.strip():
        return None
    if len(value.strip()) > MAX_URL_LENGTH:
        raise ValueError(f"{field} is longer than {MAX_URL_LENGTH} characters")
    try:
        url = public_url(value)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc
    if urlsplit(url).scheme != "https":
        raise ValueError(f"{field} must use https")
    return url


def load_companies(path: str | Path) -> CompanyList:
    """Read and validate a company list (see the module docstring)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    entries: list[Company] = []
    rejected: list[InputIssue] = []
    duplicates: list[InputIssue] = []
    rows = 0
    by_key: dict[str, int] = {}
    conflicts: dict[str, list[int]] = {}
    try:
        fields = [(name or "").strip() for name in (reader.fieldnames or [])]
        if "company_name" not in fields:
            raise ValueError(f"{path}: companies file lacks column(s): company_name")
        unknown = sorted({name for name in fields if name not in COLUMNS})
        if unknown:
            raise ValueError(
                f"{path}: unexpected column(s) {', '.join(unknown)}; allowed: {', '.join(COLUMNS)}"
            )
        if len(set(fields)) != len(fields):
            raise ValueError(f"{path}: a column appears more than once in the header")
        reader.fieldnames = fields
        for row in reader:
            line = reader.line_num
            extra = row.pop(None, None)
            if not extra and not any((value or "").strip() for value in row.values()):
                continue  # blank line
            rows += 1
            if extra:
                rejected.append(InputIssue(line, "row has more fields than the header"))
                continue
            try:
                name = check_company_name(row.get("company_name") or "")
                website = check_https_url(row.get("official_website"), "official_website")
                careers = check_https_url(row.get("careers_url"), "careers_url")
            except ValueError as exc:
                rejected.append(InputIssue(line, str(exc)))
                continue
            key = company_key(name)
            if key in by_key:
                first = entries[by_key[key]]
                duplicates.append(InputIssue(line, f"repeats the company on line {first.line}"))
                if (website, careers) != (first.official_website, first.careers_url):
                    conflicts.setdefault(key, [first.line]).append(line)
                continue
            by_key[key] = len(entries)
            entries.append(Company(name, key, line, website, careers))
    except csv.Error as exc:
        raise ValueError(f"{path}, line {reader.line_num}: malformed CSV ({exc})") from exc

    by_tokens: dict[tuple[str, ...], list[Company]] = {}
    for entry in entries:
        by_tokens.setdefault(name_tokens(entry.name), []).append(entry)
    companies: list[Company] = []
    for entry in entries:
        conflict = None
        if entry.key in conflicts:
            lines = ", ".join(str(n) for n in conflicts[entry.key])
            conflict = f"rows with different URLs for this company (lines {lines})"
        others = [c.line for c in by_tokens[name_tokens(entry.name)] if c is not entry]
        similar = (
            f"name is similar to another listed company (line {', '.join(map(str, others))})"
            if others
            else None
        )
        companies.append(
            Company(
                entry.name,
                entry.key,
                entry.line,
                entry.official_website,
                entry.careers_url,
                conflict,
                similar,
            )
        )
    return CompanyList(tuple(companies), tuple(rejected), tuple(duplicates), rows)
