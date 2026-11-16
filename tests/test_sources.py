"""Source CSV loading and validation. Offline: reads only files written to a temp directory."""

from pathlib import Path

import pytest

from collector.spiders.sources import PLATFORMS, Source, check_source, load_sources

HEADER = "platform,company_name,identifier,company_website\n"


def write(tmp_path: Path, text: str, *, name: str = "sources.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


def test_loads_every_platform_in_file_order(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        HEADER
        + "greenhouse,Example Corp,example-board,https://www.example.com\n"
        + "lever,Example Two,example-site,\n"
        + "ashby,Example Three,example-ashby,\n"
        + "jsonld,Example Four,https://jobs.example.org/careers/engineer,\n",
    )

    sources = load_sources(path)

    assert sources == [
        Source("greenhouse", "Example Corp", "example-board", "https://www.example.com/"),
        Source("lever", "Example Two", "example-site"),
        Source("ashby", "Example Three", "example-ashby"),
        Source("jsonld", "Example Four", "https://jobs.example.org/careers/engineer"),
    ]
    assert {source.platform for source in sources} == set(PLATFORMS)


def test_optional_website_column_may_be_absent_and_whitespace_is_trimmed(tmp_path: Path) -> None:
    path = write(
        tmp_path, "platform,company_name,identifier\n GreenHouse , Example Corp , board \n"
    )
    assert load_sources(path) == [Source("greenhouse", "Example Corp", "board")]


def test_utf8_bom_blank_lines_and_quoted_fields(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "﻿" + HEADER + "\n" + 'lever,"Example, Corp",site,\n' + ",,,\n",
    )
    assert load_sources(path) == [Source("lever", "Example, Corp", "site")]


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ("workday,Example Corp,board,", "platform must be one of"),  # unknown platform
        (",Example Corp,board,", "platform must be one of"),  # blank platform
        ("greenhouse,,board,", "company_name"),  # blank company
        ("greenhouse,Example\x00Corp,board,", "control characters"),
        ("greenhouse,   ,board,", "company_name"),
        ("greenhouse,Example Corp,,", "identifier"),  # blank identifier
        ("greenhouse,Example Corp,   ,", "identifier"),
        ("greenhouse,Example Corp,a/b,", "identifier must match"),  # path separator
        ("greenhouse,Example Corp,../x,", "identifier must match"),  # traversal
        ("greenhouse,Example Corp,a b,", "identifier must match"),  # space
        ("greenhouse,Example Corp,a?x=1,", "identifier must match"),  # query injection
        ("greenhouse,Example Corp,https://evil.example/x,", "identifier must match"),  # a URL
        ("lever,Example Corp,.hidden,", "identifier must match"),
        ("jsonld,Example Corp,http://jobs.example.org/a,", "must use https"),
        ("jsonld,Example Corp,ftp://jobs.example.org/a,", "jsonld page URL"),
        ("jsonld,Example Corp,file:///etc/passwd,", "jsonld page URL"),
        ("jsonld,Example Corp,javascript:alert(1),", "jsonld page URL"),
        ("jsonld,Example Corp,https://localhost/a,", "not public"),
        ("jsonld,Example Corp,https://127.0.0.1/a,", "not public"),
        ("jsonld,Example Corp,https://169.254.169.254/latest,", "not public"),
        ("jsonld,Example Corp,https://10.0.0.5/a,", "not public"),
        ("jsonld,Example Corp,https://intranet/a,", "not public"),  # single-label host
        ("jsonld,Example Corp,https://user:pw@jobs.example.org/a,", "credentials"),
        ("jsonld,Example Corp,/relative/path,", "jsonld page URL"),
        ("jsonld,Example Corp,,", "identifier"),
        ("greenhouse,Example Corp,board,not a url", "company_website"),
        ("greenhouse,Example Corp,board,http://127.0.0.1", "company_website"),
    ],
)
def test_invalid_sources_are_rejected_with_the_line_number(
    tmp_path: Path, row: str, fragment: str
) -> None:
    path = write(tmp_path, HEADER + "greenhouse,Fine Corp,fine-board,\n" + row + "\n")

    with pytest.raises(ValueError) as excinfo:
        load_sources(path)

    message = str(excinfo.value)
    assert "line 3" in message
    assert fragment in message


def test_error_messages_never_echo_the_configured_value(tmp_path: Path) -> None:
    secret = "tok3n-s3cret"
    for row in (
        f"jsonld,Example Corp,https://user:{secret}@jobs.example.org/a,",
        f"greenhouse,Example Corp,bad/{secret},",
        f"workday,Example Corp,{secret},",
    ):
        with pytest.raises(ValueError) as excinfo:
            load_sources(write(tmp_path, HEADER + row + "\n"))
        assert secret not in str(excinfo.value)


def test_duplicate_sources_are_rejected_not_merged(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        HEADER + "greenhouse,Example Corp,Board,\n" + "greenhouse,Other Name,board,\n",
    )
    with pytest.raises(ValueError, match=r"line 3: duplicate source .*line 2"):
        load_sources(path)


def test_same_identifier_on_different_platforms_is_not_a_duplicate(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + "greenhouse,A,same,\n" + "lever,A,same,\n")
    assert len(load_sources(path)) == 2


def test_duplicate_jsonld_pages_are_rejected_after_normalization(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        HEADER
        + "jsonld,A,https://Jobs.Example.org/a?utm_source=x,\n"
        + "jsonld,B,https://jobs.example.org:443/a#top,\n",
    )
    with pytest.raises(ValueError, match="duplicate source"):
        load_sources(path)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "lacks column"),  # empty file
        ("\n\n", "lacks column"),
        (HEADER, "no sources configured"),  # header only
        (HEADER + "\n,,,\n", "no sources configured"),  # only blank rows
        ("platform,identifier\nlever,site\n", "company_name"),  # missing column
        ("company_name,identifier\nA,site\n", "platform"),
    ],
)
def test_empty_or_incomplete_files_fail_explicitly(
    tmp_path: Path, text: str, fragment: str
) -> None:
    with pytest.raises(ValueError, match=fragment):
        load_sources(write(tmp_path, text))


def test_row_with_extra_fields_is_a_clear_error_not_a_crash(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + "greenhouse,Example Corp,board,,surplus\n")
    with pytest.raises(ValueError, match=r"line 2: row has more fields"):
        load_sources(path)


@pytest.mark.parametrize(
    "text",
    [
        HEADER + 'greenhouse,"Unclosed,board,\n',  # unterminated quote
        HEADER + 'greenhouse,"Ex"ample,board,\n',  # stray quote after a closing quote
    ],
)
def test_malformed_csv_is_a_value_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError, match="malformed CSV"):
        load_sources(write(tmp_path, text))


def test_short_row_fails_on_the_blank_required_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"line 2: identifier"):
        load_sources(write(tmp_path, HEADER + "greenhouse,Example Corp\n"))


def test_missing_file_and_bad_encoding_are_reported(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_sources(tmp_path / "missing.csv")
    path = tmp_path / "latin.csv"
    path.write_bytes(HEADER.encode() + b"lever,Caf\xe9,site,\n")
    with pytest.raises(ValueError):  # UnicodeDecodeError is a ValueError
        load_sources(path)


def test_check_source_does_not_invent_values() -> None:
    source = check_source("lever", " Example Corp ", " site ")
    assert source == Source("lever", "Example Corp", "site", None)
    assert check_source("lever", "A", "site", "   ").company_website is None
