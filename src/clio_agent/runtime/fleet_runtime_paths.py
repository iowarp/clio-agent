"""The Windows fleet-runtime paths the codex restricted sandbox users must read and execute.

Pure planning for ``clio sandbox setup``: which runtime trees need an ``icacls`` RX grant, and the
``icacls`` argv for each. :mod:`clio_agent.runtime.sandbox_cli` executes the plan.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Fleet-runtime RX grants for the codex restricted sandbox users (WINDOWS).      #
# --------------------------------------------------------------------------- #
#
# After codex's setup helper creates the dedicated ``codexsandbox*`` accounts, a confined child
# still fails ``CreateProcessAsUserW`` with ``WinError 5`` (access denied) unless those restricted
# users can READ+EXEC the fleet runtime they must launch: the clio-kit launcher (uv tool bin), the
# uv-managed python + tool trees (the trampoline target), clio-kit's cache, and the per-user Temp
# (traverse only — the confined workspace lives under it). The per-user profile tree denies the
# codexsandbox* users by default, so we grant RX with ``icacls``. This is an own-profile DACL edit
# (the current user owns these paths → no admin needed) run as part of ``clio sandbox setup``.
# Proven live: the real released web MCP server would not spawn confined on Windows without it.

#: The two dedicated codex restricted-sandbox users (offline + online) the fleet runs confined as.
CODEX_SANDBOX_USERS: tuple[str, ...] = ("CodexSandboxOffline", "CodexSandboxOnline")

#: The canonical clio-kit tool launcher (kept in lockstep with
#: :data:`clio_agent.tools.mcp_config._CLIO_KIT_LAUNCHER`) — the exe the restricted users exec.
_CLIO_KIT_LAUNCHER = "clio-kit"


@dataclass(frozen=True)
class FleetGrant:
    """One planned ``icacls`` RX grant for the codex restricted users on a fleet-runtime path.

    Attributes:
        label: A machine-stable name for the path (e.g. ``uv_tool_bin``) — carried into the reason.
        path: The filesystem path the restricted users need read+exec on.
        users: The codex restricted users to grant (both offline + online).
        inherit: ``True`` → ``(OI)(CI)(RX)`` container/object inheritance for the runtime tree;
            ``False`` → a bare ``(RX)`` traverse grant on the dir itself (Temp).
        exists: Whether the path currently exists (a missing path is a typed skip, never a failure).
    """

    label: str
    path: str
    users: tuple[str, ...]
    inherit: bool
    exists: bool

    def icacls_argv(self, *, combine_users: bool = False) -> list[list[str]]:
        """Build root grants, optionally applying all users in one quiet operation.

        Windows propagates inheritable root ACEs to existing and future children.
        Adding ``/T`` also rewrites an explicit ACE at every descendant, repeatedly
        propagating through nested trees. Keep inheritance barriers intact instead.
        A traverse-only grant has neither inheritance flag.
        """
        perm = "(OI)(CI)(RX)" if self.inherit else "(RX)"
        if combine_users:
            if not self.users:
                return []
            argv = ["icacls", self.path, "/grant", *(f"{user}:{perm}" for user in self.users)]
            # Suppress success lines for every dependency file, retaining errors
            # and the summary. Permissions are identical to the per-user form.
            argv.append("/Q")
            return [argv]
        cmds: list[list[str]] = []
        for user in self.users:
            argv = ["icacls", self.path, "/grant", f"{user}:{perm}"]
            cmds.append(argv)
        return cmds


def _resolve_uv_tool_bin_dir() -> Optional[Path]:
    """Resolve the uv tool bin dir (where the clio-kit launcher exe lives) robustly.

    Prefer the launcher's real location (``shutil.which('clio-kit')`` → its parent); else the uv
    default tool-bin ``~/.local/bin``. Returns ``None`` only if neither can be formed (the executor
    then simply omits the entry — never fails setup).
    """
    import shutil  # noqa: PLC0415 - cheap, only on this path

    found = shutil.which(_CLIO_KIT_LAUNCHER)
    if found:
        try:
            return Path(found).resolve().parent
        except OSError:
            return Path(found).parent
    return Path.home() / ".local" / "bin"


def _uv_tool_python_home(environment: Path) -> Optional[Path]:
    """The base interpreter folder of a uv tool environment (its ``pyvenv.cfg`` ``home``)."""
    try:
        lines = (environment / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home" and value.strip():
            return Path(value.strip())
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(path)).startswith(
            os.path.normcase(os.path.abspath(root)) + os.sep
        )
    except (OSError, ValueError):
        return False


def _resolve_fleet_runtime_paths() -> list[tuple[str, str, bool]]:
    """Resolve the Windows fleet-runtime ``(label, path, inherit)`` specs the grant plan covers.

    Bundled Desktop uses its runtime and managed tool cache. Other installations use
    the uv tool bin dir (the clio-kit launcher), uv tools/Python trees and clio-kit's cache.
    These get ``(OI)(CI)(RX)`` inherited from each root;
    a bare ``(RX)`` traverse on per-user Temp (the confined workspace lives under
    it). Grant only the runtime tree, never the adjacent private Desktop data/config. Paths resolve
    via env vars / ``Path.home``
    robustly; a path that does not currently exist is STILL returned (the executor skips it with a
    typed reason). Windows-shaped, but pure — safe to call anywhere for the plan.
    """
    home = Path.home()
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    from clio_agent.tools.desktop_mcp_runtime import resolve_bundled_runtime_root  # noqa: PLC0415

    bundled = resolve_bundled_runtime_root()
    if bundled is not None:
        from clio_agent import paths  # noqa: PLC0415

        # Desktop launches clio-kit with its bundled interpreter, not the user's
        # unrelated uv environments. Recursing those global trees at every boot
        # can take minutes and grants access to runtimes this Desktop never uses.
        cache = os.environ.get("CLIO_KIT_CACHE_DIR") or paths.mcp_build_cache_dir() / "mcp-runtime"
        return [
            ("bundled_runtime", str(bundled), True),
            ("clio_kit_cache", str(cache), True),
            ("user_temp", str(local / "Temp"), False),
        ]
    specs: list[tuple[str, str, bool]] = []
    bin_dir = _resolve_uv_tool_bin_dir()
    if bin_dir is not None:
        specs.append(("uv_tool_bin", str(bin_dir), True))
    tools = os.environ.get("UV_TOOL_DIR") or str(roaming / "uv" / "tools")
    specs.append(("uv_tools", tools, True))
    uv_python = roaming / "uv" / "python"
    specs.append(("uv_python", str(uv_python), True))
    # install.ps1 keeps the Python of a clio-kit it owns inside the CLIO prefix.
    kit_python = _uv_tool_python_home(Path(tools) / _CLIO_KIT_LAUNCHER)
    if kit_python is not None and not _is_within(kit_python, uv_python):
        specs.append(("clio_kit_python", str(kit_python), True))
    specs.append(("clio_kit_cache", str(home / ".cache" / "clio-kit"), True))
    specs.append(("user_temp", str(local / "Temp"), False))
    return specs


def build_fleet_runtime_grant_plan(
    *,
    users: Sequence[str] = CODEX_SANDBOX_USERS,
    paths_override: Optional[Sequence[tuple[str, str, bool]]] = None,
) -> list[FleetGrant]:
    """Build the ``icacls`` RX grant plan for the codex restricted users over the fleet runtime.

    Returns one :class:`FleetGrant` per resolved path (each stamped with whether it EXISTS, so the
    executor can typed-skip a missing one). ``paths_override`` injects ``(label, path, inherit)``
    tuples for unit tests (never runs ``icacls``); otherwise the real Windows layout resolves via
    :func:`_resolve_fleet_runtime_paths`. Pure — builds the plan/argv only, touches nothing.
    """
    specs = list(paths_override) if paths_override is not None else _resolve_fleet_runtime_paths()
    utuple = tuple(users)
    return [
        FleetGrant(
            label=label,
            path=path,
            users=utuple,
            inherit=inherit,
            exists=Path(path).exists(),
        )
        for label, path, inherit in specs
    ]
