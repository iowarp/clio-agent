"""One-time move of clio-core state from the old host-global places to per-host ones.

clio-core's state is keyed by host (:func:`clio_agent.arc.clio_core_config.host_key`):
the daemon bookkeeping lives in ``~/.clio/hosts/<host>`` and the default CTE store
in ``<data>/cte/hosts/<host>``. Earlier versions kept both directly in ``~/.clio``
and ``<data>/cte``. Without a migration an upgraded machine would start a new,
empty store and leave the old one (often a sparse 1 GB file tier) behind.

On clio-core start each old location is moved once into this host's directory,
by rename on the same filesystem (never a copy), when all of these hold:

* this host's directory does not exist yet (otherwise it is already migrated:
  a no-op);
* the old state exists;
* no other host's directory exists beside it. On a home shared by several
  machines the old state may belong to another host, so it is left alone
  (``*_migration_skipped_shared``);
* no daemon is using it: the old pidfile names no live process, no old client
  is live, and (for the store) nothing listens on the old config's port
  (``*_migration_skipped_in_use``).

A move renames each entry; if one rename fails, the entries already moved are
put back and the failure is logged (``*_migration_failed``). Every outcome other
than the no-op is logged with its typed reason. The moved store's own records of
its location are then pointed at the new directory by
:func:`clio_agent.arc.clio_core_relocate.relocate_store`: ``cte.yaml`` (the file
tier, metadata log, index log and conf dir, its only change), clio-core's pool log
and file bdev model, and the capacity journal's digest of ``cte.yaml`` (left naming
the old text, managed growth would read the config as a person's edit). A pool log
of an unknown format is left as it is (``cte_pool_log_not_relocated``): clio-core
takes every pool the config composes from the config.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CTE_STORE_MIGRATED = "cte_store_migrated_to_host_dir"
CTE_STORE_MIGRATION_SKIPPED_SHARED = "cte_store_migration_skipped_shared"
CTE_STORE_MIGRATION_SKIPPED_IN_USE = "cte_store_migration_skipped_in_use"
CTE_STORE_MIGRATION_FAILED = "cte_store_migration_failed"
CTE_STORE_MIGRATION_RELOCATION_INCOMPLETE = "cte_store_migration_relocation_incomplete"
RUNTIME_STATE_MIGRATED = "runtime_state_migrated_to_host_dir"
RUNTIME_STATE_MIGRATION_SKIPPED_SHARED = "runtime_state_migration_skipped_shared"
RUNTIME_STATE_MIGRATION_SKIPPED_IN_USE = "runtime_state_migration_skipped_in_use"
RUNTIME_STATE_MIGRATION_FAILED = "runtime_state_migration_failed"

# The daemon bookkeeping entries the old layout kept directly in ~/.clio.
_RUNTIME_ENTRIES = (
    "clio-runtime.pid",
    "clio-runtime.clients",
    "clio-runtime.lock",
    "clio-runtime.log",
    "clio-runtime-crash.json",
)


def migrate_legacy_cte_store(legacy_dir: Path, host_dir: Path, *, runtime_root: Path) -> str | None:
    """Move the old ``<data>/cte`` store into ``<data>/cte/hosts/<host>`` once.

    Args:
        legacy_dir: The old store directory (``<data>/cte``).
        host_dir: This host's store directory (``<legacy_dir>/hosts/<host>``).
        runtime_root: The old bookkeeping directory (``~/.clio``), whose pidfile
            and client registry say whether a daemon is using the old store.

    Returns:
        The typed reason logged, or None when there was nothing to do.
    """

    if host_dir.exists() or not (legacy_dir / "cte.yaml").is_file():
        return None
    if _other_hosts(host_dir):
        logger.warning(
            "reason=%s store=%s host_dir=%s (another host's store exists beside it; "
            "the old store may be that host's, so it stays where it is)",
            CTE_STORE_MIGRATION_SKIPPED_SHARED,
            legacy_dir,
            host_dir,
        )
        return CTE_STORE_MIGRATION_SKIPPED_SHARED
    port = _config_port(legacy_dir / "cte.yaml")
    if _runtime_in_use(runtime_root) or (port is not None and _listening(port)):
        logger.warning(
            "reason=%s store=%s port=%s (a clio-core daemon may be using it; it is "
            "moved on a later start, once no daemon runs)",
            CTE_STORE_MIGRATION_SKIPPED_IN_USE,
            legacy_dir,
            port,
        )
        return CTE_STORE_MIGRATION_SKIPPED_IN_USE
    entries = [entry for entry in legacy_dir.iterdir() if entry.name != "hosts"]
    try:
        _move_all(entries, host_dir)
    except OSError as exc:
        logger.warning(
            "reason=%s store=%s host_dir=%s error=%s (the old store was left in place)",
            CTE_STORE_MIGRATION_FAILED,
            legacy_dir,
            host_dir,
            exc,
        )
        return CTE_STORE_MIGRATION_FAILED
    _relocate_store(legacy_dir, host_dir)
    logger.warning(
        "reason=%s from=%s to=%s entries=%d",
        CTE_STORE_MIGRATED,
        legacy_dir,
        host_dir,
        len(entries),
    )
    return CTE_STORE_MIGRATED


def migrate_legacy_runtime_state(legacy_root: Path, host_dir: Path) -> str | None:
    """Move the old ``~/.clio`` daemon bookkeeping into ``~/.clio/hosts/<host>`` once.

    Only the bookkeeping entries move (pidfile, client registry, lock, daemon log,
    crash record); clio-core's own files in ``~/.clio`` are not CLIO's to move.

    Returns:
        The typed reason logged, or None when there was nothing to do.
    """

    entries = [legacy_root / name for name in _RUNTIME_ENTRIES if (legacy_root / name).exists()]
    if not entries or _already_populated(host_dir):
        return None
    if _other_hosts(host_dir):
        logger.warning(
            "reason=%s state=%s host_dir=%s (another host's state exists beside it)",
            RUNTIME_STATE_MIGRATION_SKIPPED_SHARED,
            legacy_root,
            host_dir,
        )
        return RUNTIME_STATE_MIGRATION_SKIPPED_SHARED
    if _runtime_in_use(legacy_root):
        logger.warning(
            "reason=%s state=%s (a clio-core daemon or client recorded there is still live)",
            RUNTIME_STATE_MIGRATION_SKIPPED_IN_USE,
            legacy_root,
        )
        return RUNTIME_STATE_MIGRATION_SKIPPED_IN_USE
    try:
        _move_all(entries, host_dir)
    except OSError as exc:
        logger.warning(
            "reason=%s state=%s host_dir=%s error=%s (left in place)",
            RUNTIME_STATE_MIGRATION_FAILED,
            legacy_root,
            host_dir,
            exc,
        )
        return RUNTIME_STATE_MIGRATION_FAILED
    logger.warning(
        "reason=%s from=%s to=%s entries=%s",
        RUNTIME_STATE_MIGRATED,
        legacy_root,
        host_dir,
        ",".join(entry.name for entry in entries),
    )
    return RUNTIME_STATE_MIGRATED


def _other_hosts(host_dir: Path) -> list[str]:
    hosts = host_dir.parent
    if not hosts.is_dir():
        return []
    return sorted(
        entry.name for entry in hosts.iterdir() if entry.is_dir() and entry.name != host_dir.name
    )


def _already_populated(host_dir: Path) -> bool:
    return host_dir.is_dir() and any(host_dir.iterdir())


def _move_all(entries: list[Path], destination: Path) -> None:
    """Rename every entry into ``destination``; on a failure put the moved ones back."""

    destination.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    try:
        for entry in entries:
            target = destination / entry.name
            entry.rename(target)
            moved.append((target, entry))
    except OSError:
        for target, original in reversed(moved):
            try:
                target.rename(original)
            except OSError:
                logger.error(
                    "could not put %s back at %s during a failed migration", target, original
                )
        try:
            destination.rmdir()
        except OSError:
            pass
        raise


def _relocate_store(legacy_dir: Path, host_dir: Path) -> None:
    """Point the moved store's records of its location at ``host_dir``.

    Nothing uses the store (checked before the move). A failure is logged, typed; the
    start that follows (:func:`clio_agent.arc.clio_core_relocate.ensure_store_location`)
    finishes the relocation, ``cte.yaml`` being rewritten last.
    """
    from clio_agent.arc import clio_core_relocate as relocate  # noqa: PLC0415 - cycle

    old = legacy_dir.as_posix().rstrip("/")
    try:
        try:
            relocate.relocate_store(host_dir, old)
        except relocate.WalFormatError as exc:
            logger.warning(
                "reason=%s store=%s error=%s (the pool log is left as it is; clio-core "
                "takes every pool cte.yaml composes from cte.yaml)",
                relocate.CTE_POOL_LOG_NOT_RELOCATED,
                host_dir,
                exc,
            )
            relocate.relocate_store(host_dir, old, rewrite_pool_log=False)
        _relocate_config(host_dir / "cte.yaml", legacy_dir, host_dir)
    except (OSError, ValueError) as exc:
        logger.warning(
            "reason=%s store=%s error=%s (moved; its location records are finished at "
            "this start, before clio-core starts)",
            CTE_STORE_MIGRATION_RELOCATION_INCOMPLETE,
            host_dir,
            exc,
        )


def _relocate_config(config: Path, legacy_dir: Path, host_dir: Path) -> None:
    """Point the config's native-spelled paths (Windows separators) at the new directory.

    The POSIX spelling CLIO writes is relocated with the rest of the store; a person may
    have written the native one. The capacity journal follows the config.
    """

    native = str(legacy_dir).rstrip("\\/") + os.sep
    if native == legacy_dir.as_posix().rstrip("/") + "/":
        return  # POSIX: one spelling, already relocated (a second pass would nest it)
    text = config.read_text(encoding="utf-8")
    # One pass: the new directory lies inside the old one, and a replaced path is not
    # scanned again.
    relocated = text.replace(native, str(host_dir).rstrip("\\/") + os.sep)
    if relocated != text:
        from clio_agent.arc.clio_core_growth import rekey_managed_capacity  # noqa: PLC0415

        config.write_text(relocated, encoding="utf-8")
        rekey_managed_capacity(config, text, relocated)


def _config_port(config: Path) -> int | None:
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    networking = data.get("networking") if isinstance(data, dict) else None
    port = networking.get("port") if isinstance(networking, dict) else None
    return port if isinstance(port, int) else None


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _runtime_in_use(runtime_root: Path) -> bool:
    """True when the old bookkeeping names a live daemon or a live client."""

    pidfile = runtime_root / "clio-runtime.pid"
    try:
        parts = pidfile.read_text(encoding="utf-8").split()
    except OSError:
        parts = []
    if parts and parts[0].isdigit():
        recorded = _float(parts[1]) if len(parts) > 1 else None
        if _pid_alive(int(parts[0]), recorded):
            return True
    clients = runtime_root / "clio-runtime.clients"
    if clients.is_dir():
        for entry in clients.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                recorded = _float(entry.read_text(encoding="utf-8").strip())
            except OSError:
                recorded = None
            if _pid_alive(int(entry.name), recorded):
                return True
    return False


def _float(value: str) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _pid_alive(pid: int, recorded_create_time: float | None) -> bool:
    import psutil  # noqa: PLC0415

    try:
        process = psutil.Process(pid)
        created = process.create_time()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True  # it exists; err on the side of leaving the state alone
    return recorded_create_time is None or abs(created - recorded_create_time) < 1.0
