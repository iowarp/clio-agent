"""Which clio-core daemon a CLIO home runs on: the machine's shared one or its own.

A default install (Desktop, which passes ``CLIO_DESKTOP_MANAGED=1``, and the CLI without
``CLIO_AGENT_HOME``, ``CLIO_USER_DIR`` or ``CLIO_AGENT_DATA_DIR``) keeps the one machine
daemon whose bookkeeping lives under the host state dir (``state/core-hosts/<host>``).
An explicit ``CLIO_RUNTIME_STATE_DIR`` still pins the bookkeeping dir as before.
Desktop's pin to the host dir only names the shared daemon, so it does not bypass the
rules below. An explicit home runs its own daemon, on its own store, with bookkeeping
under its own data dir (``core-runtime/<host>``). Before this, every home attached to
whichever daemon the host dir named, so a long-lived home could keep writing into a
throwaway home's store, and deleting that home lost the records.

The decision is made once per process:

* A live daemon in this home's own dir is always used.
* A default home uses the host daemon, as before, unless that daemon is live and
  cannot be attached because its config is unknown (no record, or the recorded config
  is gone with the home that started it): then it moves off to its own daemon and
  never stops the daemon other agents still use.
* An explicit home uses the host daemon only while it is live on this home's own
  store (an older CLIO started it there); otherwise it runs its own.

A home on its own daemon gets its own shared-memory names (clio-core keys them on
``USER``) so two daemons never map each other's segments, and an explicit home's
generated config is sized for running several at once (``arc.cte.home_ram_capacity``,
``arc.cte.home_segment_size``) and kept off the default port.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Typed reason logged once with the selection.
HOME_DAEMON_SELECTED = "clio_core_home_daemon_selected"
#: Typed reason of an explicit home's config resized for a per-home daemon.
HOME_CONFIG_SIZED = "clio_core_home_config_sized"

MODE_HOST = "host"
MODE_HOME = "home"

#: Set only for the import exporter: attach to the running daemon, never spawn or stop.
ATTACH_ONLY_ENV = "CLIO_CORE_ATTACH_ONLY"
#: An explicit shared-memory user for the native client and clio-core children.
CORE_USER_ENV = "CLIO_CORE_USER"
#: Set by Desktop for the Agent it manages (a default install with its own home dir).
DESKTOP_MANAGED_ENV = "CLIO_DESKTOP_MANAGED"
#: Pins the daemon bookkeeping dir (tests, sandboxes, Desktop's host registry).
RUNTIME_STATE_DIR_ENV = "CLIO_RUNTIME_STATE_DIR"

_HOME_ENV_KEYS = ("CLIO_AGENT_HOME", "CLIO_USER_DIR", "CLIO_AGENT_DATA_DIR")
_SIZED_MARKER = "cte.home-sized"
_DEFAULT_HOME_RAM = "128MB"
_DEFAULT_HOME_SEGMENT = "64MB"
_SEEDED_RAM = '    bdev_type: ram\n    capacity: "1GB"\n'
_SEEDED_MAIN = '  main_segment_size: "256MB"\n'
_SEEDED_METADATA = '  metadata_segment_size: "512MB"\n'


@dataclass(frozen=True)
class Selection:
    """The daemon this process uses: ``mode``, its bookkeeping dir and why."""

    mode: str
    state_dir: Path
    reason: str


_lock = threading.Lock()
_selection: Selection | None = None


def _norm(path: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(path))


def host_runtime_dir(*, create: bool = True) -> Path:
    """The machine's shared daemon bookkeeping (Desktop reads it too)."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle
    from clio_agent.arc.clio_core_config import host_key  # noqa: PLC0415 - cycle
    from clio_agent.arc.clio_core_host_migration import (  # noqa: PLC0415 - cycle
        migrate_legacy_runtime_state,
    )

    state = paths.host_state_dir() / "core-hosts" / host_key()
    legacy = Path.home() / ".clio" / "hosts" / host_key()
    # Preserve the pre-host compatibility migration for known Agent bookkeeping.
    # The helper refuses live/shared state and never moves Core-owned files.
    migrate_legacy_runtime_state(Path.home() / ".clio", legacy)
    if legacy.exists() and not state.exists():
        logger.warning("Using legacy Core supervision records; migrate paths after stopping CLIO")
        state = legacy
    if create:
        state.mkdir(parents=True, exist_ok=True)
    return state


