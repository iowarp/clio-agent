"""The user ``config.yaml`` as one read/modify/write document (owner module).

Several owners keep durable state in the user configuration file
(``<user config dir>/config.yaml``, the layer :mod:`clio_agent.conf` reads):
the bound LM selection (``lm``), saved model servers (``providers.servers``)
and the provider support a person installed (``providers.installed_support``).
Each edits only its own keys, but they all rewrite the WHOLE file, so their
read/modify/write transactions must be serialized by one process-wide lock --
two writers with separate locks can each read the old document and the second
write silently drops the first one's change.

This module owns that lock and the plain YAML read/write. Callers keep their
own typed errors and their own key layout.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from clio_agent import paths
from clio_agent.platform_paths import atomic_write_text

#: Serializes every read/modify/write of the user ``config.yaml`` in this process.
USER_CONFIG_LOCK = threading.RLock()


def user_config_path() -> Path:
    """The active user's ``config.yaml`` (``<user config dir>/config.yaml``)."""

    return paths.user_config_dir() / "config.yaml"


def read_document(path: Path) -> dict[str, Any]:
    """Read ``path`` as a YAML mapping (``{}`` when the file does not exist).

    Raises:
        OSError: The file exists but cannot be read.
        yaml.YAMLError: The file is not valid YAML.
        ValueError: The file's top level is not a mapping.
    """

    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, Mapping):
        raise ValueError(f"{path} must contain a YAML mapping")
    return dict(loaded)


def write_document(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically replace ``path`` with ``document`` rendered as YAML.

    Raises:
        OSError: The directory or file cannot be written.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        dict(document), allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    atomic_write_text(path, rendered)


__all__ = ["USER_CONFIG_LOCK", "read_document", "user_config_path", "write_document"]
