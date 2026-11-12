"""Validation of extracted job dictionaries. No persistence, no network, no guessing."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from collector.models import JobRecord


@dataclass(frozen=True)
class FieldError:
    """One validation problem. ``code`` is Pydantic's error type, e.g. ``missing``,
    ``string_too_short`` (blank), ``url_parsing``, ``url_scheme``, ``extra_forbidden``,
    ``timezone_aware`` or ``value_error``."""

    field: str
    code: str
    message: str


@dataclass(frozen=True)
class Rejection:
    """A job that failed validation, with the original input kept for reporting."""

    errors: tuple[FieldError, ...]
    raw: Mapping[str, Any]


def validate_job(raw: Mapping[str, Any]) -> JobRecord | Rejection:
    """Validate an extracted job; return the record, or a structured rejection."""
    try:
        return JobRecord.model_validate(raw)
    except ValidationError as exc:
        errors = tuple(
            FieldError(
                field=".".join(str(part) for part in err["loc"]) or "(record)",
                code=err["type"],
                message=err["msg"],
            )
            for err in exc.errors()
        )
        return Rejection(errors=errors, raw=raw)
