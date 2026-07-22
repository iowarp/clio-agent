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

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Optional

# Profiles (typed literals). ``fleet`` — the per-workspace, long-lived MCP servers
# (transport_for / transport_from_spec). ``shell`` — the per-invocation shell subprocess.
Profile = Literal["fleet", "shell"]
PROFILE_FLEET: Profile = "fleet"
PROFILE_SHELL: Profile = "shell"


def _platform_tool_cache_dirs() -> list[Path]:
    """Platform tool-cache dirs the MCP fleet must be able to write (false-positive guard).

    A fence that forgot these would break ``uv``/``npm``/``pip`` launchers mid-spawn. Kept
    bounded + honest: the clio-owned caches plus the common per-user tool caches (present or
    not — they are writable territory, not a precondition).
    """
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    dirs: list[Path] = [paths.user_cache_dir(), paths.user_config_dir(), paths.user_data_dir()]
    home = Path.home()
    # Common per-user tool caches (uv, npm, pip). Present or not, they are writable
    # territory for a launcher, so the fence must include them.
    dirs.extend(
        [
            home / ".cache" / "uv",
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
    "effective_write_roots",
]