def home_runtime_dir(*, create: bool = True) -> Path:
    """This home's own daemon bookkeeping, inside its data dir."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle
    from clio_agent.arc.clio_core_config import host_key  # noqa: PLC0415 - cycle

    state = paths.user_data_dir() / "core-runtime" / host_key()
    if create:
        state.mkdir(parents=True, exist_ok=True)
    return state


def desktop_managed() -> bool:
    """Whether Desktop launched this Agent (its home dir is set, yet it is a default install)."""
    return os.environ.get(DESKTOP_MANAGED_ENV, "").strip() == "1"


def is_default_home() -> bool:
    """Whether this process runs the install's default home (Desktop, or no home override)."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle

    if desktop_managed():
        return True
    plain = {key: value for key, value in os.environ.items() if key not in _HOME_ENV_KEYS}
    return _norm(paths.user_data_dir()) == _norm(paths.user_data_dir(plain))


def pinned_state_dir() -> Path | None:
    """The ``CLIO_RUNTIME_STATE_DIR`` pin, or ``None`` when selection decides.

    Desktop pins the host dir only to name the machine's shared daemon; that pin is left
    to selection so a Desktop boot recovers from an orphaned daemon as the CLI does.
    """
    override = os.environ.get(RUNTIME_STATE_DIR_ENV, "").strip()
    if not override:
        return None
    pinned = Path(override).expanduser()
    if desktop_managed() and not attach_only():
        if _norm(pinned) == _norm(host_runtime_dir(create=False)):
            return None
    return pinned


def _config_known(record: object) -> bool:
    config = str(getattr(record, "config_path", "") or "")
    return bool(config) and Path(config).is_file()


def _live(state: Path) -> bool:
    from clio_agent.arc.clio_core_effective_runtime import read_daemon_pid  # noqa: PLC0415

    return read_daemon_pid(state)[1] is True


def requested_config_path() -> str:
    """The config this home would start its daemon with."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle
    from clio_agent.arc.clio_core_effective_runtime import requested_config  # noqa: PLC0415

    return requested_config(conf.store())[0]


def record_is_mine(record: object) -> bool:
    """Whether a daemon record names this home or this home's store."""
    from clio_agent.arc.home_namespace import home_identity  # noqa: PLC0415 - cycle

    if record is None:
        return False
    home = str(getattr(record, "home", "") or "")
    config = str(getattr(record, "config_path", "") or "")
    if home and os.path.normcase(home) == home_identity():
        return True
    return bool(config) and _norm(config) == _norm(requested_config_path())


def _decide() -> Selection:
    from clio_agent.arc.clio_core_daemon_version import read_daemon_version  # noqa: PLC0415

    pinned = pinned_state_dir()
    if pinned is not None:
        return Selection(MODE_HOST, pinned, "runtime_state_dir_pinned")
    own = home_runtime_dir(create=False)
    if _live(own):
        return Selection(MODE_HOME, own, "home_daemon_running")
    host = host_runtime_dir(create=False)
    host_live = _live(host)
    record = read_daemon_version(host) if host_live else None
    mine = host_live and record_is_mine(record)
    if is_default_home():
        # Share the machine daemon as before; move off only when attaching is refused.
        if host_live and record is None:
            return Selection(MODE_HOME, own, "host_daemon_unrecorded")
        if host_live and not mine and not _config_known(record):
            return Selection(MODE_HOME, own, "host_daemon_config_unknown")
        return Selection(MODE_HOST, host, "default_home")
    if mine:
        return Selection(MODE_HOST, host, "host_daemon_on_this_store")
    return Selection(MODE_HOME, own, "explicit_home")


