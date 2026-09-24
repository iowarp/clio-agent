"""One-time default-registry re-sync when the running clio-agent version changes.

A deployed install clones the marketplace registry on first run and, for a
remote (git) registry source, never re-syncs it: ``sync_local_registry_packs``
only walks a LOCAL checkout (the dev submodule). So a box upgraded from an
older clio-agent keeps the pack snapshots it first installed, even when the new
runtime changes what a pack must declare (v15 S8: per-agent ``a2ui_catalogs``
-- an old EarthScope snapshot uses the pre-S8 mapping form, an old base-agent
lists no catalogs).

This owner module (no accretion into ``agent_blueprint_refresh.py``) records
the clio-agent version of the last default-registry sync in a state file
beside the global install root. When the running version differs, it
re-installs the UNEDITED default-registry packs from the registry source, then
records the new version.

Safety properties:

* **Off the request path.** :func:`start_in_background` runs it on a daemon
  thread at server startup (``gact/app.py`` lifespan). An agent update ("Update
  all") restarts the server on a new version, which makes the re-sync due again
  at once. Discovery never runs it.
* **Never escapes.** :func:`migrate_default_registry_on_version_change` guards
  its whole body: any exception becomes the typed
  ``default_registry_migration_failed`` reason and discovery proceeds with
  what is installed. The once-per-process gate is set regardless.
* **Cross-process lock.** A non-blocking ``filelock`` on the install root
  (:func:`registry_install_lock`, also held by the per-boot local sync) keeps
  the server and ``clio doctor``/status from migrating concurrently; a held
  lock records ``default_registry_migration_busy`` and skips this time.
* **Atomic per-pack replace.** Each pack is built in a staging directory
  OUTSIDE the install root (so discovery never sees it), then swapped in:
  old -> backup, new -> final, backup deleted. Any failure restores the
  backup, so a pack is never left half-deleted or mis-flagged as edited.
* **Backoff.** A failure records its time, target version, and attempt
  count; the same version is retried after 1 h, 6 h, then every 24 h. A
  different running version is due immediately.

Typed skip reasons (``blueprint_install_reason`` rows):
``default_registry_pack_locally_edited`` (the installed tree drifted from the
checksum recorded at install -- the same guard ``_reinstall_reason`` applies),
``default_registry_pack_foreign_source`` (installed from another source),
``default_registry_pack_pinned`` (installed with an explicit ref or pinned
commit other than the registry's), and ``user_uninstalled`` (the tombstone
ledger; never resurrected).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: State file (beside the installed packs): last-synced version + failure backoff.
SYNC_MARKER_NAME = ".clio-default-registry-sync.json"
#: Cross-process lock file for every default-registry mutation of the install root.
LOCK_NAME = ".clio-default-registry.lock"
#: Staging/backup directory, a SIBLING of the install root (never discovered).
STAGING_DIR_NAME = ".clio-registry-staging"
#: Retry delays after consecutive failures on the SAME version (1 h, 6 h, 24 h).
BACKOFF_SECONDS: tuple[int, ...] = (3600, 6 * 3600, 24 * 3600)

_GIT_TIMEOUT_S = 60
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
    """Where the sync state (last-synced version, failure backoff) is recorded."""

    return install_root / SYNC_MARKER_NAME


def _read_state(install_root: Path) -> dict[str, Any]:
    path = sync_marker_path(install_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("default_registry_sync_marker_unreadable path=%s error=%r", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(install_root: Path, state: dict[str, Any]) -> None:
    install_root.mkdir(parents=True, exist_ok=True)
    sync_marker_path(install_root).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def recorded_sync_version(install_root: Path) -> str:
    """The clio-agent version recorded at the last default-registry sync, or ``""``."""

    return str(_read_state(install_root).get("clio_agent_version") or "")


def record_sync_version(install_root: Path, version: str) -> None:
    """Record ``version`` as synced (clears any failure backoff)."""

    _write_state(install_root, {"clio_agent_version": version})


def record_first_run_version(install_root: Path) -> None:
    """Record the running version after a first-run bootstrap install -- guarded.

    Called from ``ensure_default_registry_bootstrap`` (discovery path), so a
    failure here is logged and recorded, never raised.
    """

    try:
        record_sync_version(install_root, running_clio_agent_version())
    except Exception as exc:  # noqa: BLE001 - typed, recorded, never escapes discovery
        _record("default_registry_migration_failed", stage="first_run_marker", error=repr(exc))


def _record(reason: str, **fields: Any) -> None:
    """Record a typed install reason; a recording failure is logged, never raised."""

    try:
        from clio_agent.gact.agent_blueprint_refresh import (  # noqa: PLC0415
            record_blueprint_install_reason,
        )

        record_blueprint_install_reason(reason, **fields)
    except Exception as exc:  # noqa: BLE001 - the audit leg must not break the migration
        logger.warning("default_registry_reason_unrecorded reason=%s error=%r", reason, exc)


@contextlib.contextmanager
def registry_install_lock(install_root: Path) -> Iterator[bool]:
    """Try to take the cross-process registry lock without waiting.

    Yields ``True`` when held (released on exit), ``False`` when another
    process holds it -- the caller skips this time instead of blocking.
    """

    from filelock import FileLock, Timeout  # noqa: PLC0415

    install_root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(install_root / LOCK_NAME), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def lock_busy_diagnostic() -> str:
    """Record that the per-boot local sync skipped because the lock is held.

    Returns ``""`` -- a busy lock is not a broken registry, so discovery must not
    surface it as a disabled blueprint row; the next discovery call retries.
    """

    _record("default_registry_migration_busy", stage="local_sync")
    return ""


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


def migration_due(state: dict[str, Any], version: str, now: float) -> bool:
    """Whether a re-sync to ``version`` should run now, given the recorded state."""

    if state.get("clio_agent_version") == version:
        return False
    attempts = int(state.get("attempts") or 0)
    if state.get("failure_version") != version or attempts <= 0:
        return True
    delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
    return now - float(state.get("last_failure_at") or 0) >= delay


def _skip_ids(
    install_root: Path, source: str, *, ref: str, pinned: str, home: Path, cwd: Path
) -> dict[str, str]:
    """Installed packs the re-sync must not touch, each with its typed reason."""

    from clio_agent.gact.agent_blueprint_refresh import (  # noqa: PLC0415
        read_uninstalled_tombstones,
    )
    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
        _BLUEPRINT_ROOT_NAME,
        read_install_metadata,
    )

    skips = dict.fromkeys(read_uninstalled_tombstones(home=home, cwd=cwd), "user_uninstalled")
    if not install_root.is_dir():
        return skips
    for root in sorted(install_root.iterdir()):
        if root.name in skips or not (root / _BLUEPRINT_ROOT_NAME).exists():
            continue
        metadata = read_install_metadata(root)
        installed_source = str(metadata.get("source") or "")
        installed_ref = str(metadata.get("ref") or "")
        installed_pin = str(metadata.get("pinned_commit") or "")
        if installed_source != source:
            skips[root.name] = "default_registry_pack_foreign_source"
        elif installed_ref not in ("", ref) or installed_pin not in ("", pinned):
            skips[root.name] = "default_registry_pack_pinned"
        elif pack_locally_edited(root):
            skips[root.name] = "default_registry_pack_locally_edited"
        else:
            continue
        _record(
            skips[root.name],
            blueprint_id=root.name,
            installed_source=installed_source,
            installed_ref=installed_ref,
            installed_pinned_commit=installed_pin,
            registry_source=source,
        )
    return skips


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        env=env,
        cwd=str(cwd) if cwd else None,
    ).stdout.strip()


def _materialize(source: str, *, ref: str, pinned: str, tmp: Path) -> tuple[Path, str, str]:
    """Return ``(source_root, source_kind, commit)`` for a local path or git source."""

    from clio_agent.gact.git_source import normalize_git_clone_source  # noqa: PLC0415

    local = Path(source).expanduser()
    if local.is_dir():
        try:
            commit = _git(["-C", str(local), "rev-parse", "HEAD"])
        except (OSError, subprocess.SubprocessError):
            commit = ""
        if pinned and commit != pinned:
            raise ValueError(f"registry pin mismatch: expected {pinned}, found {commit!r}")
        return local, "path", commit
    target = tmp / "repo"
    branch = ["--branch", ref] if ref else []
    _git(["clone", "--depth", "1", *branch, normalize_git_clone_source(source), str(target)])
    if pinned:
        _git(["-C", str(target), "fetch", "--depth", "1", "origin", pinned])
        _git(["-C", str(target), "checkout", "--detach", pinned])
    commit = _git(["-C", str(target), "rev-parse", "HEAD"])
    if pinned and commit != pinned:
        raise ValueError(f"registry pin mismatch: expected {pinned}, found {commit}")
    return target, "git", commit


def replace_pack_atomically(
    candidate: Path, install_root: Path, pack_id: str, metadata: dict[str, Any]
) -> None:
    """Install ``candidate`` as ``install_root/pack_id`` with an all-or-nothing swap.

    The new tree (with its install metadata) is built in a staging directory
    beside the install root; then the old pack moves to a backup, the new one
    moves into place, and the backup is deleted. If any step fails, the backup
    is restored and the error re-raised.
    """

    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
        _tree_checksum,
        _write_install_metadata,
    )

    staging_root = install_root.parent / STAGING_DIR_NAME
    staging_root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged = staging_root / f"{pack_id}.new-{token}"
    backup = staging_root / f"{pack_id}.old-{token}"
    final = install_root / pack_id
    try:
        shutil.copytree(candidate, staged)
        _write_install_metadata(staged, {**metadata, "checksum": _tree_checksum(staged)})
        if final.exists():
            final.rename(backup)
        try:
            staged.rename(final)
        except BaseException:
            if backup.exists() and not final.exists():
                backup.rename(final)
            raise
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            logger.warning("default_registry_backup_not_removed path=%s error=%r", backup, exc)


def _resync(
    source: str, install_root: Path, *, ref: str, pinned: str, home: Path, cwd: Path
) -> dict[str, Any]:
    """Re-install every registry pack not in the skip set; returns the outcome summary."""

    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
        _install_candidates,
        parse_agent_blueprint_root,
    )

    skips = _skip_ids(install_root, source, ref=ref, pinned=pinned, home=home, cwd=cwd)
    installed: list[str] = []
    skipped = dict(skips)
    with tempfile.TemporaryDirectory(prefix="clio-registry-resync-") as tmp:
        root, kind, commit = _materialize(source, ref=ref, pinned=pinned, tmp=Path(tmp))
        for candidate in _install_candidates(root):
            parsed = parse_agent_blueprint_root(candidate, scope="install")
            if parsed.id in skips:
                continue
            if not parsed.enabled:
                skipped[parsed.id] = "invalid"
                continue
            replace_pack_atomically(
                candidate,
                install_root,
                parsed.id,
                {
                    "source": source,
                    "source_kind": kind,
                    "ref": ref,
                    "commit": commit,
                    "pinned_commit": pinned,
                    "installed_at": datetime.now(UTC).isoformat(),
                    "scope": "global",
                },
            )
            installed.append(parsed.id)
    return {"installed": installed, "skipped": skipped}


def migrate_default_registry_on_version_change(
    *,
    source: str,
    home: Path,
    cwd: Path,
    ref: str,
    pinned: str,
    now: Callable[[], float] = time.time,
    on_changed: Callable[[], None] | None = None,
) -> str:
    """Re-sync unedited default-registry packs once per clio-agent version change.

    Never raises: every failure (including the skip scan, checksum reads, the
    state write, and reason recording) is the typed
    ``default_registry_migration_failed`` reason, and the caller proceeds with
    what is installed. The once-per-process gate is set regardless.

    Args:
        source: The default registry install source (git URL or local path).
        home: The home directory the install root derives from.
        cwd: The working directory the install root derives from.
        ref: The registry ref to clone.
        pinned: The pinned registry commit (``""`` when unpinned).
        now: Clock for the failure backoff.
        on_changed: Called after packs were replaced (e.g. to invalidate an
            app's cached blueprint discovery).

    Returns:
        ``""`` when nothing had to happen or the re-sync succeeded, otherwise a
        diagnostic.
    """

    with _MIGRATION_LOCK:
        gate_key = f"{home}|{cwd}"
        if gate_key in _MIGRATION_CHECKED_FOR:
            return ""
        try:
            return _migrate_guarded(
                source=source,
                home=home,
                cwd=cwd,
                ref=ref,
                pinned=pinned,
                now=now,
                on_changed=on_changed,
            )
        finally:
            _MIGRATION_CHECKED_FOR.add(gate_key)


def _migrate_guarded(
    *,
    source: str,
    home: Path,
    cwd: Path,
    ref: str,
    pinned: str,
    now: Callable[[], float],
    on_changed: Callable[[], None] | None,
) -> str:
    version = "?"
    install_root: Path | None = None
    try:
        from clio_agent.gact.agent_blueprints import _install_root  # noqa: PLC0415

        install_root = _install_root(home=home, cwd=cwd, scope="global")
        version = running_clio_agent_version()
        state = _read_state(install_root)
        if not migration_due(state, version, now()):
            return ""
        with registry_install_lock(install_root) as held:
            if not held:
                _record("default_registry_migration_busy", version=version, source=source)
                return "default registry re-sync skipped: another CLIO process holds the lock"
            result = _resync(source, install_root, ref=ref, pinned=pinned, home=home, cwd=cwd)
            record_sync_version(install_root, version)
        _record(
            "default_registry_migrated",
            previous_version=str(state.get("clio_agent_version") or ""),
            version=version,
            source=source,
            **result,
        )
        if on_changed is not None and result["installed"]:
            on_changed()
        return ""
    except Exception as exc:  # noqa: BLE001 - typed, backed off, never escapes discovery
        attempts = 0
        with contextlib.suppress(Exception):
            if install_root is not None:
                state = _read_state(install_root)
                same = state.get("failure_version") == version
                attempts = (int(state.get("attempts") or 0) if same else 0) + 1
                _write_state(
                    install_root,
                    {
                        **{k: v for k, v in state.items() if k == "clio_agent_version"},
                        "failure_version": version,
                        "attempts": attempts,
                        "last_failure_at": now(),
                    },
                )
        _record(
            "default_registry_migration_failed",
            version=version,
            source=source,
            attempts=attempts,
            error=repr(exc),
        )
        return f"default registry re-sync for clio-agent {version} failed: {exc}"


def run_default_registry_migration(
    *, home: Path | None = None, cwd: Path | None = None, app: Any | None = None
) -> str:
    """Run the re-sync against the configured default registry (guarded)."""

    try:
        from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
            DEFAULT_REGISTRY_COMMIT,
            DEFAULT_REGISTRY_REF,
            default_registry_install_source,
        )

        source = default_registry_install_source()
    except Exception as exc:  # noqa: BLE001 - typed, never escapes
        _record("default_registry_migration_failed", stage="source", error=repr(exc))
        return f"default registry re-sync failed: {exc}"

    def _invalidate() -> None:
        registry = getattr(getattr(app, "state", None), "a2ui_catalogs", None)
        if registry is not None:
            registry.invalidate()

    return migrate_default_registry_on_version_change(
        source=source,
        home=home or Path.home(),
        cwd=cwd or Path.cwd(),
        ref=DEFAULT_REGISTRY_REF,
        pinned=DEFAULT_REGISTRY_COMMIT.strip(),
        on_changed=_invalidate,
    )


def start_in_background(app: Any | None = None) -> threading.Thread | None:
    """Start the re-sync on a daemon thread (server startup; never the request path).

    Does nothing when the default-registry bootstrap is disabled
    (``agents.disable_default_registry_bootstrap``), the same knob that turns
    off the first-run install.
    """

    from clio_agent import conf  # noqa: PLC0415

    if conf.resolve(
        "agents.disable_default_registry_bootstrap",
        env="CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP",
        default=False,
        cast=conf.as_bool,
    ):
        return None
    thread = threading.Thread(
        target=run_default_registry_migration,
        kwargs={"app": app},
        name="clio-default-registry-resync",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "BACKOFF_SECONDS",
    "LOCK_NAME",
    "lock_busy_diagnostic",
    "SYNC_MARKER_NAME",
    "migrate_default_registry_on_version_change",
    "migration_due",
    "pack_locally_edited",
    "record_first_run_version",
    "record_sync_version",
    "recorded_sync_version",
    "registry_install_lock",
    "replace_pack_atomically",
    "reset_default_registry_migration_for_tests",
    "run_default_registry_migration",
    "running_clio_agent_version",
    "start_in_background",
    "sync_marker_path",
]
