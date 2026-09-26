"""clio-core daemon/client compatibility: the version gate + first-config-wins adoption.

There is ONE clio-core daemon per machine, shared by every CLIO on it (owner ruling),
and its bookkeeping lives in :func:`clio_agent.arc.clio_core_config.runtime_state_dir`.
The process that SPAWNS the daemon records, next to ``clio-runtime.pid``, the
iowarp-core version it runs and the ``cte.yaml`` it launched the daemon with
(:func:`record_daemon_version`). Every process that ATTACHES to a daemon it did not
spawn reads that record first (:func:`resolve_effective_config`).

VERSION GATE. A client of a different iowarp-core build does not fail cleanly against
the daemon. Live evidence (controlled A/B, 2026-09-24): same-version clients share one
daemon with zero errors, but a 2.1.0 client against a 2.2.1 daemon leaves the daemon
looping ``read_binary`` / ``container not found``, and a 2.2.1 client against a 2.1.0
daemon fails ``ClientInit`` and then KILLS the daemon on its first message
(``RecvMetadata: Deserialization failed``), dropping every other client's ARC. So a
mismatch is refused BEFORE ``client_init`` with the typed reason
``clio_core_version_mismatch`` naming both versions and the fix. A daemon with no
record (spawned by an older CLIO) is refused the same way
(``clio_core_daemon_version_unknown``): attaching blind is what crashes daemons.

FIRST CONFIG WINS. Two CLIOs can generate different ``cte.yaml`` files (capacity,
tiers, storage paths) before either attaches, but only one daemon runs. iowarp-core
2.x has no call to grow a running pool (``clio_cte_core_ext`` exposes blob/tag/search
plus ``RegisterTarget``, which adds a NEW target and never resizes an existing one), so
the daemon's recorded config is the effective one for every later attacher. When it
differs from the one this process asked for, the difference is recorded
(:func:`config_adoption_snapshot`), logged with the typed reason
``clio_core_config_adopted_from_daemon``, and surfaced as the
``clio_core_config_adoption`` health row -- never substituted silently.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RECORD_FILENAME = "clio-runtime.version"

#: The daemon's recorded iowarp-core version differs from this process's own.
CLIO_CORE_VERSION_MISMATCH = "clio_core_version_mismatch"
#: The daemon has no version record (spawned by an older CLIO): refused, fail closed.
CLIO_CORE_DAEMON_VERSION_UNKNOWN = "clio_core_daemon_version_unknown"
#: The daemon's recorded config file is missing or unreadable: refused, fail closed.
CLIO_CORE_DAEMON_CONFIG_UNKNOWN = "clio_core_daemon_config_unknown"
#: This process's requested config differed from the daemon's; the daemon's won.
CLIO_CORE_CONFIG_ADOPTED_FROM_DAEMON = "clio_core_config_adopted_from_daemon"

_RESTART_FIX = (
    "Stop every CLIO on this machine so the shared clio-core daemon exits (the last "
    "client out stops it), then start this CLIO again"
)


class ClioCoreDaemonIncompatibleError(RuntimeError):
    """This process must not attach to the running clio-core daemon.

    Carries ``degradation_reason`` (one of the ``CLIO_CORE_*`` refusal codes above) so
    :func:`clio_agent.arc.init_degradation.classify_init_failure` records it as the
    typed init-degrade reason, and ``details`` naming the facts behind the refusal.
    """

    def __init__(self, message: str, *, reason: str, details: Mapping[str, str]) -> None:
        super().__init__(message)
        self.degradation_reason = reason
        self.details = dict(details)


@dataclass(frozen=True)
class DaemonVersionRecord:
    """Facts about the running daemon, written by the process that spawned it."""

    iowarp_core_version: str
    daemon_binary: str
    config_path: str


def _record_file(state_dir: Path) -> Path:
    return state_dir / _RECORD_FILENAME


def installed_iowarp_core_version() -> str:
    """Return this process's installed ``iowarp-core`` version (``""`` if unknown)."""
    try:
        return importlib.metadata.version("iowarp-core")
    except importlib.metadata.PackageNotFoundError:
        return ""