def selection() -> Selection:
    """This process's daemon selection, decided once and logged with its reason."""
    global _selection
    with _lock:
        if _selection is None:
            _selection = _decide()
            log = logger.warning if _selection.reason.startswith("host_daemon_") else logger.info
            log(
                "clio-core daemon for this home: %s (reason=%s detail=%s state=%s)",
                _selection.mode,
                HOME_DAEMON_SELECTED,
                _selection.reason,
                _selection.state_dir,
            )
        return _selection


def runtime_dir(*, create: bool = True) -> Path:
    """The selected daemon's bookkeeping dir."""
    state = selection().state_dir
    if create:
        state.mkdir(parents=True, exist_ok=True)
    return state


def snapshot() -> dict[str, str]:
    """The selection for health (``{}`` before any decision)."""
    with _lock:
        chosen = _selection
    if chosen is None:
        return {}
    return {"mode": chosen.mode, "detail": chosen.reason, "state_dir": str(chosen.state_dir)}


def reset() -> None:
    """Forget the selection (test seam; production decides once per process)."""
    global _selection
    with _lock:
        _selection = None


def attach_only() -> bool:
    """Whether this process may only attach to a running daemon (the import exporter)."""
    return os.environ.get(ATTACH_ONLY_ENV, "").strip() == "1"


def core_user() -> str | None:
    """The ``USER`` clio-core must see for this home's daemon, or ``None`` to leave it.

    clio-core names its shared-memory segments ``chi_*_segment_${USER}``. A home on its
    own daemon gets a name derived from its bookkeeping dir; the shared host daemon
    keeps the plain one.
    """
    explicit = os.environ.get(CORE_USER_ENV, "").strip()
    if explicit:
        return explicit
    if attach_only() or pinned_state_dir() is not None:
        return None
    chosen = selection()
    if chosen.mode != MODE_HOME:
        return None
    base = os.environ.get("USERNAME") or os.environ.get("USER") or "clio"
    digest = hashlib.sha256(_norm(chosen.state_dir).encode("utf-8")).hexdigest()[:16]
    return f"{base}-clio-{digest}"


@contextlib.contextmanager
def core_user_pinned() -> Iterator[None]:
    """Export :func:`core_user` while the native client initializes, then restore."""
    user = core_user()
    if user is None:
        yield
        return
    previous = os.environ.get("USER")
    os.environ["USER"] = user
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("USER", None)
        else:
            os.environ["USER"] = previous


def _home_size(key: str, env: str, default: str) -> str:
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle
    from clio_agent.arc.clio_core_config import parse_capacity_bytes  # noqa: PLC0415

    value = conf.resolve(key, env=env, default=default, cast=conf.as_str).strip() or default
    parse_capacity_bytes(value)
    return value


