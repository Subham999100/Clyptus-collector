"""Discover one company's crawlable job source, with the evidence for it.

Discovery never invents anything: the official website comes from the company list, a careers
URL comes from the list or from a link on an official page, and a board identifier is read from
a board URL (``collector.detect.platforms``). There is no search provider: a company with no
supplied website cannot be identified and is reported ``NOT_FOUND``.

Pages are read only through the ``fetch`` callable supplied by the caller. Without one
(offline mode) nothing is fetched, so ownership cannot be confirmed and every candidate source
is left ``REVIEW_REQUIRED``. With one, discovery fetches at most two pages per company, both on
the official website's registered domain (the homepage, then one careers page), and checks:

1. the homepage names the company (title, ``og:site_name`` or JSON-LD ``Organization``);
2. a supported job board is linked (or redirected to) from those official pages, or the official
   careers page carries JSON-LD ``JobPosting`` markup the collector's adapter can read.

Only then is the source ``VERIFIED``. Job boards themselves are never fetched here: discovery
is not collection. Discovery never grants permission to collect; that is a separate, explicit
decision recorded by ``collector.companies.registry``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from collector.adapters.common import public_url
from collector.adapters.json_ld import extract_json_ld_jobs
from collector.companies.input import Company, name_tokens
from collector.detect.platforms import (
    Board,
    PageSignals,
    detect_board,
    is_careers_link,
    read_page,
    same_site,
)


class Status(StrEnum):
    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class Page:
    url: str  # the final URL after any redirects
    html: str


class FetchError(Exception):
    """A page could not be fetched. ``str(error)`` is a short reason without the URL."""


Fetch = Callable[[str], Page]


@dataclass(frozen=True)
class Discovery:
    status: Status
    official_website: str | None
    careers_url: str | None
    platform: str | None
    identifier: str | None
    evidence: tuple[str, ...]
    reason: str | None
    failed: bool = False  # a page could not be fetched


def names_company(company_name: str, page_name: str) -> bool:
    """Whether a page's name contains the company's name as whole consecutive words."""
    wanted = name_tokens(company_name)
    have = name_tokens(page_name)
    size = len(wanted)
    return bool(wanted) and any(have[i : i + size] == wanted for i in range(len(have) - size + 1))


def discover(
    company: Company, fetch: Fetch | None = None, *, now: datetime | None = None
) -> Discovery:
    """Discover the job source of one company (see the module docstring)."""
    result = _discover(company, fetch, now or datetime.now(UTC))
    concern = company.conflict or company.similar
    if concern and result.status == Status.VERIFIED:
        return _with(result, Status.REVIEW_REQUIRED, concern)
    if concern and result.status == Status.REVIEW_REQUIRED and result.reason:
        return _with(result, Status.REVIEW_REQUIRED, f"{concern}; {result.reason}")
    return result


def _with(result: Discovery, status: Status, reason: str) -> Discovery:
    return Discovery(
        status,
        result.official_website,
        result.careers_url,
        result.platform,
        result.identifier,
        result.evidence,
        reason,
        result.failed,
    )


def _discover(company: Company, fetch: Fetch | None, now: datetime) -> Discovery:
    website = company.official_website
    careers = company.careers_url
    evidence: list[str] = []
    if website:
        evidence.append(f"official website supplied in the company list (line {company.line})")
    if careers:
        evidence.append(f"careers URL supplied in the company list (line {company.line})")
    supplied_board = detect_board(careers) if careers else None

    def result(
        status: Status,
        reason: str | None,
        board: Board | None = None,
        careers_url: str | None = careers,
        *,
        platform: str | None = None,
        identifier: str | None = None,
        failed: bool = False,
    ) -> Discovery:
        if board is not None:
            platform, identifier = board.platform, board.identifier
        return Discovery(
            status, website, careers_url, platform, identifier, tuple(evidence), reason, failed
        )

    if supplied_board is not None:
        problem = _board_problem(supplied_board)
        if problem is not None:
            status = Status.UNSUPPORTED if not supplied_board.supported else Status.REVIEW_REQUIRED
            return result(status, problem, supplied_board)
    if website is None:
        if careers is None:
            return result(
                Status.NOT_FOUND,
                "no official website supplied; automatic website search is not available",
            )
        return result(
            Status.REVIEW_REQUIRED,
            "official website not supplied, so the careers URL's ownership cannot be confirmed",
            supplied_board,
            platform="jsonld" if supplied_board is None else None,
            identifier=careers if supplied_board is None else None,
        )
    if careers and supplied_board is None and not same_site(careers, website):
        return result(Status.REVIEW_REQUIRED, "careers URL is not on the official website's domain")

    if fetch is None:  # offline: record the candidate, confirm nothing
        if supplied_board is not None:
            return result(
                Status.REVIEW_REQUIRED,
                "offline: board not confirmed as linked from the official website",
                supplied_board,
            )
        if careers:
            return result(
                Status.REVIEW_REQUIRED,
                "offline: JSON-LD JobPosting markup on the careers page not checked",
                platform="jsonld",
                identifier=careers,
            )
        return result(Status.NOT_FOUND, "offline: careers page not searched (no pages fetched)")

    # --- online: the official homepage first -------------------------------------------------
    try:
        home = fetch(website)
    except FetchError as exc:
        return result(
            Status.REVIEW_REQUIRED,
            f"official website could not be fetched ({exc})",
            supplied_board,
            failed=True,
        )
    if not same_site(home.url, website):
        return result(
            Status.REVIEW_REQUIRED,
            "official website redirected to a different domain",
            supplied_board,
        )
    home_signals = read_page(home.html, home.url)
    identity = next(
        (name for name in home_signals.names if names_company(company.name, name)), None
    )
    if identity is not None:
        evidence.append("official website homepage names the company")
    identity_problem = (
        None
        if identity is not None
        else (
            "official website does not name the company"
            if home_signals.names
            else "official website gives no name to compare with the company"
        )
    )

    # --- the careers page: supplied, linked from the homepage, or the homepage itself --------
    careers_page: Page | None = None
    careers_signals: PageSignals | None = None
    target = careers if careers and supplied_board is None else None
    if target is None:
        target = next(
            (
                link.url
                for link in home_signals.links
                if is_careers_link(link)
                and same_site(link.url, website)
                and detect_board(link.url) is None
            ),
            None,
        )
        if target is not None:
            evidence.append("careers page linked from the official homepage")
    if target is not None:
        try:
            careers_page = fetch(target)
        except FetchError as exc:
            return result(
                Status.REVIEW_REQUIRED,
                f"careers page could not be fetched ({exc})",
                careers_url=target,
                failed=True,
            )
        if not same_site(careers_page.url, website) and detect_board(careers_page.url) is None:
            return result(
                Status.REVIEW_REQUIRED,
                "careers page redirected to a different domain",
                careers_url=target,
            )
        careers_signals = read_page(careers_page.html, careers_page.url)

    pages = [(home, home_signals)]
    if careers_page is not None and careers_signals is not None:
        pages.append((careers_page, careers_signals))
    supported: dict[tuple[str, str], str] = {}
    unsupported: list[str] = []
    for page, signals in pages:
        candidates = [(page.url, "redirect")] if page is careers_page else []
        # On the homepage only careers links count (not, say, a partner's job in a news item);
        # on the careers page every board link or embed does.
        candidates += [
            (link.url, "link")
            for link in signals.links
            if page is careers_page or is_careers_link(link)
        ]
        for url, how in candidates:
            board = detect_board(url)
            if board is None:
                continue
            if board.supported and board.identifier:
                supported.setdefault((board.platform, board.identifier), how)
            elif not board.supported and board.platform not in unsupported:
                unsupported.append(board.platform)

    def finish(found: Discovery) -> Discovery:
        if identity_problem and found.status == Status.VERIFIED:
            return _with(found, Status.REVIEW_REQUIRED, identity_problem)
        return found

    if supplied_board is not None:
        key = (supplied_board.platform, supplied_board.identifier or "")
        if key not in supported:
            return finish(
                result(
                    Status.REVIEW_REQUIRED,
                    "supplied board is not linked from the official website",
                    supplied_board,
                )
            )
        evidence.append(f"official website links the {supplied_board.platform} board")
        return finish(result(Status.VERIFIED, None, supplied_board))
    careers_url = careers_page.url if careers_page is not None else None
    if len(supported) > 1:
        platforms = ", ".join(sorted({platform for platform, _ in supported}))
        return finish(
            result(
                Status.REVIEW_REQUIRED,
                f"{len(supported)} different job boards are linked from the official website "
                f"({platforms})",
                careers_url=careers_url,
            )
        )
    if supported:
        (platform, identifier), how = next(iter(supported.items()))
        evidence.append(
            f"official careers page redirects to the {platform} board"
            if how == "redirect"
            else f"official website links the {platform} board"
        )
        return finish(
            result(
                Status.VERIFIED,
                None,
                careers_url=careers_url,
                platform=platform,
                identifier=identifier,
            )
        )
    if careers_page is not None and same_site(careers_page.url, website):
        page_url = _https(careers_page.url)
        postings = 0
        if page_url is not None:
            extraction = extract_json_ld_jobs(
                careers_page.html, page_url=page_url, collected_at=now
            )
            postings = len(extraction.records)
        if postings:
            evidence.append(f"official careers page has {postings} readable JSON-LD JobPosting(s)")
            return finish(
                result(
                    Status.VERIFIED,
                    None,
                    careers_url=page_url,
                    platform="jsonld",
                    identifier=page_url,
                )
            )
    if unsupported:
        return finish(
            result(
                Status.UNSUPPORTED,
                f"{unsupported[0]} is not supported by the collector",
                careers_url=careers_url,
                platform=unsupported[0],
            )
        )
    if careers_page is not None:
        return finish(
            result(
                Status.UNSUPPORTED,
                "careers page has no supported job board and no readable JSON-LD JobPosting",
                careers_url=careers_url,
                platform="custom",
            )
        )
    return finish(result(Status.NOT_FOUND, "no careers page linked from the official homepage"))


def _board_problem(board: Board) -> str | None:
    if not board.supported:
        return f"{board.platform} is not supported by the collector"
    if board.identifier is None:
        return f"careers URL has no valid {board.platform} board identifier"
    return None


def _https(url: str) -> str | None:
    try:
        clean = public_url(url)
    except ValueError:
        return None
    return clean if clean.startswith("https://") else None
