"""Domain models."""

from datetime import UTC, date, datetime
from typing import Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

WorkType = Literal["remote", "hybrid", "onsite"]
PostedDateSource = Literal["jsonld", "platform", "page_text", "unknown"]


class JobRecord(BaseModel):
    """A single validated job posting.

    Required fields must be supplied by the extractor; optional fields stay ``None`` when the
    source does not provide them. Nothing is inferred or defaulted on the extractor's behalf,
    and URLs are validated syntactically only (they are never fetched).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    apply_url: HttpUrl
    source_url: HttpUrl
    platform: str = Field(min_length=1)
    # Must be timezone-aware; stored as UTC. Naive datetimes are rejected, not assumed UTC.
    collected_at: AwareDatetime

    company_website: HttpUrl | None = None
    location: str | None = None
    work_type: WorkType | None = None
    employment_type: str | None = None
    salary_text: str | None = None
    posted_date: date | None = None
    posted_date_source: PostedDateSource = "unknown"
    external_id: str | None = None
    description_snippet: str | None = None

    @field_validator("apply_url", "source_url", mode="before")
    @classmethod
    def _strip_required_url(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "company_website",
        "location",
        "employment_type",
        "salary_text",
        "external_id",
        "description_snippet",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        """Strip optional text; a blank value means "not provided", so store ``None``."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("collected_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _date_source_matches_date(self) -> Self:
        if self.posted_date is None and self.posted_date_source != "unknown":
            raise ValueError("posted_date_source must be 'unknown' when posted_date is not set")
        return self
