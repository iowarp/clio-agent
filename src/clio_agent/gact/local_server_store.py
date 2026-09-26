"""Saved local and self-hosted model servers, persisted in the user ``config.yaml``.

A *server entry* is a model server the person runs themselves: a local
runtime at a non-default address (LM Studio on another port, Ollama on another
host) or any OpenAI-compatible server on another machine or a cluster node.
Each entry names the catalog preset it is reached through (``lm_studio``,
``ollama``, ``llama_cpp``, ``vllm`` -- a custom server uses the generic
OpenAI-compatible ``vllm`` preset) and its address.

Entries live in the EXISTING user configuration file
(``<user config dir>/config.yaml``, the file :mod:`clio_agent.conf` reads as
its user layer), under ``providers.servers`` -- no new store (RULE 4). The
read/modify/write is one atomic transaction that preserves every other key,
and :func:`clio_agent.conf.reload` runs after each write so the resolver
sees the new document.

Only configuration is persisted (id, preset, label, address). A reachability
check's result is runtime evidence, not configuration: callers keep it in
memory (see :mod:`clio_agent.gact.routes.local_servers`).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from clio_agent import conf, paths

logger = logging.getLogger(__name__)

__all__ = [
    "CUSTOM_SERVER_PRESET_ID",
    "LocalServerEntry",
    "LocalServerStoreError",
    "add_server",
    "get_server",
    "list_servers",
    "normalize_server_address",
    "remove_server",
    "saved_address_for_preset",
    "update_server",
    "user_config_path",
]

#: The OpenAI-compatible preset every custom (non-catalog) server is reached through.
CUSTOM_SERVER_PRESET_ID = "vllm"

_LOCK = threading.RLock()
_SECTION = ("providers", "servers")


class LocalServerStoreError(ValueError):
    """The saved-server configuration could not be read, validated or written."""


@dataclass(frozen=True)
class LocalServerEntry:
    """One saved server: its id, the preset it is reached through, a label and an address.

    ``id`` equals ``preset_id`` for a catalog local runtime whose address was
    changed (one entry per such preset); a custom server gets ``server-<slug>``.
    """

    id: str
    preset_id: str
    label: str
    address: str

    @property
    def custom(self) -> bool:
        """Whether this is a server added by the person rather than a catalog runtime."""
        return self.id != self.preset_id

    def to_wire(self) -> dict[str, Any]:
        """The entry as the API returns it."""
        return {**asdict(self), "custom": self.custom}


def user_config_path() -> Path:
    """The active user's ``config.yaml`` (the resolver's user layer)."""
    return paths.user_config_dir() / "config.yaml"


def normalize_server_address(address: str) -> str:
    """Return an address as an ``http(s)://host[:port]/path`` URL, defaulting the path to ``/v1``.

    Raises:
        LocalServerStoreError: When the address is empty or not an http(s) URL.
    """
    from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

    text = str(address or "").strip()
    if not text:
        raise LocalServerStoreError("a server address is required")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, flags=re.IGNORECASE):
        text = f"http://{text}"
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise LocalServerStoreError(f"not an http(s) server address: {address!r}")
    path = parts.path.rstrip("/") or "/v1"
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))


def _read_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise LocalServerStoreError(f"could not read {path}: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise LocalServerStoreError(f"{path} must contain a YAML mapping")
    return dict(loaded)


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        dict(document), allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise LocalServerStoreError(f"could not write {path}: {exc}") from exc
    conf.reload()


def _entries(document: Mapping[str, Any]) -> list[LocalServerEntry]:
    providers = document.get(_SECTION[0])
    raw = providers.get(_SECTION[1]) if isinstance(providers, Mapping) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LocalServerStoreError("providers.servers must be a list")
    entries: list[LocalServerEntry] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise LocalServerStoreError("each providers.servers entry must be a mapping")
        try:
            entries.append(
                LocalServerEntry(
                    id=str(item["id"]),
                    preset_id=str(item["preset_id"]),
                    label=str(item.get("label") or item["id"]),
                    address=str(item["address"]),
                )
            )
        except KeyError as exc:
            raise LocalServerStoreError(f"providers.servers entry is missing {exc}") from exc
    return entries


def _store(document: dict[str, Any], entries: list[LocalServerEntry]) -> None:
    providers = document.get(_SECTION[0])
    section = dict(providers) if isinstance(providers, Mapping) else {}
    section[_SECTION[1]] = [
        {"id": e.id, "preset_id": e.preset_id, "label": e.label, "address": e.address}
        for e in entries
    ]
    document[_SECTION[0]] = section


def list_servers() -> list[LocalServerEntry]:
    """Every saved server, in the order they were added."""
    with _LOCK:
        return _entries(_read_document(user_config_path()))


def get_server(server_id: str) -> LocalServerEntry | None:
    """The saved server with ``server_id``, or ``None``."""
    return next((entry for entry in list_servers() if entry.id == server_id), None)


def saved_address_for_preset(preset_id: str) -> str | None:
    """The saved address of a catalog runtime (its own entry), or ``None``.

    Never raises: a malformed config file is reported by the routes that edit
    it, and discovery then simply uses the preset's own address.
    """
    try:
        entry = get_server(preset_id)
    except LocalServerStoreError as exc:
        logger.warning(
            "saved_server_address_unreadable preset=%s reason=%s -- probing the preset's own address",
            preset_id,
            exc,
        )
        return None
    return entry.address if entry is not None and not entry.custom else None


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "server"


def add_server(*, address: str, label: str = "", preset_id: str | None = None) -> LocalServerEntry:
    """Save a server and return it.

    With ``preset_id`` naming a catalog runtime, the entry IS that runtime's
    saved address (id == preset id; replaces any earlier one). Without it, a
    custom OpenAI-compatible server is added under a fresh ``server-<slug>`` id.
    """
    normalized = normalize_server_address(address)
    with _LOCK:
        path = user_config_path()
        document = _read_document(path)
        entries = _entries(document)
        if preset_id:
            entry = LocalServerEntry(
                id=preset_id, preset_id=preset_id, label=label or preset_id, address=normalized
            )
            entries = [e for e in entries if e.id != preset_id] + [entry]
        else:
            name = label.strip() or (_host_of(normalized) or "Server")
            base = f"server-{_slug(name)}"
            taken = {e.id for e in entries}
            server_id, n = base, 2
            while server_id in taken:
                server_id, n = f"{base}-{n}", n + 1
            entry = LocalServerEntry(
                id=server_id, preset_id=CUSTOM_SERVER_PRESET_ID, label=name, address=normalized
            )
            entries.append(entry)
        _store(document, entries)
        _write_document(path, document)
        return entry


def update_server(
    server_id: str, *, address: str | None = None, label: str | None = None
) -> LocalServerEntry:
    """Change a saved server's address and/or label.

    Raises:
        KeyError: When no server has ``server_id``.
    """
    with _LOCK:
        path = user_config_path()
        document = _read_document(path)
        entries = _entries(document)
        current = next((e for e in entries if e.id == server_id), None)
        if current is None:
            raise KeyError(server_id)
        updated = LocalServerEntry(
            id=current.id,
            preset_id=current.preset_id,
            label=label.strip() if label and label.strip() else current.label,
            address=normalize_server_address(address) if address is not None else current.address,
        )
        _store(document, [updated if e.id == server_id else e for e in entries])
        _write_document(path, document)
        return updated


def remove_server(server_id: str) -> None:
    """Forget a saved server (a catalog runtime goes back to its own address).

    Raises:
        KeyError: When no server has ``server_id``.
    """
    with _LOCK:
        path = user_config_path()
        document = _read_document(path)
        entries = _entries(document)
        if not any(e.id == server_id for e in entries):
            raise KeyError(server_id)
        _store(document, [e for e in entries if e.id != server_id])
        _write_document(path, document)


def _host_of(address: str) -> str:
    from urllib.parse import urlsplit  # noqa: PLC0415

    return urlsplit(address).hostname or ""
