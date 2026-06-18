"""Configuration for the distributable runtime + cluster deployment (epic #667).

ONE source of truth, with a strict per-parameter precedence the user asked for:

    config file  >  environment variable  >  built-in default

The config file (``CLIO_CLUSTER_CONFIG``, a YAML) defines the deployment so a run is
reproducible from a file rather than a scatter of env vars. :meth:`ClusterConfig.apply_to_env`
pushes the file's values into ``os.environ`` (file overrides any pre-existing env), so the
hot-path code keeps reading the same ``CLIO_*`` env vars it always did — the precedence is
honored without threading a config object through every call site.

Also the home of :func:`default_daemon_config_path`: the bundled, cluster-ready clio-core
daemon config under ``external/clio-core/clio.yaml`` that ``clio_run`` defaults to (bounded
DRAM + a disk tier, never the "80% of RAM" default).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

# external/clio-core/clio.yaml relative to repo root (this file is src/clio_agent/runtime/…)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DAEMON_CONFIG = _REPO_ROOT / "external" / "clio-core" / "clio.yaml"

# Each tunable: (dotted config-file key, env var, default, cast). The env var is BOTH the
# fallback source (when the file omits it) AND where apply_to_env writes the resolved value.
_PARAMS: dict[str, tuple[str, Any, Any]] = {
    "transport.mode": ("CLIO_EXPERT_INVOKER", "", str),
    "transport.poll_interval": ("CLIO_CORE_POLL", 0.05, float),
    "transport.ready_timeout": ("CLIO_CORE_READY_TIMEOUT", 60.0, float),
    "transport.timeout": ("CLIO_CORE_TIMEOUT", 300.0, float),
    "transport.prefix": ("CLIO_CORE_PREFIX", "clio_core_", str),
    "transport.pool_query": ("CLIO_CORE_POOL_QUERY", "dynamic", str),
    "transport.role": ("CLIO_CORE_ROLE", "", str),
    "transport.fleet": ("CLIO_CORE_FLEET", "", str),
    "daemon.config_path": ("CLIO_SERVER_CONF", "", str),
}

_MISSING = object()


def default_daemon_config_path() -> str:
    """Path to the bundled cluster-ready clio-core daemon config (``external/clio-core/
    clio.yaml``). ``clio_run`` is defaulted here so a bare start uses a bounded, disk-backed
    config instead of the 80%-of-RAM default."""
    return str(_DEFAULT_DAEMON_CONFIG)


class ClusterConfig:
    """Loaded distributable-runtime + deployment config. Per-param precedence is
    config-file > env > default (see module docstring)."""

    def __init__(self, *, path: Optional[str] = None, data: Optional[dict] = None) -> None:
        if data is not None:
            self._data: dict = data
        else:
            chosen = path if path is not None else os.environ.get("CLIO_CLUSTER_CONFIG", "")
            self._data = _load_yaml(chosen) if chosen and os.path.exists(chosen) else {}
        self.source = path or os.environ.get("CLIO_CLUSTER_CONFIG", "")

    # ---- raw lookups ---------------------------------------------------------------

    def _from_file(self, dotted: str) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return _MISSING
        return node

    def get(self, dotted: str, env_var: str = "", default: Any = None, cast: Any = str) -> Any:
        """Resolve one parameter: config file, then env var, then default."""
        val = self._from_file(dotted)
        if val is not _MISSING and val is not None:
            return cast(val)
        if env_var and os.environ.get(env_var, "") != "":
            return cast(os.environ[env_var])
        return default

    # ---- typed transport accessors -------------------------------------------------

    @property
    def mode(self) -> str:
        return self.get("transport.mode", "CLIO_EXPERT_INVOKER", "")

    @property
    def poll_interval(self) -> float:
        return self.get("transport.poll_interval", "CLIO_CORE_POLL", 0.05, float)

    @property
    def ready_timeout(self) -> float:
        return self.get("transport.ready_timeout", "CLIO_CORE_READY_TIMEOUT", 60.0, float)

    @property
    def timeout(self) -> float:
        return self.get("transport.timeout", "CLIO_CORE_TIMEOUT", 300.0, float)

    @property
    def prefix(self) -> str:
        return self.get("transport.prefix", "CLIO_CORE_PREFIX", "clio_core_")

    @property
    def pool_query(self) -> str:
        """Cross-node blob-visibility query mode: ``dynamic`` (single-node / local-first),
        ``broadcast`` (search all nodes — needed for a worker to read a blob another node
        wrote), or ``physical:<node_id>``."""
        return self.get("transport.pool_query", "CLIO_CORE_POOL_QUERY", "dynamic")

    @property
    def daemon_config_path(self) -> str:
        """The clio-core daemon config: file ``daemon.config_path``, else ``CLIO_SERVER_CONF``,
        else the bundled :func:`default_daemon_config_path`."""
        return self.get("daemon.config_path", "CLIO_SERVER_CONF", default_daemon_config_path())

    # ---- deployment (cluster) section ----------------------------------------------

    @property
    def cluster(self) -> dict:
        """The raw ``cluster:`` mapping (nodes, workers, ssh, shared paths) for the deployer."""
        c = self._from_file("cluster")
        return c if isinstance(c, dict) else {}

    # ---- precedence application -----------------------------------------------------

    def apply_to_env(self) -> dict[str, str]:
        """Push every config-file value into ``os.environ`` (file OVERRIDES pre-existing env),
        so downstream code reading ``CLIO_*`` honors config > env. Also defaults
        ``CLIO_SERVER_CONF`` to the bundled daemon config when neither the file nor the env set
        it. Returns the keys written (for logging/tests)."""
        written: dict[str, str] = {}
        for dotted, (env_var, _default, _cast) in _PARAMS.items():
            val = self._from_file(dotted)
            if val is not _MISSING and val is not None:
                os.environ[env_var] = str(val)
                written[env_var] = str(val)
        # make clio_run default to the bundled config when nothing set it
        if not os.environ.get("CLIO_SERVER_CONF"):
            os.environ["CLIO_SERVER_CONF"] = default_daemon_config_path()
            written.setdefault("CLIO_SERVER_CONF", os.environ["CLIO_SERVER_CONF"])
        return written


def _load_yaml(path: str) -> dict:
    import yaml  # noqa: PLC0415

    with open(path, encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return loaded if isinstance(loaded, dict) else {}


_CACHED: Optional[ClusterConfig] = None


def get_cluster_config(*, reload: bool = False) -> ClusterConfig:
    """Process-wide :class:`ClusterConfig` (loaded once from ``CLIO_CLUSTER_CONFIG``).
    Pass ``reload=True`` after changing the env/file (mainly tests)."""
    global _CACHED
    if _CACHED is None or reload:
        _CACHED = ClusterConfig()
    return _CACHED
