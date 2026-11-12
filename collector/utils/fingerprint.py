"""Deterministic content fingerprint, used to detect real changes to a stored job."""

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from typing import Any

FINGERPRINT_FIELDS = (
    "title",
    "company_name",
    "location",
    "description_snippet",
    "salary_text",
    "employment_type",
    "work_type",
)


def _normalize(value: Any) -> str | None:
    """NFC-normalize and collapse whitespace; blank or missing becomes ``None``."""
    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFC", str(value)).split()) or None


def content_fingerprint(fields: Mapping[str, Any]) -> str:
    """SHA-256 (hex) over the normalized ``FINGERPRINT_FIELDS`` of ``fields``.

    The fields are serialized as a compact, key-sorted JSON object, so field boundaries are
    unambiguous and a missing value (``null``) differs from an empty string-like value. Case is
    preserved. Observation timestamps (``collected_at``, ``last_seen_at``) and URLs are not
    part of the fingerprint; pass the final effective record, not partial incoming data.
    """
    payload = {name: _normalize(fields.get(name)) for name in FINGERPRINT_FIELDS}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
