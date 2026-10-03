"""Agent filesystem roots: clio.filesystem/v1, contract 1.1.

Resolution is read-only. Role overrides outrank CLIO_AGENT_HOME and native
roots. CLIO_USER_DIR retains its old layout for one compatibility release.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import warnings
from collections.abc import Mapping
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal

logger = logging.getLogger(__name__)
Role = Literal["config", "data", "state", "cache", "runtime"]
Platform = Literal["linux", "macos", "windows"]
_APP = "clio-agent"


class HomeDirectoryUnavailable(RuntimeError):
    """Native roots cannot be resolved and no explicit role root was configured."""


def resolve_root(
    role: Role, *, home: str | PurePath, env: Mapping[str, str], platform: Platform
) -> PurePath | None:
    """Resolve without filesystem access; absent runtime means private temp.

    This pure entry point supports cross-platform contract checks. Runtime has
    no HOME fallback: its caller must allocate a private instance directory.
    """
    pure = PureWindowsPath if platform == "windows" else PurePosixPath
    base = pure(home)

    def override(name: str) -> PurePath | None:
        raw = env.get(name, "").strip()
        if not raw:
            return None
        path = pure(raw)
        if not path.is_absolute():
            raise ValueError(f"{name} must be an absolute path")
        return path

    direct = override(f"CLIO_AGENT_{role.upper()}_DIR")
    if direct is not None:
        return direct
    if role != "runtime":
        relocated = override("CLIO_AGENT_HOME")
        if relocated is not None:
            return relocated / role
        legacy = override("CLIO_USER_DIR")
        if legacy is not None:
            warnings.warn(
                "CLIO_USER_DIR is deprecated; migrate to CLIO_AGENT_HOME",
                FutureWarning,
                stacklevel=2,
            )
            return legacy if role == "config" else legacy / role
    if platform == "windows":
        if role == "runtime":
            return None
        name = "APPDATA" if role == "config" else "LOCALAPPDATA"
        fallback = base / "AppData" / ("Roaming" if role == "config" else "Local")
        return (override(name) or fallback) / _APP / role
    if platform == "macos":
        if role == "runtime":
            return None
        if role == "cache":
            return base / "Library" / "Caches" / _APP
        return base / "Library" / "Application Support" / _APP / role
    defaults = {
        "config": base / ".config",
        "data": base / ".local" / "share",
        "state": base / ".local" / "state",
        "cache": base / ".cache",
        "runtime": None,
    }
    name = "XDG_RUNTIME_DIR" if role == "runtime" else f"XDG_{role.upper()}_HOME"
    raw = env.get(name)
    selected = defaults[role]
    if raw is not None:
        if raw and pure(raw).is_absolute():
            selected = pure(raw)
        else:
            logger.warning("%s is empty or relative; using the native fallback", name)
    return selected / _APP if selected is not None else None


def _platform() -> Platform:
    return (
        "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
    )


def _root(
    role: Role,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    legacy: bool = True,
) -> Path:
    source = os.environ if env is None else env
    override_names = (f"CLIO_AGENT_{role.upper()}_DIR", "CLIO_AGENT_HOME", "CLIO_USER_DIR")
    explicit = next((source[name] for name in override_names if source.get(name, "").strip()), "")
    # A fully relocated process need not have a resolvable OS home (service users).
    try:
        base_home = home if home is not None else Path(explicit) if explicit else Path.home()
    except RuntimeError as exc:
        raise HomeDirectoryUnavailable("Configure CLIO_AGENT_HOME for this service user") from exc
    root = resolve_root(role, home=base_home, env=source, platform=_platform())
    if root is None:
        raise ValueError("runtime requires a private instance directory")
    result = Path(root)
    if legacy and not any(
        source.get(key, "").strip()
        for key in ("CLIO_AGENT_HOME", "CLIO_USER_DIR", f"CLIO_AGENT_{role.upper()}_DIR")
    ):
        # Continue an existing user store until the explicit migration retires it.
        # In particular Windows/macOS previously used the XDG spelling too.
        legacy_base = (
            Path(source.get("XDG_CONFIG_HOME") or (home or Path.home()) / ".config") / _APP
        )
        legacy_path = legacy_base if role == "config" else legacy_base / role
        if legacy_path != result and legacy_path.is_dir() and not result.exists():
            logger.warning(
                "Using legacy Agent %s at %s; run migrate-paths to relocate it", role, legacy_path
            )
            return legacy_path
    return result


def canonical_root(role: Role) -> Path:
    """Return the configured root without the transitional existing-store fallback."""
    return _root(role, legacy=False)


def user_config_dir() -> Path:
    """Return the user-authored configuration root."""
    return _root("config")


def user_config_dir_for(home: Path, env: Mapping[str, str]) -> Path:
    """Resolve config using injected inputs and the same rules as production."""
    return _root("config", home=home, env=env)


def user_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the durable Agent data root."""
    return _root("data", env=env)


