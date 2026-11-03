import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from p2p_sync.core.settings import Environment, Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Run each test in an empty directory with no P2P_* variables leaking in."""
    for key in list(os.environ):
        if key.startswith("P2P_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_target_local_mocks() -> None:
    settings = Settings()

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.is_development
    assert settings.procurement.base_url == "http://localhost:8001"
    assert settings.erp.base_url == "http://localhost:8002"


def test_reads_prefixed_and_nested_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2P_ENVIRONMENT", "production")
    monkeypatch.setenv("P2P_LOG_LEVEL", "debug")
    monkeypatch.setenv("P2P_ERP__COMPANY", "GLOBEX")
    monkeypatch.setenv("P2P_ERP__API_KEY", "erp-key")

    settings = Settings()

    assert settings.environment is Environment.PRODUCTION
    assert not settings.is_development
    assert settings.log_level == "DEBUG"
    assert settings.erp.company == "GLOBEX"
    assert settings.erp.api_key.get_secret_value() == "erp-key"


def test_reads_dotenv_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("P2P_PROCUREMENT__CLIENT_ID=from-dotenv\n", encoding="utf-8")

    assert Settings().procurement.client_id == "from-dotenv"


def test_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2P_PROCUREMENT__CLIENT_SECRET", "super-secret-value")

    settings = Settings()

    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in settings.model_dump_json()


def test_rejects_unknown_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2P_TIMEZONE", "Mars/Olympus_Mons")

    with pytest.raises(ValidationError, match="unknown time zone"):
        Settings()


def test_exposes_zoneinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2P_TIMEZONE", "Asia/Manila")

    assert Settings().tz.key == "Asia/Manila"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
