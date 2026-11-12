"""Keep each CLIO home's sessions apart on the one clio-core daemon per machine.

Every CLIO home on a machine attaches to the same daemon, and the first home to start
it decides its config and its storage (``clio_core_daemon_version.resolve_effective_config``).
Session records were then shared too: one bare ``session_records`` index, so a second
home listed the first home's sessions as its own, and each home's index writes (a whole
bucket from its own memory) dropped the other's entries.

The process that spawns the daemon records its home next to the pidfile. A process
from another home that attaches still adopts the daemon's config (ports and tiers must
match to attach at all), but keeps its session index and rows under its own prefix,
``home-<digest>/``, inside the ``session_records`` kind. The daemon's own home keeps the
bare names it always had, so a single-home install sees no change. Other record kinds
are keyed by session id and stay where they are, so a session's history is found
wherever it was written.

Sessions a home wrote before this existed sit in the bare index. On attaching with a
prefix, a home adopts the ones that are its own (:mod:`clio_agent.gact.session_records`)
and leaves a ``claimed/<id>`` marker, so the daemon's home stops listing them.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

from clio_agent import paths

logger = logging.getLogger(__name__)

#: Typed reason logged when this home's sessions are namespaced on another home's daemon.
CLIO_CORE_HOME_NAMESPACED = "clio_core_home_namespaced"

_lock = threading.Lock()
_active = ""  # this process's home namespace; "" while the daemon is this home's own


def home_identity() -> str:
    """Return this process's CLIO home: its resolved data dir, case-normalised."""
    return os.path.normcase(str(paths.user_data_dir().resolve()))


def namespace_for(identity: str) -> str:
    """Return the stable namespace a home with ``identity`` uses on another home's daemon."""
    return "home-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return a == b


def adopt_daemon_home(daemon_home: str, daemon_config_path: str, requested_config_path: str) -> str:
    """Decide this process's namespace on attaching to a daemon it did not spawn.

    Args:
        daemon_home: The home recorded by the daemon's spawner ("" from an older record).
        daemon_config_path: The config the daemon runs.
        requested_config_path: The config this home would have started it with.

    Returns:
        This home's namespace, "" for this home's own daemon.
    """
    global _active
    own = home_identity()
    if daemon_home:
        foreign = os.path.normcase(daemon_home) != own
    else:
        foreign = not _same_file(daemon_config_path, requested_config_path)
    namespace = namespace_for(own) if foreign else ""
    with _lock:
        _active = namespace
    if namespace:
        logger.warning(
            "clio-core daemon belongs to another CLIO home (reason=%s):             this home's sessions "
            "live under namespace %s; home=%s daemon_home=%s",
            CLIO_CORE_HOME_NAMESPACED,
            namespace,
            own,
            daemon_home or "<unrecorded>",
        )
    return namespace


def active_namespace() -> str:
    """Return this home's namespace on another home's daemon ("" when none)."""
    with _lock:
        return _active


def session_record_prefix() -> str:
    """Return the prefix of this home's session record names ("" for bare names)."""
    home = active_namespace()
    return f"{home}/" if home else ""


def reset() -> None:
    """Forget the active namespace (test seam; production decides once per attach)."""
    global _active
    with _lock:
        _active = ""


__all__ = [
    "CLIO_CORE_HOME_NAMESPACED",
    "active_namespace",
    "adopt_daemon_home",
    "home_identity",
    "namespace_for",
    "reset",
    "session_record_prefix",
]
