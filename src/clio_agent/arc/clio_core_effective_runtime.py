"""Where this machine's clio-core daemon is, as the runtime itself would find it (read-only).

The doctor (:meth:`clio_agent.runtime.status.RuntimeProbe._probe_clio_core_runtime`) has
to report on the SAME daemon the ARC store attaches to. It used to learn the port only
from ``$CLIO_ARC_STORE_CONFIG`` and look for the pidfile and log under ``~/.clio``, so it
missed:

- a ``cte.yaml`` chosen in the config file (``arc.store_config``), a per-workspace
  ``.clio/core/cte.yaml``, or the per-install default config (whose port is
  ``arc.core_port``, not 9413);
- the running daemon's own record (first config wins, :mod:`clio_core_daemon_version`):
  a daemon spawned by another CLIO with a different port is the one every later
  attacher uses;
- the per-host state directory (``~/.clio/hosts/<host>``, or
  ``$CLIO_RUNTIME_STATE_DIR``) where the pidfile, log and record actually live.

:func:`resolve_effective_runtime` follows the store's own order
(:func:`clio_agent.arc.storage.make_arc_store` then
:func:`clio_agent.arc.storage._ensure_runtime`) without side effects: it never seeds the
default config, creates the state directory, or migrates legacy state.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from clio_agent import conf
from clio_agent.arc import clio_core_config, clio_core_liveness
from clio_agent.arc.clio_core_daemon_version import read_daemon_version

#: The config's source, as reported in the doctor's ``config_source`` detail.
SOURCE_DAEMON_RECORD = "daemon_record"
SOURCE_STORE_CONFIG = "arc.store_config"
SOURCE_WORKSPACE = "workspace"
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class EffectiveClioCoreRuntime:
    """The daemon this process would attach to.

    Attributes:
        state_dir: The per-host runtime state directory (pidfile, log, version record).
        config_path: The ``cte.yaml`` whose port identifies the daemon.
        config_source: Where ``config_path`` came from (one of the ``SOURCE_*`` codes).
        port: The daemon's RPC port.
    """

    state_dir: Path
    config_path: str
    config_source: str
    port: int


def runtime_state_path(env: Mapping[str, str] | None = None) -> Path:
    """Return :func:`clio_core_config.runtime_state_dir`'s path without creating it."""
    source = os.environ if env is None else env
    override = (source.get("CLIO_RUNTIME_STATE_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    from clio_agent import paths  # noqa: PLC0415

    current = paths.host_state_dir(source) / "core-hosts" / clio_core_config.host_key()
    legacy = Path.home() / ".clio" / "hosts" / clio_core_config.host_key()
    return legacy if legacy.exists() and not current.exists() else current


def requested_config(store: conf.ConfigStore) -> tuple[str, str]:
    """Return ``(config_path, source)`` this process would spawn the daemon with.

    ``arc.store_config`` (config file, then ``$CLIO_ARC_STORE_CONFIG``), else the
    per-workspace ``.clio/core/cte.yaml``, else the default config's path (not seeded).
    """
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle

    explicit = store.resolve(
        "arc.store_config", env="CLIO_ARC_STORE_CONFIG", default="", cast=conf.as_str
    ).strip()
    if explicit:
        return str(Path(explicit).expanduser()), SOURCE_STORE_CONFIG
    workspace = paths.workspace_core_dir() / "cte.yaml"
    if workspace.is_file():
        return str(workspace), SOURCE_WORKSPACE
    return str(clio_core_config._default_cte_dir() / "cte.yaml"), SOURCE_DEFAULT


def resolve_effective_runtime(
    store: conf.ConfigStore,
    *,
    env: Mapping[str, str] | None = None,
    state_dir: Path | None = None,
) -> EffectiveClioCoreRuntime:
    """Resolve the daemon's state directory, config and port, as the store would.

    The daemon's recorded config wins when its file exists (first config wins); else the
    config this process would request. The port is the one that config declares; an
    unseeded default config means the per-install default port it would be seeded with.

    Args:
        store: The config store to read ``arc.*`` settings from.
        env: Environment mapping for ``$CLIO_RUNTIME_STATE_DIR`` (default ``os.environ``).
        state_dir: An explicit runtime state directory (overrides the resolution).

    Returns:
        The effective runtime location.
    """
    state = state_dir if state_dir is not None else runtime_state_path(env)
    config_path, source = requested_config(store)
    record = read_daemon_version(state)
    if record is not None and record.config_path and Path(record.config_path).is_file():
        config_path, source = record.config_path, SOURCE_DAEMON_RECORD
    declared = clio_core_liveness._read_yaml_port(config_path)
    if declared is not None:
        port = declared
    elif source == SOURCE_DEFAULT:
        port = clio_core_config._default_cte_core_port()
    else:
        port = clio_core_liveness._resolve_runtime_port(config_path)
    return EffectiveClioCoreRuntime(state, config_path, source, port)


def read_daemon_pid(state_dir: Path) -> tuple[int | None, bool | None]:
    """Return ``(pid, alive)`` from the daemon pidfile in ``state_dir``.

    ``(None, None)`` when there is no readable pidfile. ``alive`` checks the recorded
    process create time too, so a reused pid does not count as the daemon.
    """
    from clio_agent.arc.pid_identity import pid_alive  # noqa: PLC0415 - keep import light

    try:
        parts = (state_dir / "clio-runtime.pid").read_text("utf-8").split()
        pid = int(parts[0])
    except (OSError, ValueError, IndexError):
        return None, None
    recorded: float | None = None
    if len(parts) > 1:
        with contextlib.suppress(ValueError):
            recorded = float(parts[1])
    return pid, pid_alive(pid, recorded)


def arc_backend(store: conf.ConfigStore, env: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(backend, source)``: the ARC backend the store would build, and why.

    Same order as :func:`clio_agent.arc.storage.make_arc_store`: ``arc.store`` in the
    config file, then ``$CLIO_ARC_STORE``, else ``cte``.
    """
    file_value = store.file_value("arc.store")
    if isinstance(file_value, str) and file_value.strip():
        return file_value.strip().lower(), "config:arc.store"
    env_value = (env.get("CLIO_ARC_STORE") or "").strip()
    if env_value:
        return env_value.lower(), "env:CLIO_ARC_STORE"
    return "cte", "default:cte"
