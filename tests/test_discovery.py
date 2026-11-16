"""Phase 6: company input, platform detection and single-company discovery.

Offline. Every company, website and board below is synthetic (reserved ``example.*`` domains and
made-up board tokens in the documented URL shapes). Pages come from an in-memory fake fetcher.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from collector.companies.discovery import (
    Discovery,
    FetchError,
    Page,
    Status,
    discover,
    names_company,
)
from collector.companies.input import (
    Company,
    company_key,
    load_companies,
)
from collector.detect.platforms import Board, detect_board, read_page

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
HEADER = "company_name,official_website,careers_url\n"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "companies.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return path


def company(
    name: str = "Acme Example",
    website: str | None = "https://www.example.com/",
    careers: str | None = None,
    **extra: str,
) -> Company:
    return Company(name, company_key(name), 2, website, careers, **extra)


class FakeWeb:
    """Serves scripted pages; records every URL asked for."""

    def __init__(self, pages: dict[str, Page | Exception]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url: str) -> Page:
        self.calls.append(url)
        page = self.pages.get(url, FetchError("HTTP 404"))
        if isinstance(page, Exception):
            raise page
        return page


def html(title: str = "Acme Example | Home", body: str = "", head: str = "") -> str:
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>"


HOME = "https://www.example.com/"
CAREERS = "https://www.example.com/careers"


# --- company input ----------------------------------------------------------------------------


def test_valid_company_list_keeps_file_order_and_optional_urls(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        HEADER
        + "Zeta Example,https://zeta.example.org,\n"
        + "Acme Example,,\n"
        + "Beta Example,https://www.example.net/,https://jobs.lever.co/beta-example\n",
    )
    listed = load_companies(path)
    assert [c.name for c in listed.companies] == ["Zeta Example", "Acme Example", "Beta Example"]
    assert listed.companies[0].official_website == "https://zeta.example.org/"
    assert listed.companies[1].official_website is None  # missing website is allowed
    assert listed.companies[2].careers_url == "https://jobs.lever.co/beta-example"
    assert [c.line for c in listed.companies] == [2, 3, 4]
    assert listed.rows == 3 and not listed.rejected and not listed.duplicates


def test_name_only_column_is_enough(tmp_path: Path) -> None:
    listed = load_companies(write(tmp_path, "company_name\nAcme Example\n\n Beta Example \n"))
    assert [(c.name, c.official_website, c.careers_url) for c in listed.companies] == [
        ("Acme Example", None, None),
        ("Beta Example", None, None),
    ]


def test_duplicates_are_skipped_conflicts_and_similar_names_flagged_never_merged(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path,
        HEADER
        + "Acme Example,https://www.example.com,\n"
        + "ACME  example,https://www.example.com,\n"  # same company: skipped
        + "Beta Example,https://www.example.net,\n"
        + "beta example,https://www.example.org,\n"  # same name, different site: conflict
        + "Gamma Example Inc,,\n"
        + "Gamma Example Ltd.,,\n",  # similar, not identical: both kept, both flagged
    )
    listed = load_companies(path)
    names = [c.name for c in listed.companies]
    assert names == ["Acme Example", "Beta Example", "Gamma Example Inc", "Gamma Example Ltd."]
    assert [(d.line, d.reason) for d in listed.duplicates] == [
        (3, "repeats the company on line 2"),
        (5, "repeats the company on line 4"),
    ]
    acme, beta, gamma_inc, gamma_ltd = listed.companies
    assert acme.conflict is None and acme.similar is None
    assert beta.conflict == "rows with different URLs for this company (lines 4, 5)"
    assert beta.official_website == "https://www.example.net/"  # the first row is kept
    assert gamma_inc.similar and "line 7" in gamma_inc.similar
    assert gamma_ltd.similar and "line 6" in gamma_ltd.similar


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        (",https://www.example.com,", "company_name is blank"),
        ("   ,https://www.example.com,", "company_name is blank"),
        ("Acme\x07Example,,", "control characters"),
        ("A" * 201 + ",,", "longer than 200"),
        ("=HYPERLINK(1),,", "must not start with"),
        ("@Acme,,", "must not start with"),
        ("---,,", "must not start with"),
        ("...,,", "no letters or digits"),
    ],
)
def test_invalid_names_are_rejected_with_line_numbers(
    tmp_path: Path, row: str, fragment: str
) -> None:
    listed = load_companies(write(tmp_path, HEADER + "Fine Example,,\n" + row + "\n"))
    assert [c.name for c in listed.companies] == ["Fine Example"]
    assert len(listed.rejected) == 1
    assert listed.rejected[0].line == 3
    assert fragment in listed.rejected[0].reason


@pytest.mark.parametrize(
    ("website", "fragment"),
    [
        ("http://www.example.com", "must use https"),
        ("https://localhost/", "not public"),
        ("https://127.0.0.1/", "not public"),
        ("https://169.254.169.254/latest", "not public"),
        ("https://10.1.2.3/", "not public"),
        ("https://intranet/", "not public"),
        ("javascript:alert(1)", "not an absolute"),
        ("ftp://files.example.com/", "not an absolute"),
        ("www.example.com", "not an absolute"),
        ("https://user:s3cr3t-pass@www.example.com/", "credentials"),
        ("https://www.example.com/" + "a" * 2000, "longer than 2000"),
    ],
)
def test_unsafe_or_invalid_urls_are_rejected_without_echoing_them(
    tmp_path: Path, website: str, fragment: str
) -> None:
    listed = load_companies(write(tmp_path, HEADER + f"Acme Example,{website},\n"))
    assert listed.companies == ()
    (issue,) = listed.rejected
    assert issue.reason.startswith("official_website") and fragment in issue.reason
    assert website not in issue.reason and "s3cr3t" not in issue.reason


def test_unsafe_careers_url_is_rejected(tmp_path: Path) -> None:
    listed = load_companies(write(tmp_path, HEADER + "Acme Example,,https://192.168.0.1/jobs\n"))
    assert listed.companies == ()
    assert listed.rejected[0].reason == "careers_url: host is not public"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "lacks column"),  # empty file
        ("\n\n", "lacks column"),
        ("official_website\nhttps://www.example.com\n", "lacks column"),
        ("company_name,website\nAcme,\n", "unexpected column(s) website"),
        ("company_name,company_name\nAcme,Acme\n", "more than once"),
        (HEADER + '"Acme"Example,,\n', "malformed CSV"),  # stray quote after a closing quote
        (HEADER + '"Unclosed,,\n', "malformed CSV"),
    ],
)
def test_structurally_unusable_files_raise(tmp_path: Path, text: str, fragment: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        load_companies(write(tmp_path, text))
    assert fragment in str(excinfo.value)


def test_header_only_file_has_no_companies_and_row_with_extra_fields_is_rejected(
    tmp_path: Path,
) -> None:
    assert load_companies(write(tmp_path, HEADER)).companies == ()
    listed = load_companies(write(tmp_path, HEADER + "Acme Example,,,surplus\n"))
    assert listed.companies == () and listed.rejected[0].reason.startswith("row has more fields")


def test_shipped_template_is_a_valid_empty_company_list() -> None:
    template = Path(__file__).parents[1] / "collector" / "companies" / "seed_companies.csv"
    listed = load_companies(template)
    assert listed.companies == () and listed.rows == 0 and not listed.rejected


# --- platform detection -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://boards.greenhouse.io/acmeexample", Board("greenhouse", "acmeexample", True)),
        (
            "https://job-boards.greenhouse.io/acmeexample/jobs/4000001",
            Board("greenhouse", "acmeexample", True),
        ),
        (
            "https://boards.greenhouse.io/embed/job_board/js?for=acmeexample",
            Board("greenhouse", "acmeexample", True),
        ),
        (
            "https://boards-api.greenhouse.io/v1/boards/acmeexample/jobs",
            Board("greenhouse", "acmeexample", True),
        ),
        ("https://jobs.lever.co/acme-example", Board("lever", "acme-example", True)),
        (
            "https://jobs.lever.co/acme-example/0f1e2d3c-0000-4000-8000-000000000001/apply",
            Board("lever", "acme-example", True),
        ),
        (
            "https://api.lever.co/v0/postings/acme-example?mode=json",
            Board("lever", "acme-example", True),
        ),
        ("https://jobs.ashbyhq.com/AcmeExample", Board("ashby", "AcmeExample", True)),
        (
            "https://api.ashbyhq.com/posting-api/job-board/AcmeExample",
            Board("ashby", "AcmeExample", True),
        ),
    ],
)
def test_supported_boards_are_detected_from_documented_url_shapes(url: str, board: Board) -> None:
    assert detect_board(url) == board


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://boards.greenhouse.io/", "greenhouse"),  # no token
        ("https://boards.greenhouse.io/embed/job_board", "greenhouse"),  # embed without ?for=
        ("https://boards.greenhouse.io/embed/job_board?for=a%2Fb", "greenhouse"),  # separator
        ("https://jobs.lever.co/.hidden", "lever"),
        ("https://jobs.lever.co/a%20b", "lever"),
        ("https://jobs.ashbyhq.com/", "ashby"),
        ("https://api.lever.co/v1/other/acme", "lever"),
    ],
)
def test_invalid_or_missing_identifiers_are_never_guessed(url: str, platform: str) -> None:
    assert detect_board(url) == Board(platform, None, True)


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://acmeexample.wd1.myworkdayjobs.com/External", "workday"),
        ("https://acmeexample.keka.com/careers/", "keka"),
        ("https://acmeexample.darwinbox.in/ms/candidate/careers", "darwinbox"),
        ("https://careers.zohorecruit.com/jobs/Careers", "zoho-recruit"),
        ("https://boards.eu.greenhouse.io/acmeexample", "greenhouse-eu"),
        ("https://jobs.eu.lever.co/acme-example", "lever-eu"),
    ],
)
def test_unsupported_platforms_are_recognized(url: str, platform: str) -> None:
    assert detect_board(url) == Board(platform, None, False)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.keka.com/",  # the vendor's own marketing site, not a tenant
        "https://keka.com/pricing",
        "https://www.example.com/careers",
        "https://greenhouse.io.example.com/acme",  # look-alike host
        "http://127.0.0.1/acme",
        "not a url",
    ],
)
def test_other_urls_are_not_job_boards(url: str) -> None:
    assert detect_board(url) is None


def test_read_page_collects_names_and_safe_absolute_links() -> None:
    page = html(
        head=(
            '<meta property="og:site_name" content="Acme Example">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@graph":[{"@type":"Organization",'
            '"name":"Acme Example Ltd","legalName":"Acme Example Limited"}]}</script>'
            '<script type="application/ld+json">{not json</script>'
        ),
        body=(
            '<a href="/careers">Careers</a><a href="/careers">Careers again</a>'
            '<a href="javascript:alert(1)">x</a><a href="https://localhost/admin">x</a>'
            '<a href="mailto:jobs@example.com">mail</a>'
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acmeexample"></iframe>'
        ),
    )
    signals = read_page(page, HOME)
    assert signals.names == (
        "Acme Example | Home",
        "Acme Example",
        "Acme Example Ltd",
        "Acme Example Limited",
    )
    assert [(link.url, link.text) for link in signals.links] == [
        (CAREERS, "Careers"),
        ("https://boards.greenhouse.io/embed/job_board?for=acmeexample", ""),
    ]


def test_names_company_matches_whole_words_only() -> None:
    assert names_company("Acme Example", "Welcome to Acme Example | Home")
    assert names_company("Acme Example Inc.", "ACME EXAMPLE")
    assert names_company("Acme Example Pvt Ltd", "Acme-Example")
    assert not names_company("Acme", "Acmeville Supplies")
    assert not names_company("Acme Example", "Example Acme")
    assert not names_company("TCS Example", "Tata Consultancy Example")  # acronyms are not guessed


# --- discovery: offline -----------------------------------------------------------------------


def test_offline_name_only_is_not_found_and_nothing_is_invented() -> None:
    found = discover(company(website=None), now=NOW)
    assert found == Discovery(
        Status.NOT_FOUND,
        None,
        None,
        None,
        None,
        (),
        "no official website supplied; automatic website search is not available",
    )


def test_offline_website_only_is_not_found() -> None:
    found = discover(company(), now=NOW)
    assert found.status == Status.NOT_FOUND
    assert found.official_website == HOME and found.careers_url is None
    assert found.platform is None and found.identifier is None
    assert "offline" in (found.reason or "")


@pytest.mark.parametrize(
    ("careers", "platform", "identifier"),
    [
        ("https://boards.greenhouse.io/acmeexample", "greenhouse", "acmeexample"),
        ("https://jobs.lever.co/acme-example", "lever", "acme-example"),
        ("https://jobs.ashbyhq.com/AcmeExample", "ashby", "AcmeExample"),
    ],
)
def test_offline_supplied_board_is_a_review_required_candidate(
    careers: str, platform: str, identifier: str
) -> None:
    found = discover(company(careers=careers), now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert (found.platform, found.identifier) == (platform, identifier)
    assert found.reason == "offline: board not confirmed as linked from the official website"
    assert found.evidence == (
        "official website supplied in the company list (line 2)",
        "careers URL supplied in the company list (line 2)",
    )


def test_offline_same_site_careers_page_is_only_a_json_ld_candidate() -> None:
    found = discover(company(careers=CAREERS), now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert (found.platform, found.identifier) == ("jsonld", CAREERS)
    assert "not checked" in (found.reason or "")


def test_careers_url_on_another_domain_is_not_attributed() -> None:
    found = discover(company(careers="https://jobs.example.org/acme"), now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert found.platform is None and found.identifier is None
    assert found.reason == "careers URL is not on the official website's domain"


def test_supplied_unsupported_platform_is_unsupported() -> None:
    found = discover(company(careers="https://acmeexample.keka.com/careers"), now=NOW)
    assert (found.status, found.platform, found.identifier) == (Status.UNSUPPORTED, "keka", None)
    assert found.reason == "keka is not supported by the collector"


def test_supplied_board_without_valid_identifier_is_review_required() -> None:
    found = discover(company(careers="https://jobs.lever.co/"), now=NOW)
    assert (found.status, found.platform, found.identifier) == (
        Status.REVIEW_REQUIRED,
        "lever",
        None,
    )
    assert found.reason == "careers URL has no valid lever board identifier"


def test_board_without_official_website_cannot_be_attributed() -> None:
    found = discover(company(website=None, careers="https://jobs.lever.co/acme-example"), now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert "official website not supplied" in (found.reason or "")


# --- discovery: with pages (fake fetcher) -----------------------------------------------------


def test_verified_greenhouse_board_embedded_on_the_official_careers_page() -> None:
    web = FakeWeb(
        {
            HOME: Page(HOME, html(body='<a href="/careers">Careers</a>')),
            CAREERS: Page(
                CAREERS,
                html(
                    "Careers at Acme Example",
                    body='<script src="https://boards.greenhouse.io/embed/job_board/js'
                    '?for=acmeexample"></script>',
                ),
            ),
        }
    )
    found = discover(company(), web, now=NOW)
    assert found.status == Status.VERIFIED
    assert (found.platform, found.identifier) == ("greenhouse", "acmeexample")
    assert found.careers_url == CAREERS and found.official_website == HOME
    assert found.evidence == (
        "official website supplied in the company list (line 2)",
        "official website homepage names the company",
        "careers page linked from the official homepage",
        "official website links the greenhouse board",
    )
    assert found.reason is None
    assert web.calls == [HOME, CAREERS]  # the board itself is never fetched


def test_verified_lever_board_linked_from_the_homepage_navigation() -> None:
    web = FakeWeb(
        {HOME: Page(HOME, html(body='<a href="https://jobs.lever.co/acme-example">Jobs</a>'))}
    )
    found = discover(company(), web, now=NOW)
    assert (found.status, found.platform, found.identifier) == (
        Status.VERIFIED,
        "lever",
        "acme-example",
    )
    assert web.calls == [HOME]


def test_verified_ashby_board_reached_by_a_careers_redirect() -> None:
    board = "https://jobs.ashbyhq.com/AcmeExample"
    web = FakeWeb(
        {
            HOME: Page(HOME, html(body='<a href="/careers">Join our team</a>')),
            CAREERS: Page(board, "<html><body>Open roles</body></html>"),
        }
    )
    found = discover(company(), web, now=NOW)
    assert (found.status, found.platform, found.identifier) == (
        Status.VERIFIED,
        "ashby",
        "AcmeExample",
    )
    assert "official careers page redirects to the ashby board" in found.evidence


def test_verified_json_ld_careers_page() -> None:
    jobs_page = (FIXTURES / "jsonld" / "single_job_posting.html").read_text(encoding="utf-8")
    web = FakeWeb(
        {
            HOME: Page(HOME, html(body='<a href="/careers">Careers</a>')),
            CAREERS: Page(CAREERS, jobs_page),
        }
    )
    found = discover(company(), web, now=NOW)
    assert (found.status, found.platform, found.identifier) == (Status.VERIFIED, "jsonld", CAREERS)
    assert "official careers page has 1 readable JSON-LD JobPosting(s)" in found.evidence


def test_careers_page_without_supported_markup_is_unsupported_custom() -> None:
    web = FakeWeb(
        {
            HOME: Page(HOME, html(body='<a href="/careers">Careers</a>')),
            CAREERS: Page(CAREERS, html("Careers", body="<p>Email us your CV.</p>")),
        }
    )
    found = discover(company(), web, now=NOW)
    assert (found.status, found.platform) == (Status.UNSUPPORTED, "custom")


def test_careers_page_on_an_unsupported_ats_is_unsupported() -> None:
    web = FakeWeb(
        {
            HOME: Page(HOME, html(body='<a href="/careers">Careers</a>')),
            CAREERS: Page(
                CAREERS,
                html(body='<a href="https://acmeexample.wd1.myworkdayjobs.com/External">Jobs</a>'),
            ),
        }
    )
    found = discover(company(), web, now=NOW)
    assert (found.status, found.platform, found.identifier) == (
        Status.UNSUPPORTED,
        "workday",
        None,
    )


def test_identity_mismatch_keeps_the_candidate_but_requires_review() -> None:
    web = FakeWeb(
        {
            HOME: Page(
                HOME,
                html(
                    "Other Example Holdings", body='<a href="https://jobs.lever.co/x1">Careers</a>'
                ),
            )
        }
    )
    found = discover(company(), web, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert (found.platform, found.identifier) == ("lever", "x1")
    assert found.reason == "official website does not name the company"


def test_page_without_any_name_cannot_confirm_identity() -> None:
    web = FakeWeb(
        {HOME: Page(HOME, "<html><body><a href='https://jobs.lever.co/x1'>Jobs</a></body></html>")}
    )
    found = discover(company(), web, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert found.reason == "official website gives no name to compare with the company"


def test_several_boards_are_ambiguous() -> None:
    web = FakeWeb(
        {
            HOME: Page(HOME, html(body='<a href="/careers">Careers</a>')),
            CAREERS: Page(
                CAREERS,
                html(
                    body='<a href="https://jobs.lever.co/acme-a">A</a>'
                    '<a href="https://boards.greenhouse.io/acmeb">B</a>'
                ),
            ),
        }
    )
    found = discover(company(), web, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert found.platform is None and found.identifier is None
    assert found.reason == (
        "2 different job boards are linked from the official website (greenhouse, lever)"
    )


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"similar": "name is similar to another listed company (line 9)"}, "similar"),
        ({"conflict": "rows with different URLs for this company (lines 2, 9)"}, "different URLs"),
    ],
)
def test_ambiguous_company_identity_is_never_verified(extra: dict[str, str], reason: str) -> None:
    web = FakeWeb({HOME: Page(HOME, html(body='<a href="https://jobs.lever.co/acme">Jobs</a>'))})
    found = discover(company(**extra), web, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert reason in (found.reason or "")
    assert (found.platform, found.identifier) == ("lever", "acme")  # kept for the reviewer


def test_supplied_board_is_verified_only_when_the_official_site_links_it() -> None:
    supplied = "https://boards.greenhouse.io/acmeexample"
    linked = FakeWeb({HOME: Page(HOME, html(body=f'<a href="{supplied}/jobs/1">Careers</a>'))})
    found = discover(company(careers=supplied), linked, now=NOW)
    assert (found.status, found.identifier) == (Status.VERIFIED, "acmeexample")

    unlinked = FakeWeb({HOME: Page(HOME, html(body='<a href="https://jobs.lever.co/zz">Jobs</a>'))})
    found = discover(company(careers=supplied), unlinked, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert found.reason == "supplied board is not linked from the official website"
    assert "https://boards.greenhouse.io/acmeexample" not in unlinked.calls


def test_board_links_outside_careers_navigation_are_not_attributed() -> None:
    web = FakeWeb(
        {
            HOME: Page(
                HOME,
                html(body='<a href="https://jobs.lever.co/partner">Read about our partner</a>'),
            )
        }
    )
    found = discover(company(), web, now=NOW)
    assert found.status == Status.NOT_FOUND
    assert found.reason == "no careers page linked from the official homepage"


def test_fetch_failure_is_recorded_as_a_failure() -> None:
    found = discover(company(), FakeWeb({HOME: FetchError("HTTP 503")}), now=NOW)
    assert (found.status, found.failed) == (Status.REVIEW_REQUIRED, True)
    assert found.reason == "official website could not be fetched (HTTP 503)"


def test_redirects_off_the_official_domain_are_not_trusted() -> None:
    elsewhere = "https://www.example.org/"
    found = discover(company(), FakeWeb({HOME: Page(elsewhere, html())}), now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert found.reason == "official website redirected to a different domain"

    web = FakeWeb(
        {
            HOME: Page(HOME, html(body='<a href="/careers">Careers</a>')),
            CAREERS: Page("https://jobs.example.org/acme", html()),
        }
    )
    found = discover(company(), web, now=NOW)
    assert found.status == Status.REVIEW_REQUIRED
    assert found.reason == "careers page redirected to a different domain"


def test_only_official_domain_pages_are_fetched() -> None:
    web = FakeWeb(
        {
            HOME: Page(
                HOME,
                html(
                    body='<a href="https://careers.example.org/acme">Careers</a>'
                    '<a href="/jobs">Jobs</a>'
                ),
            ),
            "https://www.example.com/jobs": Page("https://www.example.com/jobs", html()),
        }
    )
    discover(company(), web, now=NOW)
    assert web.calls == [HOME, "https://www.example.com/jobs"]
