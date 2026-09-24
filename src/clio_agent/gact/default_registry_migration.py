"""One-time default-registry re-sync when the running clio-agent version changes.

A deployed install clones the marketplace registry on first run and, for a
remote (git) registry source, never re-syncs it: ``sync_local_registry_packs``
only walks a LOCAL checkout (the dev submodule). So a box upgraded from an
older clio-agent keeps the pack snapshots it first installed, even when the new
runtime changes what a pack must declare (v15 S8: per-agent ``a2ui_catalogs``
-- an old EarthScope snapshot uses the pre-S8 mapping form, an old base-agent
lists no catalogs).

This owner module (no accretion into ``agent_blueprint_refresh.py``) records
the clio-agent version at the last default-registry sync in a small marker
file beside the global install root. When the running version differs, it
re-installs the UNEDITED default-registry packs from the registry source in one
install call (one clone for a remote source), then records the new version.
Every decision is a typed ``blueprint_install_reason``, never silent:

* ``default_registry_pack_locally_edited`` -- the installed tree no longer
  matches the checksum recorded at its own install time; the user's edits are
  left alone (the same guard ``_reinstall_reason`` applies on every boot);
* ``default_registry_pack_foreign_source`` -- the installed pack came from a
  different source than the default registry, so it is not the registry's to
  replace;
* ``default_registry_migrated`` -- the re-sync ran; its installed/skipped ids;
* ``default_registry_migration_failed`` -- the re-sync raised (offline, clone
  error). The version is NOT recorded, so the next boot retries.

User-uninstalled packs (the tombstone ledger) are skipped exactly as the
per-boot sync skips them.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Marker file (beside the installed packs) holding the last-synced version.
SYNC_MARKER_NAME = ".clio-default-registry-sync.json"

_MIGRATION_CHECKED_FOR: set[str] = set()
_MIGRATION_LOCK = threading.Lock()


def reset_default_registry_migration_for_tests() -> None:
    """Clear the once-per-process migration gate (test isolation)."""

    _MIGRATION_CHECKED_FOR.clear()


def running_clio_agent_version() -> str:
    """The running clio-agent version (the migration trigger's left side)."""

    from clio_agent import __version__  # noqa: PLC0415

    return str(__version__)


def sync_marker_path(install_root: Path) -> Path:
    """Where the last-synced clio-agent version is recorded."""

    return install_root / SYNC_MARKER_NAME


def recorded_sync_version(install_root: Path) -> str:
    """The clio-agent version recorded at the last default-registry sync, or ``""``."""

    path = sync_marker_path(install_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        logger.warning("default_registry_sync_marker_unreadable path=%s error=%r", path, exc)
        return ""
    return str(data.get("clio_agent_version") or "") if isinstance(data, dict) else ""


def record_sync_version(install_root: Path, version: str) -> None:
    """Record ``version`` as the clio-agent version of the last default-registry sync."""

    install_root.mkdir(parents=True, exist_ok=True)
    sync_marker_path(install_root).write_text(
        json.dumps({"clio_agent_version": version}, indent=2) + "\n", encoding="utf-8"
    )


def pack_locally_edited(root: Path) -> bool:
    """Whether an installed pack's tree drifted from the checksum recorded at install.

    The local-edits guard shared with ``agent_blueprint_refresh._reinstall_reason``:
    a pack with no recorded checksum is not considered edited.
    """

    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
        _tree_checksum,
        read_install_metadata,
    )

    recorded = str(read_install_metadata(root).get("checksum") or "").strip()
    return bool(recorded) and _tree_checksum(root) != recorded


def _skip_ids(install_root: Path, source: str, *, home: Path, cwd: Path) -> dict[str, str]:
    """Installed packs the re-sync must not touch, each with its typed reason."""

    from clio_agent.gact.agent_blueprint_refresh import (  # noqa: PLC0415
        read_uninstalled_tombstones,
        record_blueprint_install_reason,
    )
    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
        _BLUEPRINT_ROOT_NAME,
        read_install_metadata,
    )

    skips = dict.fromkeys(read_uninstalled_tombstones(home=home, cwd=cwd), "user_uninstalled")
    if not install_root.is_dir():
        return skips
    for root in sorted(install_root.iterdir()):
        if not (root / _BLUEPRINT_ROOT_NAME).exists():
            continue
        installed_source = str(read_install_metadata(root).get("source") or "")
        if installed_source != source:
            skips[root.name] = "default_registry_pack_foreign_source"
        elif pack_locally_edited(root):
            skips[root.name] = "default_registry_pack_locally_edited"
        else:
            continue
        record_blueprint_install_reason(
            skips[root.name],
            blueprint_id=root.name,
            installed_source=installed_source,
            registry_source=source,
        )
    return skips


def migrate_default_registry_on_version_change(
    *, source: str, home: Path, cwd: Path, ref: str, pinned: str
) -> str:
    """Re-sync unedited default-registry packs once per clio-agent version change.

    Args:
        source: The default registry install source (git URL or local path).
        home: The home directory the install root derives from.
        cwd: The working directory the install root derives from.
        ref: The registry ref to clone.
        pinned: The pinned registry commit (``""`` when unpinned).

    Returns:
        ``""`` when nothing had to happen or the re-sync succeeded, otherwise a
        diagnostic (the failure is also a typed reason and is retried next boot).
    """

    from clio_agent.gact.agent_blueprint_refresh import (  # noqa: PLC0415
        record_blueprint_install_reason,
    )
    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
        _install_root,
        install_agent_blueprint,
    )

    install_root = _install_root(home=home, cwd=cwd, scope="global")
    gate_key = str(install_root)
    with _MIGRATION_LOCK:
        if gate_key in _MIGRATION_CHECKED_FOR:
            return ""
        version = running_clio_agent_version()
        previous = recorded_sync_version(install_root)
        if previous == version:
            _MIGRATION_CHECKED_FOR.add(gate_key)
            return ""
        skips = _skip_ids(install_root, source, home=home, cwd=cwd)
        try:
            result: dict[str, Any] = install_agent_blueprint(
                source=source,
                scope="global",
                cwd=cwd,
                home=home,
                ref=ref,
                blueprint_id="",
                pinned_commit=pinned,
                skip_invalid=True,
                skip_blueprint_ids=skips,
            )
        except Exception as exc:  # noqa: BLE001 - typed, retried next boot
            record_blueprint_install_reason(
                "default_registry_migration_failed",
                previous_version=previous,
                version=version,
                source=source,
                error=repr(exc),
            )
            _MIGRATION_CHECKED_FOR.add(gate_key)
            return f"default registry re-sync for clio-agent {version} failed: {exc}"
        record_sync_version(install_root, version)
        record_blueprint_install_reason(
            "default_registry_migrated",
            previous_version=previous,
            version=version,
            source=source,
            installed=[str(row.get("id") or "") for row in result.get("installed") or []],
            skipped={
                str(row.get("id") or ""): str(row.get("reason") or "invalid")
                for row in result.get("skipped") or []
            },
        )
        _MIGRATION_CHECKED_FOR.add(gate_key)
        return ""


__all__ = [
    "SYNC_MARKER_NAME",
    "migrate_default_registry_on_version_change",
    "pack_locally_edited",
    "record_sync_version",
    "recorded_sync_version",
    "reset_default_registry_migration_for_tests",
    "running_clio_agent_version",
    "sync_marker_path",
]
