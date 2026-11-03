from dataclasses import dataclass
from typing import Any

import pytest

from p2p_sync.core.errors import ConfigurationError
from p2p_sync.core.secrets import (
    ChainedSecretProvider,
    EnvSecretProvider,
    KeyVaultSecretProvider,
    to_key_vault_name,
)


@dataclass
class _Secret:
    value: str | None


class _NotFoundError(Exception):
    status_code = 404


class _ForbiddenError(Exception):
    status_code = 403


class FakeSecretClient:
    """Stands in for azure.keyvault.secrets.SecretClient."""

    def __init__(self, secrets: dict[str, str], error: Exception | None = None) -> None:
        self.secrets = secrets
        self.error = error
        self.calls: list[str] = []

    def get_secret(self, name: str) -> Any:
        self.calls.append(name)
        if self.error:
            raise self.error
        if name not in self.secrets:
            raise _NotFoundError(name)
        return _Secret(self.secrets[name])


# -- EnvSecretProvider ----------------------------------------------------------------------


def test_env_provider_reads_with_prefix() -> None:
    provider = EnvSecretProvider(prefix="P2P_", environ={"P2P_ERP_API_KEY": "k"})

    assert provider.get("ERP_API_KEY") == "k"
    assert provider.get("MISSING") is None


def test_env_provider_treats_empty_as_missing() -> None:
    assert EnvSecretProvider(environ={"TOKEN": ""}).get("TOKEN") is None


def test_env_provider_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_SECRET", "from-os")

    assert EnvSecretProvider().get("SOME_SECRET") == "from-os"


# -- KeyVaultSecretProvider -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [("ERP_API_KEY", "erp-api-key"), ("procurement.client_secret", "procurement-client-secret")],
)
def test_to_key_vault_name(name: str, expected: str) -> None:
    assert to_key_vault_name(name) == expected


def test_key_vault_provider_maps_name_and_caches() -> None:
    client = FakeSecretClient({"erp-api-key": "vault-value"})
    provider = KeyVaultSecretProvider(client=client)

    assert provider.get("ERP_API_KEY") == "vault-value"
    assert provider.get("ERP_API_KEY") == "vault-value"
    assert client.calls == ["erp-api-key"]


def test_key_vault_provider_returns_none_when_missing() -> None:
    provider = KeyVaultSecretProvider(client=FakeSecretClient({}))

    assert provider.get("NOPE") is None


def test_key_vault_provider_propagates_other_errors() -> None:
    provider = KeyVaultSecretProvider(client=FakeSecretClient({}, error=_ForbiddenError("denied")))

    with pytest.raises(_ForbiddenError):
        provider.get("ERP_API_KEY")


def test_key_vault_provider_requires_url_or_client() -> None:
    with pytest.raises(ConfigurationError, match="vault_url"):
        KeyVaultSecretProvider()


def test_key_vault_provider_explains_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("azure"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ConfigurationError, match=r"p2p-sync-engine\[azure\]"):
        KeyVaultSecretProvider("https://example.vault.azure.net")


# -- ChainedSecretProvider ------------------------------------------------------------------


def test_chain_returns_first_hit_in_order() -> None:
    env = EnvSecretProvider(environ={"ERP_API_KEY": "from-env"})
    vault = KeyVaultSecretProvider(client=FakeSecretClient({"erp-api-key": "from-vault", "only-vault": "v"}))
    chain = ChainedSecretProvider(env, vault)

    assert chain.get("ERP_API_KEY") == "from-env"
    assert chain.get("ONLY_VAULT") == "v"
    assert chain.get("NOWHERE") is None


def test_chain_require_raises_configuration_error() -> None:
    chain = ChainedSecretProvider(EnvSecretProvider(environ={}))

    with pytest.raises(ConfigurationError, match="'ERP_API_KEY'"):
        chain.require("ERP_API_KEY")


def test_chain_needs_a_provider() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ChainedSecretProvider()