def user_state_dir() -> Path:
    """Return the generated Agent state root."""
    return _root("state")


def host_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return host-wide Agent coordination state, shared by managed installs.

    CLIO_AGENT_HOME/CLIO_USER_DIR isolate an instance's content, not the one
    Core daemon's fixed RPC endpoint. CLIO_RUNTIME_STATE_DIR remains the explicit
    complete isolation override at the runtime owner.
    """
    source = dict(os.environ if env is None else env)
    source.pop("CLIO_AGENT_HOME", None)
    source.pop("CLIO_USER_DIR", None)
    return _root("state", env=source, legacy=False)


def user_cache_dir() -> Path:
    """Return the disposable Agent cache root."""
    return _root("cache")


def server_state_dir() -> Path:
    """Return persistent server state, never a directory inside the install."""
    return user_state_dir() / "server"


def arc_data_dir() -> Path:
    """Keep an existing working-directory ARC active until explicit migration."""
    legacy = Path.cwd() / ".clio" / "agent" / "arc"
    if legacy.is_dir():
        logger.warning("Using legacy ARC at %s; migrate-paths relocates this store", legacy)
        return legacy
    return user_data_dir() / "arc"


def workspace_key(root: str | Path) -> str:
    """Return a stable identity shared by writers that know the workspace path."""
    normalized = os.path.normcase(str(Path(root).expanduser().resolve()))
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def workspace_state_dir(root: str | Path) -> Path:
    """Return generated workspace storage without creating project directories."""
    return user_state_dir() / "workspaces" / workspace_key(root)


def workspace_cache_dir(root: str | Path) -> Path:
    """Return the workspace's disposable sandbox cache."""
    return user_cache_dir() / "sandbox" / workspace_key(root)


def workspace_shared_dir(root: str | Path) -> Path:
    """Return the explicit, authored project configuration location."""
    return Path(root) / ".clio-agent" / "shared"


def workspace_config_path(root: str | Path, name: str) -> Path:
    """Read authored configuration from the new location, then the legacy one.

    Writers must use workspace_shared_dir. The fallback is read-only and lasts
    until an explicit migration; an upgrade must not hide existing settings.
    """
    current = workspace_shared_dir(root) / name
    legacy = Path(root) / ".clio" / name
    if not current.exists() and legacy.exists():
        logger.warning(
            "Legacy workspace configuration at %s; migrate to .clio-agent/shared", legacy
        )
        return legacy
    return current


def initialize_workspace(root: Path) -> Path:
    """Explicitly initialize a project, installing self-ignore before any payload."""
    if not root.is_dir():
        raise ValueError("workspace must already exist")
    project = root / ".clio-agent"
    local = project / "local"
    if project.is_symlink() or local.is_symlink():
        raise ValueError("workspace initialization refuses symlinks")
    local.mkdir(parents=True, exist_ok=True)
    marker = local / ".gitignore"
    try:
        with marker.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("*\n")
    except FileExistsError:
        if marker.is_symlink() or marker.read_text(encoding="utf-8") != "*\n":
            raise ValueError(
                "existing local/.gitignore must contain exactly '*' and a newline"
            ) from None
    (project / "shared").mkdir(exist_ok=True)
    return project


def workspace_clio(cwd: str | Path | None = None) -> Path:
    """Compatibility helper for generated state; no project-root writes."""
    return workspace_state_dir(cwd) if cwd is not None else server_state_dir()


def workspace_agent_dir(cwd: str | Path | None = None) -> Path:
    """Return workspace state or, without a workspace, server state."""
    return workspace_clio(cwd)


def workspace_core_dir(cwd: str | Path | None = None) -> Path:
    """Return the authored workspace CTE configuration lookup location."""
    return workspace_config_path(cwd if cwd is not None else Path.cwd(), "core")
