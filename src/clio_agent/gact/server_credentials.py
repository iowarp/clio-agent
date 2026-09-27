"""Per-port credential record a local GACT server publishes for the Desktop (#1478).

The Desktop first tries to attach to a CLIO that is already running on the
conventional local port (``supervisor_attach.rs``). It did not spawn that
server, so it never saw the bearer token the server enforces, and handed the UI
an empty one. HTTP from loopback does not need the token, but the Desktop SSH
transport WebSocket does, so every remote deploy through such a server failed
with a bare "rejected".

A foreground server therefore publishes ``<runtime state dir>/gact-servers/
<port>.json`` for as long as it serves, readable only by its user (mode 0600 on
POSIX; on Windows the file lives under the user's profile, whose ACL already
excludes other users). The runtime state dir is the per-machine directory both
the Python runtime and the Desktop resolve identically
(:func:`clio_agent.arc.clio_core_config.runtime_state_dir`,
``clio_core_registry.rs::runtime_state_dir``). The record names the port, the
serving process id, and the bearer token (``null`` when the server enforces
none), so the Desktop can present exactly what the server checks.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from clio_agent.arc.clio_core_config import runtime_state_dir
from clio_agent.platform_paths import atomic_replace

logger = logging.getLogger(__name__)

RECORD_DIR_NAME = "gact-servers"
RECORD_SCHEMA = 1


def credential_record_path(port: int, *, state_dir: Path | None = None) -> Path:
    """Return where the server on ``port`` publishes its credential record.

    Args:
        port: The TCP port the server listens on.
        state_dir: Override for the runtime state dir (tests).

    Returns:
        ``<runtime state dir>/gact-servers/<port>.json``.
    """

    root = state_dir if state_dir is not None else runtime_state_dir()
    return root / RECORD_DIR_NAME / f"{int(port)}.json"


def publish_server_credentials(
    port: int,
    bearer_token: str | None,
    *,
    pid: int | None = None,
    state_dir: Path | None = None,
) -> Path:
    """Write the credential record for the server on ``port``, owner-only.

    Args:
        port: The TCP port the server listens on.
        bearer_token: The bearer token the server enforces, or ``None``.
        pid: The serving process id (defaults to this process).
        state_dir: Override for the runtime state dir (tests).

    Returns:
        The path written.

    Raises:
        OSError: The record could not be written. The caller decides; a server
            that cannot publish still serves, and the Desktop reports that it
            cannot authenticate instead of guessing.
    """

    path = credential_record_path(port, state_dir=state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": RECORD_SCHEMA,
        "port": int(port),
        "pid": int(pid if pid is not None else os.getpid()),
        "bearer_token": bearer_token,
    }
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.chmod(staging, 0o600)
    atomic_replace(staging, path)
    return path


def remove_server_credentials(
    port: int, *, pid: int | None = None, state_dir: Path | None = None
) -> bool:
    """Remove the record for ``port`` if it still belongs to ``pid``.

    A later server on the same port may already have replaced the record; that
    record is left alone.

    Args:
        port: The TCP port the server listened on.
        pid: The process that published it (defaults to this process).
        state_dir: Override for the runtime state dir (tests).

    Returns:
        ``True`` when this process's record was removed.
    """

    path = credential_record_path(port, state_dir=state_dir)
    owner = int(pid if pid is not None else os.getpid())
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        logger.warning("gact_credential_record_unreadable path=%s error=%r", path, exc)
        return False
    if not isinstance(recorded, dict) or recorded.get("pid") != owner:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
