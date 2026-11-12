"""Stable job identity.

Strategy:

1. If the record has an ``external_id``: ``platform + company + external_id``.
2. Otherwise: the normalized ``apply_url``.

The job title is never part of the identity.

The caller (platform adapter) must only set ``external_id`` when the platform guarantees it is
stable and unique within a company; this module cannot verify that.

Known limitations:

- Redirects are not followed. If an apply URL redirects elsewhere, or the platform changes URL
  shapes, the same job gets a new key (a URL-based key is only as stable as the URL).
- Company scope is the casefolded, whitespace-collapsed company name. Names that vary between
  sources (e.g. "Infosys" vs "Infosys Limited") produce different keys for the same external id,
  so the same job can be stored twice. No fuzzy or speculative company matching is done. A later
  adapter phase should scope external ids by a stable platform board identifier (for example the
  ATS board/tenant slug) instead of the company name.
- The same job published on two platforms has two keys (platform is part of the key).
- Platform-specific ID formats (e.g. numeric vs. slug forms of one id) are not unified;
  adapters should supply one canonical form.
"""

import hashlib

from collector.models import JobRecord
from collector.utils.urls import normalize_url

_SEP = "\x1f"


def _digest(kind: str, *parts: str) -> str:
    return f"{kind}:{hashlib.sha256(_SEP.join(parts).encode()).hexdigest()}"


def job_key(record: JobRecord) -> str:
    """Return the stable identity key (``ext:<sha256>`` or ``url:<sha256>``) for a job."""
    if record.external_id:
        company = " ".join(record.company_name.casefold().split())
        return _digest("ext", record.platform.casefold(), company, record.external_id)
    return _digest("url", normalize_url(str(record.apply_url)))
