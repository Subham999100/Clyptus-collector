"""Typed configuration. Override any field with a ``COLLECTOR_<FIELD>`` environment variable."""

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Collector settings.

    Constructing this object only reads environment variables. It never touches the
    filesystem (no database or directories are created) and never opens a network connection.
    """

    model_config = SettingsConfigDict(env_prefix="COLLECTOR_", extra="ignore")

    date_window_days: int = 30
    timezone: str = "Asia/Kolkata"  # business timezone; an IANA name
    robots_txt_obey: bool = True
    concurrent_requests_per_domain: int = 1
    concurrent_requests: int = 4
    request_timeout_seconds: int = 20
    retry_times: int = 2
    download_delay_seconds: float = 1.0
    autothrottle_enabled: bool = True
    sqlite_path: Path = Path("data/collector.db")
    output_dir: Path = Path("output")
    discovery_state_path: Path = Path("data/discovery.json")  # source-discovery results
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        """The business timezone as a ``tzinfo``."""
        return ZoneInfo(self.timezone)


def get_settings() -> Settings:
    """Load settings from the environment."""
    return Settings()
