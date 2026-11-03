"""Secret lookup - env vars locally, Azure Key Vault when deployed.

    secrets = ChainedSecretProvider(EnvSecretProvider(), KeyVaultSecretProvider(vault_url))
    api_key = secrets.require("ERP_API_KEY")

Key Vault needs the extra: pip install p2p-sync-engine[azure]
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Protocol

from p2p_sync.core.errors import ConfigurationError


class SecretProvider(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvSecretProvider:
    def __init__(self, prefix: str = "", environ: Mapping[str, str] | None = None) -> None:
        self.prefix = prefix
        self._environ = environ if environ is not None else os.environ

    def get(self, name: str) -> str | None:
        value = self._environ.get(f"{self.prefix}{name}")
        return value or None


class _SecretClient(Protocol):
    def get_secret(self, name: str) -> Any: ...


_INVALID_KEY_VAULT_CHARS = re.compile(r"[^0-9A-Za-z-]")


def to_key_vault_name(name: str) -> str:
    # Key Vault only allows letters, digits and dashes: ERP_API_KEY -> erp-api-key
    return _INVALID_KEY_VAULT_CHARS.sub("-", name).lower()


class KeyVaultSecretProvider:
    def __init__(self, vault_url: str | None = None, *, client: _SecretClient | None = None) -> None:
        if client is None:
            if not vault_url:
                raise ConfigurationError("KeyVaultSecretProvider needs a vault_url or a client")
            client = _build_client(vault_url)
        self._client = client
        self._cache: dict[str, str | None] = {}

    def get(self, name: str) -> str | None:
        key = to_key_vault_name(name)
        if key not in self._cache:
            self._cache[key] = self._fetch(key)
        return self._cache[key]

    def _fetch(self, key: str) -> str | None:
        try:
            secret = self._client.get_secret(key)
        except Exception as exc:
            # ResourceNotFoundError has status_code 404. Checking the attribute means we don't
            # have to import azure.core here.
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        value = getattr(secret, "value", None)
        return str(value) if value else None


def _build_client(vault_url: str) -> _SecretClient:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:
        raise ConfigurationError(
            "Azure Key Vault support is not installed; run: pip install 'p2p-sync-engine[azure]'"
        ) from exc
    client: _SecretClient = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
    return client


class ChainedSecretProvider:
    def __init__(self, *providers: SecretProvider) -> None:
        if not providers:
            raise ValueError("ChainedSecretProvider needs at least one provider")
        self.providers = providers

    def get(self, name: str) -> str | None:
        for provider in self.providers:
            value = provider.get(name)
            if value is not None:
                return value
        return None

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise ConfigurationError(f"secret {name!r} was not found in any configured provider")
        return value
