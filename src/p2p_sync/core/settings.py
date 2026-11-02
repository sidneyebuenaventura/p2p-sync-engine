"""Typed application settings loaded from environment variables and an optional ``.env`` file.

Every variable is prefixed with ``P2P_``; nested sections use a double underscore::

    P2P_ENVIRONMENT=production
    P2P_PROCUREMENT__BASE_URL=https://procurement.example.com/api
    P2P_ERP__API_KEY=...

Defaults point at the local mock services so the demo runs without any configuration.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class ProcurementSettings(BaseModel):
    """Connection details for the procurement system (OAuth2 client credentials)."""

    base_url: str = "http://localhost:8001"
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    page_size: int = Field(default=50, ge=1, le=500)


class ErpSettings(BaseModel):
    """Connection details for the ERP system (API key plus basic auth)."""

    base_url: str = "http://localhost:8002"
    company: str = "ACME"
    api_key: SecretStr = SecretStr("")
    username: str = ""
    password: SecretStr = SecretStr("")


class MaintenanceSettings(BaseModel):
    """Connection details for the maintenance system's database."""

    database_url: SecretStr = SecretStr("sqlite:///maintenance.db")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="P2P_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT
    log_level: LogLevel = "INFO"
    log_format: Literal["text", "json"] = "text"
    timezone: str = "UTC"
    http_timeout_seconds: float = Field(default=30.0, gt=0)

    procurement: ProcurementSettings = ProcurementSettings()
    erp: ErpSettings = ErpSettings()
    maintenance: MaintenanceSettings = MaintenanceSettings()

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone: {value!r}") from exc
        return value

    @property
    def is_development(self) -> bool:
        return self.environment is Environment.DEVELOPMENT

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loading them on first use."""
    return Settings()
