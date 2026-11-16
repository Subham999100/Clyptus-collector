"""Career-platform detection from URLs and already-fetched pages. Pure: never fetches anything.

``detect_board`` recognizes a job-board URL by its documented host and path structure only:

- Greenhouse: ``boards.greenhouse.io/<token>``, ``job-boards.greenhouse.io/<token>``, the embed
  ``boards.greenhouse.io/embed/job_board[/js]?for=<token>`` and the API
  ``boards-api.greenhouse.io/v1/boards/<token>/...``.
- Lever: ``jobs.lever.co/<site>`` and ``api.lever.co/v0/postings/<site>``.
- Ashby: ``jobs.ashbyhq.com/<board>`` and ``api.ashbyhq.com/posting-api/job-board/<board>``.

The identifier is read from the URL, never derived from a company name, and must pass the same
rule the crawler applies (``check_source``). The collector fetches the US API hosts only, so EU
Greenhouse/Lever boards are reported as unsupported, as are other known ATS tenants (Workday,
Keka, Darwinbox, Zoho Recruit ...). JSON-LD cannot be recognized from a URL; it needs the page
itself (see ``collector.companies.discovery``).

``read_page`` lists the names a page gives itself (``<title>``, ``og:site_name``, JSON-LD
``Organization`` names) and its outgoing links/embeds, resolved and checked with ``public_url``.
"""

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.parser import HTMLParser

from collector.adapters.common import public_url, without_control_characters
from collector.spiders.sources import check_source
from collector.utils.urls import registered_domain

SUPPORTED_BOARDS = ("greenhouse", "lever", "ashby")
MAX_LINKS = 2000  # per page; links beyond this are ignored
MAX_NAME_LENGTH = 300

_GREENHOUSE_HOSTS = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})
_EU_HOSTS = {
    "boards.eu.greenhouse.io": "greenhouse-eu",
    "job-boards.eu.greenhouse.io": "greenhouse-eu",
    "boards-api.eu.greenhouse.io": "greenhouse-eu",
    "jobs.eu.lever.co": "lever-eu",
    "api.eu.lever.co": "lever-eu",
}
# Other ATS products, recognized by the registered domain of a tenant subdomain.
_UNSUPPORTED_DOMAINS = {
    "bamboohr.com": "bamboohr",
    "breezy.hr": "breezy",
    "darwinbox.com": "darwinbox",
    "darwinbox.in": "darwinbox",
    "freshteam.com": "freshteam",
    "icims.com": "icims",
    "jobvite.com": "jobvite",
    "keka.com": "keka",
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
    "personio.com": "personio",
    "personio.de": "personio",
    "recruitee.com": "recruitee",
    "smartrecruiters.com": "smartrecruiters",
    "successfactors.com": "successfactors",
    "successfactors.eu": "successfactors",
    "taleo.net": "taleo",
    "teamtailor.com": "teamtailor",
    "workable.com": "workable",
    "zohorecruit.com": "zoho-recruit",
    "zohorecruit.eu": "zoho-recruit",
    "zohorecruit.in": "zoho-recruit",
}
_CAREERS_TEXT = re.compile(
    r"\b(careers?|jobs?|join (us|our team)|work (with|at|for) us|open (roles|positions)|"
    r"vacanc(y|ies)|we('| a)re hiring)\b",
    re.IGNORECASE,
)
_CAREERS_PATH = re.compile(
    r"(^|/)(careers?|jobs?|join-us|joinus|work-with-us|vacancies|openings)(/|$|\.)",
    re.IGNORECASE,
)
_ORGANIZATION_TYPES = frozenset({"organization", "corporation"})


@dataclass(frozen=True)
class Board:
    """A job-board URL. ``identifier`` is ``None`` when the URL has no valid one; ``supported``
    says whether the collector has a spider for the platform."""

    platform: str
    identifier: str | None
    supported: bool


@dataclass(frozen=True)
class Link:
    url: str  # normalized public URL
    text: str  # anchor text ("" for embeds)


@dataclass(frozen=True)
class PageSignals:
    names: tuple[str, ...]
    links: tuple[Link, ...]


def _valid(platform: str, identifier: str | None) -> str | None:
    if not identifier:
        return None
    try:
        return check_source(platform, "-", identifier).identifier
    except ValueError:
        return None


