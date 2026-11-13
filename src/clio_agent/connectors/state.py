"""A person's connector choices: ``<user config>/connectors.yaml`` (CN3, CN4).

For each connector: whether it is enabled (installed and ready; it reaches a
conversation only through its plugin, CN10 *Amended*) and the values the person gave
for its needs, except secrets (:mod:`.secrets`). Custom connectors (AQ11, "Add by URL or command") are
stored here too, as entries. The file is written atomically, owner-only.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from clio_agent import paths
from clio_agent.platform_paths import atomic_write_text

_LOCK = threading.RLock()
FILE_NAME = "connectors.yaml"


class ConnectorStateError(ValueError):
    """The connector state file could not be read or the request is invalid."""


@dataclass(frozen=True)
class Choice:
    """One connector's saved choices."""

    enabled: bool = False
    values: Mapping[str, str] = field(default_factory=dict)


def state_path() -> Path:
    return paths.user_config_dir() / FILE_NAME


def _read() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {"connectors": {}, "custom": {}}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConnectorStateError(f"could not read {path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ConnectorStateError(f"{path} must contain a mapping")
    return {
        "connectors": dict(document.get("connectors") or {}),
        "custom": dict(document.get("custom") or {}),
    }


def _write(document: Mapping[str, Any]) -> None:
    _CHOICES.clear()
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        yaml.safe_dump(dict(document), sort_keys=True, allow_unicode=True),
        file_mode=0o600,
    )


def _choice(raw: Any) -> Choice:
    if not isinstance(raw, Mapping):
        return Choice()
    values = raw.get("values") or {}
    return Choice(
        enabled=bool(raw.get("enabled")),
        values={str(k): str(v) for k, v in values.items() if v is not None}
        if isinstance(values, Mapping)
        else {},
    )


_CHOICES: dict[str, Any] = {}


def choices() -> dict[str, Choice]:
    """Every connector's saved choices, by id (re-read when the file changes)."""

    with _LOCK:
        stamp = (str(state_path()), revision())
        if _CHOICES.get("stamp") != stamp:
            _CHOICES.update(
                stamp=stamp,
                value={str(key): _choice(raw) for key, raw in _read()["connectors"].items()},
            )
        return dict(_CHOICES["value"])


def choice(connector_id: str) -> Choice:
    return choices().get(connector_id, Choice())


def update(
    connector_id: str,
    *,
    enabled: bool | None = None,
    values: Mapping[str, str | None] | None = None,
) -> Choice:
    """Change one connector's choices; a ``None`` value clears that need."""

    with _LOCK:
        document = _read()
        current = dict(document["connectors"].get(connector_id) or {})
        # A scope an older CLIO wrote is not read any more (CN10 *Amended*).
        current.pop("scope", None)
        if enabled is not None:
            current["enabled"] = enabled
        if values is not None:
            merged = dict(current.get("values") or {})
            for key, value in values.items():
                if value is None or value == "":
                    merged.pop(key, None)
                else:
                    merged[key] = value
            current["values"] = merged
        document["connectors"][connector_id] = current
        _write(document)
        return _choice(current)


def forget(connector_id: str) -> None:
    """Remove a connector's choices and, for a custom one, its entry."""

    with _LOCK:
        document = _read()
        document["connectors"].pop(connector_id, None)
        document["custom"].pop(connector_id, None)
        _write(document)


def custom_entries() -> dict[str, dict[str, Any]]:
    with _LOCK:
        return {str(k): dict(v) for k, v in _read()["custom"].items() if isinstance(v, Mapping)}


def save_custom(entry: Mapping[str, Any]) -> None:
    with _LOCK:
        document = _read()
        document["custom"][str(entry["id"])] = dict(entry)
        _write(document)


def revision() -> int:
    """Changes whenever the file does (for caches keyed by the person's choices)."""

    try:
        return state_path().stat().st_mtime_ns
    except OSError:
        return 0


def all_entries() -> dict[str, Any]:
    """Catalog entries, then the person's custom connectors (a custom id never shadows one)."""

    from clio_agent.connectors.entries import entry_from_raw, load_catalog  # noqa: PLC0415

    entries: dict[str, Any] = dict(load_catalog().entries)
    for connector_id, raw in custom_entries().items():
        if connector_id not in entries:
            entries[connector_id] = entry_from_raw(raw, folder="", source="custom")
    return entries
