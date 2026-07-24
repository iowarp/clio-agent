"""The ONE shared write-territory boundary both confinement twins consume (#974.6).

Split out of :mod:`clio_agent.runtime.sandbox` (the ladder/composition owner) so the
owner module stays under its file-size ratchet while B2 grows the ladder. This module
owns the single source of writable territory: the base is the ADVISORY
:attr:`clio_agent.tools.file_policy.FileAccessPolicy.allowed_roots` (the same source the
tool-boundary check reads), so the OS fence territory can never be narrower than what
file_policy already permits — anti-drift by construction (owner decision #974.6). The
fence then ADDS the caches a spawned launcher needs (tempdir + clio + tool caches) so
confinement never false-positives on a legitimate ``uv``/``npm`` cache write.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Optional

# Profiles (typed literals). ``fleet`` — the per-workspace, long-lived MCP servers
# (transport_for / transport_from_spec). ``shell`` — the per-invocation shell subprocess.
Profile = Literal["fleet", "shell"]
PROFILE_FLEET: Profile = "fleet"
PROFILE_SHELL: Profile = "shell"


# --------------------------------------------------------------------------- #
# Mid-session root grants (B5 #979.3) — the ONE process registry both the fence
# (:func:`effective_write_roots`) and the advisory twin (``file_policy``) consult so a
# recorded root grant takes effect LIVE on the next spawn/tool-boundary check, and the two
# can never drift (owner decision #974.6). Keyed by the workspace ROOT PATH — the same key
# both territory consumers already carry (the seams pass ``workspace_root``; file_policy
# resolves the active workspace root) — so no workspace_id has to be threaded through the
# runtime seams. A grant is a DECISION recorded through the route layer + persisted on the
# workspace record; this registry is the live in-process projection replayed at boot.
_GRANTED_WRITE_ROOTS: dict[str, tuple[Path, ...]] = {}
_GRANTS_LOCK = threading.Lock()


def _normalize_root_key(workspace_root: Optional[str]) -> str:
    if not workspace_root:
        return ""
    try:
        return str(Path(workspace_root).expanduser().resolve(strict=False))
    except OSError:
        return str(Path(workspace_root).expanduser())


def register_write_root_grant(workspace_root: str, granted: str) -> Path:
    """Register a granted writable root for ``workspace_root`` (idempotent). Returns the path.

    The live projection of a recorded workspace root grant: the next confined spawn's
    :func:`effective_write_roots` and the next advisory ``file_policy`` check both include it.
    Fenced children already spawned keep their compile-time territory until they respawn — the
    grant path restarts the workspace's resident fleet at a safe boundary so this takes effect
    live (a busy fleet defers, reported ``grant_restart_deferred_busy`` — #1033), never silently.
    """
    key = _normalize_root_key(workspace_root)
    resolved = Path(granted).expanduser()
    try:
        resolved = resolved.resolve(strict=False)
    except OSError:
        pass
    with _GRANTS_LOCK:
        current = list(_GRANTED_WRITE_ROOTS.get(key, ()))
        if resolved not in current:
            current.append(resolved)
        _GRANTED_WRITE_ROOTS[key] = tuple(current)
    return resolved


def granted_write_roots(workspace_root: Optional[str]) -> tuple[Path, ...]:
    """Return the roots granted for ``workspace_root`` (empty when none). Never raises."""
    key = _normalize_root_key(workspace_root)
    with _GRANTS_LOCK:
        return _GRANTED_WRITE_ROOTS.get(key, ())


def clear_write_root_grants() -> None:
    """Drop all registered root grants (test isolation seam)."""
    with _GRANTS_LOCK:
        _GRANTED_WRITE_ROOTS.clear()


def _platform_tool_cache_dirs() -> list[Path]:
    """Platform tool-cache + tool-DATA dirs the MCP fleet must be able to write.

    A fence that forgot these would break ``uv``/``npm``/``pip`` launchers mid-spawn (the
    false-positive guard). Kept bounded + honest: the clio-owned caches plus the common
    per-user tool caches/data dirs (present or not — they are writable territory, not a
    precondition).

    Includes the uv/clio-kit **data** dirs, not just caches: the shipped fleet launcher is
    ``clio-kit`` (``uv tool install clio-kit`` — install/install.sh), and launching an MCP
    server BUILDS that server's package **in-place inside the uv tool install tree**
    (``<uv-data>/uv/tools/clio-kit/clio-kit-mcp-servers/<name>``) and writes uv temp files
    there. Granting only the caches denied that build under an active fence (EROFS), so the
    whole fleet failed to start — caught by the B2 Linux live gate, invisible to the unit
    false-positive suite (which used a fixture server, not the real clio-kit uv-tool
    launcher). ``platformdirs`` resolves these per-OS (respects ``XDG_*``); the Windows
    layout is re-verified by B3's provisioned-fence gate (the Windows fence is floor here).
    """
    import platformdirs  # noqa: PLC0415 - cheap; only on this path

    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    dirs: list[Path] = [paths.user_cache_dir(), paths.user_config_dir(), paths.user_data_dir()]
    home = Path.home()
    # The uv + clio-kit cache AND data dirs (the fleet launcher's whole toolchain writes
    # here at spawn — see the docstring). platformdirs is per-OS + XDG-aware.
    dirs.extend(
        [
            Path(platformdirs.user_cache_dir("uv", appauthor=False)),
            Path(platformdirs.user_data_dir("uv", appauthor=False)),
            Path(platformdirs.user_cache_dir("clio-kit", appauthor=False)),
            Path(platformdirs.user_data_dir("clio-kit", appauthor=False)),
            home / ".cache" / "uv",
            home / ".local" / "share" / "uv",
            home / ".cache" / "clio-kit",
            home / ".cache" / "pip",
            home / ".npm",
        ]
    )
    return dirs


def effective_write_roots(
    profile: Profile,
    *,
    policy: Any = None,
    env: Optional[Mapping[str, str]] = None,
    workspace_root: Optional[str] = None,
) -> tuple[Path, ...]:
    """The writable territory for ``profile`` — the ONE boundary both twins consume (#974.6).

    Anti-drift by construction: the base is the ADVISORY
    :attr:`clio_agent.tools.file_policy.FileAccessPolicy.allowed_roots` (the same source
    the tool-boundary check reads), so the fence territory can never be narrower than what
    file_policy already permits. The fence then ADDS the caches a spawned launcher needs
    (tempdir + clio + tool caches) so confinement never false-positives on a legitimate
    ``uv``/``npm`` cache write.

    ``profile`` is :data:`PROFILE_FLEET` (long-lived MCP servers; adds the mcp-uv-cache +
    platform tool caches) or :data:`PROFILE_SHELL` (per-invocation). ``policy`` supplies the
    advisory base (defaults to :meth:`FileAccessPolicy.from_mapping` when ``env`` given, else
    ``from_env``); ``workspace_root`` includes an explicit root (shell computes it per
    invocation). Returns a deduped, order-stable tuple (advisory roots first).
    """
    import tempfile  # noqa: PLC0415 - cheap, only on this path

    from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415 - avoid cycle

    if policy is None:
        policy = (
            FileAccessPolicy.from_mapping(env) if env is not None else FileAccessPolicy.from_env()
        )
    roots: list[Path] = list(policy.allowed_roots)

    def _add(path: Path) -> None:
        resolved = path.expanduser()
        if resolved not in roots:
            roots.append(resolved)

    if workspace_root:
        _add(Path(workspace_root))
    # Mid-session root grants (B5 #979.3): a recorded workspace root grant takes effect on the
    # NEXT spawn — union the live registry so the fence territory widens without a restart.
    for granted in granted_write_roots(workspace_root):
        _add(granted)
    _add(Path(tempfile.gettempdir()))

    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    _add(paths.user_cache_dir())
    _add(paths.user_config_dir())

    if profile == PROFILE_FLEET:
        from clio_agent.tools.mcp_config import _mcp_uv_cache_dir  # noqa: PLC0415 - avoid cycle

        _add(_mcp_uv_cache_dir())
        for cache in _platform_tool_cache_dirs():
            _add(cache)

    return tuple(roots)


__all__ = [
    "PROFILE_FLEET",
    "PROFILE_SHELL",
    "Profile",
    "clear_write_root_grants",
    "effective_write_roots",
    "granted_write_roots",
    "register_write_root_grant",
]