def detect_board(url: str) -> Board | None:
    """Classify a URL as a job board, or ``None`` when its host is not a known ATS."""
    try:
        parts = urlsplit(public_url(url))
    except ValueError:
        return None
    host = parts.hostname or ""
    segments = [segment for segment in parts.path.split("/") if segment]
    first = segments[0] if segments else None
    if host in _EU_HOSTS:
        return Board(_EU_HOSTS[host], None, False)
    if host in _GREENHOUSE_HOSTS:
        if first == "embed":
            token = (parse_qs(parts.query).get("for") or [None])[0]
            return Board("greenhouse", _valid("greenhouse", token), True)
        return Board("greenhouse", _valid("greenhouse", first), True)
    if host == "boards-api.greenhouse.io":
        token = segments[2] if segments[:2] == ["v1", "boards"] and len(segments) > 2 else None
        return Board("greenhouse", _valid("greenhouse", token), True)
    if host == "jobs.lever.co":
        return Board("lever", _valid("lever", first), True)
    if host == "api.lever.co":
        slug = segments[2] if segments[:2] == ["v0", "postings"] and len(segments) > 2 else None
        return Board("lever", _valid("lever", slug), True)
    if host == "jobs.ashbyhq.com":
        return Board("ashby", _valid("ashby", first), True)
    if host == "api.ashbyhq.com":
        name = (
            segments[2]
            if segments[:2] == ["posting-api", "job-board"] and len(segments) > 2
            else None
        )
        return Board("ashby", _valid("ashby", name), True)
    domain = registered_domain(url)
    if domain in _UNSUPPORTED_DOMAINS and host not in (domain, f"www.{domain}"):
        return Board(_UNSUPPORTED_DOMAINS[domain], None, False)
    return None


def is_careers_link(link: Link) -> bool:
    """Whether a link looks like a careers page, by its text or its path."""
    return bool(_CAREERS_TEXT.search(link.text) or _CAREERS_PATH.search(urlsplit(link.url).path))


def read_page(html: str, page_url: str) -> PageSignals:
    """Names and links of one fetched page. Nothing is executed or loaded."""
    markup = without_control_characters(html)
    if not markup.strip():
        return PageSignals((), ())
    tree = HTMLParser(markup)
    names: list[str] = []

    def add_name(value: object) -> None:
        if isinstance(value, str) and value.strip() and len(value) <= MAX_NAME_LENGTH:
            names.append(" ".join(value.split()))

    title = tree.css_first("title")
    if title is not None:
        add_name(title.text(deep=True))
    for meta in tree.css("meta"):
        if (meta.attributes.get("property") or "").strip().casefold() == "og:site_name":
            add_name(meta.attributes.get("content"))
    for script in tree.css("script"):
        declared = (script.attributes.get("type") or "").strip().casefold()
        if declared == "application/ld+json":
            for node in _json_ld_nodes(script.text(deep=True) or ""):
                if _is_organization(node):
                    for key in ("name", "legalName", "alternateName"):
                        add_name(node.get(key))

    links: list[Link] = []
    seen: set[str] = set()
    for selector, attribute in (("a", "href"), ("iframe", "src"), ("script", "src")):
        for node in tree.css(selector):
            if len(links) >= MAX_LINKS:
                break
            target = node.attributes.get(attribute)
            if not target:
                continue
            try:
                url = public_url(urljoin(page_url, target.strip()))
            except ValueError:
                continue  # mailto:, javascript:, relative junk, private hosts ...
            if url in seen:
                continue
            seen.add(url)
            text = " ".join((node.text(deep=True) or "").split()) if selector == "a" else ""
            links.append(Link(url, text[:200]))
    return PageSignals(tuple(names), tuple(links))


def same_site(url: str, other: str) -> bool:
    """Whether two URLs share a registered domain (``careers.example.com`` ~ ``example.com``)."""
    domain = registered_domain(url)
    return domain is not None and domain == registered_domain(other)


def _json_ld_nodes(text: str) -> list[dict[str, object]]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    found: list[dict[str, object]] = []
    stack: list[tuple[object, int]] = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 4:
            continue
        if isinstance(node, list):
            stack.extend((entry, depth + 1) for entry in node)
        elif isinstance(node, dict):
            found.append(node)
            graph = node.get("@graph")
            if isinstance(graph, list):
                stack.extend((entry, depth + 1) for entry in graph)
    return found


def _is_organization(node: dict[str, object]) -> bool:
    declared = node.get("@type")
    for entry in declared if isinstance(declared, list) else [declared]:
        if isinstance(entry, str):
            name = entry.strip().rpartition("/")[2].rpartition(":")[2].casefold()
            if name in _ORGANIZATION_TYPES:
                return True
    return False
