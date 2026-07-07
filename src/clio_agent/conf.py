"""File→env→default configuration resolution for CLIO Agent.

A single, dependency-free resolver so configuration is *sharable* (committable
to a YAML file) instead of scattered across hardcoded constants and bare
``os.environ`` reads. Every knob resolves with one precedence:

1. **config FILE** — workspace ``<cwd>/.clio/config.yaml`` deep-merged OVER
   user ``<config>/clio-agent/config.yaml`` (mirrors the ``mcp.yaml`` discovery
   in :mod:`clio_agent.tools.mcp_config`; honours ``XDG_CONFIG_HOME``).
2. **environment** — ``os.environ`` (already seeded by ``.env`` via
   :func:`clio_agent.config.load_project_env_file`, so dotenv keeps working).
3. **default** — the in-code fallback.

The FILE wins over the environment on purpose: the file is the sharable source
of truth and the environment is the fallback used only when a key is absent from
the file. This is the inverse of the 12-factor "env overrides file" convention,
and is a deliberate project decision (see the logging/config plan).

**Deliberately NOT resolved through this store** (they stay bare ``os.environ``
reads on purpose, so a config file cannot silently redirect them):

- *Bootstrap tier* — read before this store (or its file discovery) exists, so a
  ``resolve`` call here would recurse or read a not-yet-loaded layer:
  ``CLIO_USER_DIR`` (``clio_agent.paths``; :meth:`ConfigStore._load` imports
  ``paths``), ``CLIO_ENV_FILE`` / ``CLIO_ENV_FILE_LOADED`` (the dotenv loader in
  ``clio_agent.config``), and ``XDG_CONFIG_HOME`` (drives the file discovery).
- *Secret tier* — never committed to a shared config file; env-only by policy:
  ``CLIO_LM_API_KEY``, ``CLIO_ARGONNE_TOKEN``, ``ALCF_INFERENCE_TOKEN``, and the
  ``CLIO_CRED_*`` credential vars.
- *Provider auth-status probes* — presence-of-env checks that drive the auth UI
  in ``clio_agent.gact.routes.providers`` must reflect the real process env, not
  a file layer, so they stay env-direct.

Usage::

    from clio_agent import conf
    timeout = conf.resolve("limits.lm_call_s", env="CLIO_MAX_LM_CALL_S",
                           default=1800.0, cast=conf.as_float)
    level = conf.resolve("debug.level", env="CLIO_DEBUG", default="low")
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

import yaml

T = TypeVar("T")

_USER_CONFIG_RELPATH = ("clio-agent", "config.yaml")
_WORKSPACE_CONFIG_RELPATH = (".clio", "config.yaml")

# Sentinel distinguishing "key absent from file" from "file value is None/empty".
_UNSET: Any = object()


# --------------------------------------------------------------------------- #
# Cast helpers — accept the raw value from any source (already-typed YAML scalar
# or a string from the environment) and coerce explicitly. Raise on garbage so a
# misconfigured value fails loudly rather than silently falling back.
# --------------------------------------------------------------------------- #

_TRUE_TOKENS = {"1", "true", "yes", "on", "y", "t"}
_FALSE_TOKENS = {"0", "false", "no", "off", "n", "f", ""}


def as_str(value: Any) -> str:
    """Coerce to ``str`` (pass through actual strings unchanged)."""
    return value if isinstance(value, str) else str(value)


def as_bool(value: Any) -> bool:
    """Coerce a YAML bool/number or an env string to ``bool``.

    Accepts ``1/true/yes/on/y/t`` (true) and ``0/false/no/off/n/f``/empty
    (false), case-insensitively. Raises ``ValueError`` on anything else.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value).strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(f"cannot interpret {value!r} as a boolean")


def as_int(value: Any) -> int:
    """Coerce to ``int``. Raises ``ValueError``/``TypeError`` on non-integers."""
    if isinstance(value, bool):
        raise ValueError(f"refusing to read boolean {value!r} as an int")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return int(str(value).strip())


def as_float(value: Any) -> float:
    """Coerce to ``float``. Raises ``ValueError``/``TypeError`` on non-numbers."""
    if isinstance(value, bool):
        raise ValueError(f"refusing to read boolean {value!r} as a float")
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).strip())


def as_csv(value: Any) -> list[str]:
    """Coerce a YAML list or a comma-separated string to ``list[str]``.

    Whitespace-trimmed; empty items are dropped.
    """
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


# --------------------------------------------------------------------------- #
# File discovery + merge
# --------------------------------------------------------------------------- #


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML file into a dict, returning ``{}`` on missing/invalid input."""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` (override wins per leaf)."""
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = out.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


class ConfigStore:
    """Lazily-loaded, cached merge of the user + workspace config files.

    The file layer is loaded once and cached; :meth:`reload` clears the cache.
    The environment layer is read live on every :meth:`resolve` call, so env
    changes take effect without a reload (matching how ``os.environ`` is used
    everywhere else).
    """

    def __init__(
        self,
        *,
        home: Path | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._home = home
        self._cwd = cwd
        self._env = env
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def _env_map(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def _load(self) -> dict[str, Any]:
        from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

        home = self._home or Path.home()
        cwd = self._cwd or Path.cwd()
        env = self._env_map()
        # OS-correct per-user config dir (honors injected home/env for tests).
        user = _read_yaml_mapping(paths.user_config_dir_for(home, env) / _USER_CONFIG_RELPATH[-1])
        workspace = _read_yaml_mapping(cwd.joinpath(*_WORKSPACE_CONFIG_RELPATH))
        return _deep_merge(user, workspace)

    @property
    def data(self) -> dict[str, Any]:
        """The merged file config (workspace over user), loaded once and cached."""
        with self._lock:
            if self._data is None:
                self._data = self._load()
            return self._data

    def reload(self) -> None:
        """Drop the cached file config so the next access re-reads from disk."""
        with self._lock:
            self._data = None

    def file_value(self, key: str) -> Any:
        """Return the dotted-path value from the file layer, or ``_UNSET``."""
        node: Any = self.data
        for part in key.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                return _UNSET
        return node

    def resolve(
        self,
        key: str,
        *,
        env: str,
        default: T,
        cast: Callable[[Any], T] | None = None,
    ) -> T:
        """Resolve ``key`` by file → env → default precedence.

        Args:
            key: dotted path into the YAML file (e.g. ``"debug.level"``).
            env: environment variable name checked when the file lacks ``key``.
            default: returned when neither file nor env provides a value.
            cast: optional coercion applied to a file/env value (not to
                ``default``). Use the module ``as_*`` helpers. When ``None`` the
                value is returned unchanged (env values are therefore strings).

        Returns:
            The resolved, optionally-cast value.
        """
        file_value = self.file_value(key)
        if file_value is not _UNSET:
            return cast(file_value) if cast is not None else file_value
        env_value = self._env_map().get(env)
        if env_value is not None and env_value.strip() != "":
            return cast(env_value) if cast is not None else env_value  # type: ignore[return-value]
        return default


# Process-wide default store (real home/cwd/env). Tests construct their own
# ``ConfigStore`` with injected paths; production code uses these module funcs.
_STORE = ConfigStore()


def resolve(
    key: str,
    *,
    env: str,
    default: T,
    cast: Callable[[Any], T] | None = None,
) -> T:
    """Resolve a config value via the process-wide store (file → env → default)."""
    return _STORE.resolve(key, env=env, default=default, cast=cast)


def reload() -> None:
    """Clear the process-wide store's cached file config."""
    _STORE.reload()


def store() -> ConfigStore:
    """Return the process-wide :class:`ConfigStore` (for tests / introspection)."""
    return _STORE
