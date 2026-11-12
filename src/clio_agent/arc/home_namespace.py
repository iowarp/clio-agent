"""Keep each CLIO home's records apart on the one clio-core daemon per machine.

Every CLIO home on a machine attaches to the same daemon, and the first home to start
it decides its config (``clio_core_daemon_version.resolve_effective_config``). Records
were then shared too: they live under bare ``<kind>`` tags, so a second home listed the
first home's sessions and workspaces as its own.

The process that spawns the daemon records its home next to the pidfile. A process
from another home that attaches still adopts the daemon's config (ports and tiers must
match to attach at all), but every store it opens nests its tags under a namespace
derived from its own home: ``home-<digest>/<kind>``. The daemon's own home keeps the
bare tags it always had, so a single-home install sees no change.

An explicit ``arc.namespace`` / ``CLIO_ARC_NAMESPACE`` is the operator's choice and is
used as is, never nested.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

from clio_agent import paths

logger = logging.getLogger(__name__)

#: Typed reason logged when this home's records are namespaced on another home's daemon.
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
        The namespace this process's stores nest under, "" for this home's own daemon.
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
            "clio-core daemon belongs to another CLIO home (reason=%s): this home's records "
            "live under namespace %s; home=%s daemon_home=%s",
            CLIO_CORE_HOME_NAMESPACED,
            namespace,
            own,
            daemon_home or "<unrecorded>",
        )
    return namespace


def active_namespace() -> str:
    """Return the namespace this process's stores nest under ("" when none)."""
    with _lock:
        return _active


def scoped(namespace: str) -> str:
    """Nest a store's ``namespace`` under this home's, unless one is configured explicitly."""
    home = active_namespace()
    if not home:
        return namespace
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    explicit = conf.resolve("arc.namespace", env="CLIO_ARC_NAMESPACE", default="", cast=conf.as_str)
    if explicit.strip().strip("/"):
        return namespace
    return f"{home}/{namespace}" if namespace else home


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
    "scoped",
]