def record_daemon_version(state_dir: Path, *, daemon_binary: str, config_path: str) -> None:
    """Record the spawned daemon's iowarp-core version and config next to its pidfile.

    Called only from the spawn path: the ``clio_run`` binary ships in this process's
    own iowarp-core package, so this process's version IS the daemon's. Rewritten on
    every spawn, so a restarted daemon never carries a stale record.

    Args:
        state_dir: ``runtime_state_dir()``.
        daemon_binary: The launched ``clio_run`` path.
        config_path: The ``cte.yaml`` the daemon was launched with.
    """
    payload = {
        "iowarp_core_version": installed_iowarp_core_version(),
        "daemon_binary": daemon_binary,
        "config_path": config_path,
    }
    _record_file(state_dir).write_text(json.dumps(payload), encoding="utf-8")


def read_daemon_version(state_dir: Path) -> DaemonVersionRecord | None:
    """Return the daemon's record, or ``None`` when absent, unreadable or malformed."""
    try:
        data = json.loads(_record_file(state_dir).read_text(encoding="utf-8"))
        return DaemonVersionRecord(
            iowarp_core_version=str(data["iowarp_core_version"]),
            daemon_binary=str(data["daemon_binary"]),
            config_path=str(data.get("config_path", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def running_daemon_config(state_dir: Path, requested_config_path: str) -> str:
    """Return the config whose port identifies this machine's daemon.

    The recorded daemon's config when its file still exists, so an attacher whose own
    config declares a different port still finds the ONE running daemon instead of
    spawning a second; else ``requested_config_path``.
    """
    record = read_daemon_version(state_dir)
    if record is not None and record.config_path and Path(record.config_path).is_file():
        return record.config_path
    return requested_config_path


def _refuse(
    message: str,
    *,
    reason: str,
    details: Mapping[str, str],
    on_failure: Callable[[], None] | None,
) -> ClioCoreDaemonIncompatibleError:
    logger.error("%s reason=%s details=%s", message, reason, dict(details))
    if on_failure is not None:
        on_failure()
    return ClioCoreDaemonIncompatibleError(message, reason=reason, details=details)


def refuse_if_incompatible_daemon(
    state_dir: Path,
    *,
    client_version: str | None = None,
    on_failure: Callable[[], None] | None = None,
) -> DaemonVersionRecord:
    """Refuse to attach unless the running daemon runs this process's iowarp-core version.

    Args:
        state_dir: ``runtime_state_dir()``, where the daemon's record lives.
        client_version: This process's iowarp-core version (defaults to the installed one).
        on_failure: Run before raising (the caller's client deregistration, so a refused
            process holds no vote in the daemon's last-one-out refcount).

    Returns:
        The daemon's record (versions match).

    Raises:
        ClioCoreDaemonIncompatibleError: ``clio_core_version_mismatch`` naming both
            versions, or ``clio_core_daemon_version_unknown`` when no record exists.
    """
    client = installed_iowarp_core_version() if client_version is None else client_version
    record = read_daemon_version(state_dir)
    if record is None:
        raise _refuse(
            f"Refusing to attach to the running clio-core daemon: it has no version record "
            f"at {_record_file(state_dir)} (an older CLIO started it), and this CLIO uses "
            f"iowarp-core {client or '<unknown>'}. Attaching across versions can crash the "
            f"daemon. {_RESTART_FIX}.",
            reason=CLIO_CORE_DAEMON_VERSION_UNKNOWN,
            details={"client_version": client, "record_path": str(_record_file(state_dir))},
            on_failure=on_failure,
        )
    if record.iowarp_core_version != client:
        raise _refuse(
            f"Refusing to attach to the running clio-core daemon: it runs iowarp-core "
            f"{record.iowarp_core_version} but this CLIO uses iowarp-core "
            f"{client or '<unknown>'}. Attaching across versions can crash the daemon and "
            f"every CLIO on it. {_RESTART_FIX}, or use a CLIO built on iowarp-core "
            f"{record.iowarp_core_version}.",
            reason=CLIO_CORE_VERSION_MISMATCH,
            details={
                "daemon_version": record.iowarp_core_version,
                "client_version": client,
                "daemon_binary": record.daemon_binary,
            },
            on_failure=on_failure,
        )
    return record


# ---- first config wins -------------------------------------------------------


def _config_fingerprint(path: str) -> dict[str, Any]:
    """Return the comparison-relevant fields of a ``cte.yaml`` (``{}`` if unreadable).

    RAM bdev capacity, the CTE pool identity, and its storage tiers (path, bdev type,
    capacity limit, score): the settings that change what the daemon actually runs.
    """
    import yaml  # noqa: PLC0415

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    fields: dict[str, Any] = {}
    for module in data.get("compose", []) or []:
        if not isinstance(module, Mapping):
            continue
        if str(module.get("bdev_type", "")).strip().lower() == "ram":
            fields["ram_bdev_capacity"] = module.get("capacity")
        if str(module.get("mod_name", "")) == "clio_cte_core":
            fields["cte_pool_id"] = module.get("pool_id")
            fields["cte_pool_name"] = module.get("pool_name")
            fields["cte_storage_tiers"] = [
                {
                    "path": tier.get("path"),
                    "bdev_type": tier.get("bdev_type"),
                    "capacity_limit": tier.get("capacity_limit"),
                    "score": tier.get("score"),
                }
                for tier in module.get("storage", []) or []
                if isinstance(tier, Mapping)
            ]
    return fields


def diff_configs(requested_path: str, effective_path: str) -> dict[str, dict[str, Any]]:
    """Return ``{field: {"requested": ..., "effective": ...}}`` for each differing field."""
    requested = _config_fingerprint(requested_path)
    effective = _config_fingerprint(effective_path)
    return {
        key: {"requested": requested.get(key), "effective": effective.get(key)}
        for key in sorted(set(requested) | set(effective))
        if requested.get(key) != effective.get(key)
    }


@dataclass(frozen=True)
class ConfigAdoption:
    """This process asked for one config; the running daemon's config took effect."""

    requested_config_path: str
    effective_config_path: str
    diffs: dict[str, dict[str, Any]]


_adoption_lock = threading.Lock()
_last_adoption: ConfigAdoption | None = None


def config_adoption_snapshot() -> ConfigAdoption | None:
    """Return the config adoption recorded in this process, or ``None``."""
    with _adoption_lock:
        return _last_adoption


def reset_config_adoption() -> None:
    """Clear the recorded config adoption (test seam; production never resets)."""
    global _last_adoption
    with _adoption_lock:
        _last_adoption = None


def resolve_effective_config(
    state_dir: Path,
    requested_config_path: str,
    *,
    client_version: str | None = None,
    on_failure: Callable[[], None] | None = None,
) -> str:
    """Gate an attach to a daemon this process did not spawn; return the effective config.

    Runs the version gate, then returns the daemon's recorded config (first config
    wins). A difference from ``requested_config_path`` is recorded, logged, and shown
    in health as the ``clio_core_config_adoption`` row.

    Args:
        state_dir: ``runtime_state_dir()``.
        requested_config_path: The config this process would have spawned the daemon with.
        client_version: Forwarded to :func:`refuse_if_incompatible_daemon`.
        on_failure: Run before any refusal raises.

    Returns:
        The daemon's config path, the one the native client must attach with.

    Raises:
        ClioCoreDaemonIncompatibleError: from the version gate, or
            ``clio_core_daemon_config_unknown`` when the recorded config is unreadable.
    """
    global _last_adoption
    record = refuse_if_incompatible_daemon(
        state_dir, client_version=client_version, on_failure=on_failure
    )
    daemon_config = record.config_path
    if not daemon_config or not Path(daemon_config).is_file():
        raise _refuse(
            f"Refusing to attach to the running clio-core daemon: its recorded config "
            f"({daemon_config or 'no path recorded'}) is missing, so this CLIO cannot know "
            f"which ports and tiers it runs. {_RESTART_FIX}.",
            reason=CLIO_CORE_DAEMON_CONFIG_UNKNOWN,
            details={
                "daemon_config_path": daemon_config,
                "requested_config_path": requested_config_path,
            },
            on_failure=on_failure,
        )
    diffs = diff_configs(requested_config_path, daemon_config)
    if diffs:
        with _adoption_lock:
            _last_adoption = ConfigAdoption(requested_config_path, daemon_config, diffs)
        logger.warning(
            "clio-core config adopted from the running daemon (reason=%s): first config "
            "wins on the one daemon per machine; requested=%s effective=%s diffs=%s",
            CLIO_CORE_CONFIG_ADOPTED_FROM_DAEMON,
            requested_config_path,
            daemon_config,
            diffs,
        )
    return daemon_config