def _size_home_config(config: Path) -> None:
    """Size a generated config for a per-home daemon, once (a later edit is kept)."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle
    from clio_agent.arc.clio_core_growth import rekey_managed_capacity  # noqa: PLC0415

    marker = config.with_name(_SIZED_MARKER)
    if marker.exists():
        return
    before = config.read_text(encoding="utf-8")
    after = before
    segment = _home_size(
        "arc.cte.home_segment_size", "CLIO_ARC_CTE_HOME_SEGMENT_SIZE", _DEFAULT_HOME_SEGMENT
    )
    after = after.replace(_SEEDED_MAIN, f'  main_segment_size: "{segment}"\n', 1)
    after = after.replace(_SEEDED_METADATA, f'  metadata_segment_size: "{segment}"\n', 1)
    ram_source = conf.store().source("arc.cte.ram_capacity", env="CLIO_ARC_CTE_RAM_CAPACITY")
    if ram_source == "default":
        ram = _home_size(
            "arc.cte.home_ram_capacity", "CLIO_ARC_CTE_HOME_RAM_CAPACITY", _DEFAULT_HOME_RAM
        )
        after = after.replace(_SEEDED_RAM, f'    bdev_type: ram\n    capacity: "{ram}"\n', 1)
    if after != before:
        config.write_text(after, encoding="utf-8")
        rekey_managed_capacity(config, before, after)
        logger.info(
            "clio-core config %s sized for this home's daemon (reason=%s)",
            config,
            HOME_CONFIG_SIZED,
        )
    marker.write_text("1\n", encoding="utf-8")


def _move_off_default_port(config: Path, state: Path) -> None:
    """Keep an explicit home's daemon off the default port the shared daemon uses."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle
    from clio_agent.arc import clio_core_config, runtime_ownership  # noqa: PLC0415 - cycle
    from clio_agent.arc.clio_core_liveness import _read_yaml_port  # noqa: PLC0415 - cycle

    if conf.store().source("arc.core_port", env="CLIO_CORE_PORT") != "default":
        return
    if _read_yaml_port(str(config)) != clio_core_config._DEFAULT_CTE_CORE_PORT:
        return
    for base in runtime_ownership.private_port_candidates(state):
        if runtime_ownership._block_free(base):
            runtime_ownership._write_port(config, base)
            logger.info("clio-core config %s uses port %d for this home's daemon", config, base)
            return


class AttachOnlyUnavailableError(RuntimeError):
    """The import exporter found no running daemon to attach to."""

    reason = "clio_core_attach_only_unavailable"


def attach_only_daemon(config_path: str) -> str:
    """Register as a client of the running daemon without spawning or replacing it.

    Called under the spawn lock. Registering keeps a concurrent last-one-out release
    from stopping the daemon while the exporter reads it; release never stops it.
    """
    from clio_agent.arc import storage  # noqa: PLC0415 - cycle
    from clio_agent.arc.clio_core_liveness import _runtime_alive  # noqa: PLC0415 - cycle

    storage._register_client()
    if not _runtime_alive(storage._resolve_runtime_port(config_path)):
        storage._deregister_client()
        raise AttachOnlyUnavailableError(
            f"no clio-core daemon is running for {config_path} "
            f"(reason={AttachOnlyUnavailableError.reason})"
        )
    return config_path


def prepare_attach(config_path: str) -> None:
    """Get this home ready to attach: import pending records, then size its own config.

    Runs before connect-or-spawn. The import of records this home left in another
    home's store runs first, while that store is still where the record says it is.
    """
    from clio_agent.arc import home_import, runtime_ownership  # noqa: PLC0415 - cycle

    if attach_only() or os.environ.get(home_import.EXPORTER_ENV):
        return
    home_import.export_if_pending()
    chosen = selection()
    if chosen.mode != MODE_HOME or is_default_home() or _live(chosen.state_dir):
        return
    if not runtime_ownership.generated_config(config_path):
        return
    config = Path(config_path)
    _size_home_config(config)
    _move_off_default_port(config, chosen.state_dir)


__all__ = [
    "ATTACH_ONLY_ENV",
    "CORE_USER_ENV",
    "DESKTOP_MANAGED_ENV",
    "RUNTIME_STATE_DIR_ENV",
    "AttachOnlyUnavailableError",
    "attach_only_daemon",
    "HOME_CONFIG_SIZED",
    "HOME_DAEMON_SELECTED",
    "MODE_HOME",
    "MODE_HOST",
    "Selection",
    "attach_only",
    "core_user",
    "core_user_pinned",
    "desktop_managed",
    "home_runtime_dir",
    "host_runtime_dir",
    "is_default_home",
    "pinned_state_dir",
    "prepare_attach",
    "record_is_mine",
    "requested_config_path",
    "reset",
    "runtime_dir",
    "selection",
    "snapshot",
]
