import pytest

from collector.utils.identity import job_key
from collector.utils.urls import normalize_url
from tests.conftest import make_record


def test_scheme_and_host_lowercased_path_case_preserved() -> None:
    assert (
        normalize_url("HTTPS://Jobs.Example.COM/Apply/AbC") == "https://jobs.example.com/Apply/AbC"
    )


def test_lowercase_path_only_when_requested() -> None:
    assert (
        normalize_url("https://a.example/Jobs/X", lowercase_path=True) == "https://a.example/jobs/x"
    )


def test_fragment_removed() -> None:
    assert normalize_url("https://a.example/job/1#apply") == "https://a.example/job/1"


def test_tracking_params_removed() -> None:
    url = "https://a.example/job/1?utm_source=x&UTM_Medium=y&gclid=1&fbclid=2&id=7"
    assert normalize_url(url) == "https://a.example/job/1?id=7"


def test_meaningful_params_preserved_and_order_independent() -> None:
    a = normalize_url("https://a.example/jobs?gh_jid=42&lang=en")
    b = normalize_url("https://a.example/jobs?lang=en&gh_jid=42&utm_campaign=z")
    assert a == b == "https://a.example/jobs?gh_jid=42&lang=en"


def test_unknown_params_not_removed() -> None:
    assert normalize_url("https://a.example/j?ref=abc&source=x") == (
        "https://a.example/j?ref=abc&source=x"
    )


def test_param_values_kept_byte_for_byte() -> None:
    assert normalize_url("https://a.example/j?q=a%2Fb+c&empty=") == (
        "https://a.example/j?empty=&q=a%2Fb+c"
    )


def test_default_ports_dropped_other_ports_kept() -> None:
    assert normalize_url("https://a.example:443/x") == "https://a.example/x"
    assert normalize_url("http://a.example:80/x") == "http://a.example/x"
    assert normalize_url("https://a.example:8443/x") == "https://a.example:8443/x"


def test_empty_path_becomes_slash() -> None:
    assert normalize_url("https://a.example") == "https://a.example/"


@pytest.mark.parametrize("url", ["ftp://a.example/x", "/relative", "https://", "mailto:a@b.c"])
def test_non_http_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(url)


def test_normalization_is_idempotent() -> None:
    once = normalize_url("HTTPS://A.example:443/P?b=2&utm_x=1&a=1#f")
    assert normalize_url(once) == once


# --- job identity -------------------------------------------------------------------------


def test_key_with_external_id_ignores_urls() -> None:
    a = make_record(external_id="42", apply_url="https://a.example/x")
    b = make_record(external_id="42", apply_url="https://a.example/changed")
    assert job_key(a) == job_key(b)
    assert job_key(a).startswith("ext:")


def test_external_id_scoped_by_platform_and_company() -> None:
    base = job_key(make_record(external_id="42"))
    assert job_key(make_record(external_id="42", platform="other")) != base
    assert job_key(make_record(external_id="42", company_name="Other Corp")) != base
    assert job_key(make_record(external_id="43")) != base


def test_company_and_platform_scope_ignores_case_and_spacing() -> None:
    a = make_record(external_id="42", company_name="Example  Corp", platform="TestBoard")
    b = make_record(external_id="42", company_name="example corp", platform="testboard")
    assert job_key(a) == job_key(b)


def test_external_id_case_is_preserved() -> None:
    assert job_key(make_record(external_id="AbC")) != job_key(make_record(external_id="abc"))


def test_key_falls_back_to_normalized_apply_url() -> None:
    a = make_record(apply_url="https://Jobs.example.com/apply/1?utm_source=x#top")
    b = make_record(apply_url="https://jobs.example.com/apply/1")
    assert job_key(a) == job_key(b)
    assert job_key(a).startswith("url:")


def test_blank_external_id_falls_back_to_url() -> None:
    assert job_key(make_record(external_id="  ")).startswith("url:")


def test_title_is_not_part_of_identity() -> None:
    a = make_record(title="Same Title", apply_url="https://a.example/1")
    b = make_record(title="Same Title", apply_url="https://a.example/2")
    c = make_record(title="Other Title", apply_url="https://a.example/1")
    assert job_key(a) != job_key(b)
    assert job_key(a) == job_key(c)
