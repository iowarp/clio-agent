"""Durable per-provider API key store (the picker's "Save key").

A key saved from a client lives here, in one 0600 JSON file under the user's
config dir, persisted through
:class:`clio_agent.tools.atomic_json_store.AtomicJsonFileStore` (atomic,
created at 0600) -- the same durable credential store the Codex subscription
credential and MCP OAuth tokens use. Every read goes to the file, so a
restarted service sees exactly what was saved before it stopped; there is no
in-memory copy that could outlive or diverge from it.

Resolution order for a provider's default key (see
:func:`clio_agent.providers.model_discovery.resolve_cloud_api_key` and
:func:`clio_agent.providers.credentials.resolve`): a key saved here wins, then
the provider's own environment variable, then ``CLIO_LM_API_KEY``. Never log a
key: only provider ids and typed reasons reach the trace.
"""

from __future__ import annotations

from pathlib import Path

from clio_agent import paths
from clio_agent.tools.atomic_json_store import AtomicJsonFileStore

__all__ = ["ProviderApiKeyStore", "stored_api_key"]

_SCHEMA = "clio-agent.provider-api-keys.v1"
_FILE_BASENAME = "provider_api_keys.json"


def _default_path() -> Path:
    # Resolved per call, never baked in: ``user_config_dir`` follows
    # ``CLIO_USER_DIR`` (per-test / per-stack isolation).
    return paths.user_config_dir() / _FILE_BASENAME


class ProviderApiKeyStore:
    """Save, load and clear the API key a client saved for one provider."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._store = AtomicJsonFileStore(
            path or _default_path(), schema=_SCHEMA, trace_tag="PROVIDERS"
        )

    def load(self, provider_id: str) -> str:
        """The saved key for ``provider_id``, or ``""`` when none was saved."""
        entry = self._store.read_entries().get(provider_id)
        key = entry.get("api_key") if isinstance(entry, dict) else None
        return key if isinstance(key, str) else ""

    def save(self, provider_id: str, api_key: str) -> None:
        """Persist ``api_key`` for ``provider_id``, replacing any previous one."""
        if not api_key:
            raise ValueError("api_key must be non-empty")
        entries = self._store.read_entries()
        entries[provider_id] = {"api_key": api_key}
        self._store.write_entries(entries)

    def clear(self, provider_id: str) -> bool:
        """Delete the saved key for ``provider_id``; ``True`` when one existed."""
        entries = self._store.read_entries()
        if entries.pop(provider_id, None) is None:
            return False
        self._store.write_entries(entries)
        return True


def stored_api_key(provider_id: str) -> str:
    """The key a client saved for ``provider_id`` (``""`` when none)."""
    return ProviderApiKeyStore().load(provider_id)
