"""URL normalization. Pure string handling: never makes network requests."""

from functools import cache
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import tldextract

TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset(
    {"gclid", "dclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "gh_src"}
)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking(name: str) -> bool:
    name = unquote_plus(name).lower()
    return name in TRACKING_PARAMS or name.startswith(TRACKING_PARAM_PREFIXES)


def normalize_url(url: str, *, lowercase_path: bool = False) -> str:
    """Return a canonical form of an http(s) URL so equivalent URLs compare equal.

    - Scheme and host are lower-cased; default ports (80/443) are dropped.
    - The fragment is removed; an empty path becomes ``/``.
    - Known tracking parameters (``utm_*``, ``gclid``, ``fbclid`` ...) are removed. Every other
      query parameter is kept byte-for-byte, and the remaining ones are sorted so parameter
      order does not matter.
    - Path case is preserved, because paths are case-sensitive on most servers. Pass
      ``lowercase_path=True`` only for a platform that explicitly guarantees case-insensitive
      paths.

    Limitations: redirects are not followed, so two URLs that redirect to one another are
    different here; trailing slashes and percent-encoding variants are not unified.

    Raises:
        ValueError: if the URL is not a well-formed http(s) URL with a host.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS or not host:
        raise ValueError(f"not an absolute http(s) URL: {url!r}")

    netloc = f"[{host}]" if ":" in host else host  # hostname is already lower-cased
    port = parts.port  # raises ValueError on a malformed port
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    userinfo, has_userinfo, _ = parts.netloc.rpartition("@")
    if has_userinfo:
        netloc = f"{userinfo}@{netloc}"

    path = parts.path or "/"
    if lowercase_path:
        path = path.lower()

    pairs = [p for p in parts.query.split("&") if p and not _is_tracking(p.partition("=")[0])]
    return urlunsplit((scheme, netloc, path, "&".join(sorted(pairs)), ""))


@cache
def _extractor() -> tldextract.TLDExtract:
    # suffix_list_urls=() uses the bundled public-suffix snapshot only: no network, no disk cache.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def registered_domain(url: str) -> str | None:
    """Registered domain of a URL's host (``https://www.example.co.uk/x`` -> ``example.co.uk``).

    Offline. Returns ``None`` for hosts without a public suffix (localhost, IP addresses).
    """
    return _extractor()(url).top_domain_under_public_suffix or None
