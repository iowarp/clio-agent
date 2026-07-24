"""The ``clio sandbox`` CLI verb: ``setup`` / ``status`` — Codex-backed (B-codex-5).

Owner module for the ``clio sandbox setup`` / ``clio sandbox status`` CLI. ``ui/cli.py`` only
parses argv and dispatches to :func:`run_sandbox_cli` (no accretion), exactly as it does for
``doctor``. This module replaces the deleted srt provisioning CLI (``sandbox_provision.py``); the
Codex-specific provisioning primitives it orchestrates live in
:mod:`clio_agent.runtime.sandbox_codex`.

* ``clio sandbox status`` (the default) renders the ``sandbox`` doctor row standalone (reuses
  :func:`~clio_agent.runtime.sandbox.probe_sandbox`).
* ``clio sandbox setup`` provisions the Codex **Windows** write fence: a ONE-TIME self-elevating
  ``codex sandbox`` run that fires codex's setup helper (creating the dedicated
  ``codexsandbox*`` accounts), then — crucially, #1026 no-false-green — VERIFIES enforcement
  before claiming provisioned and records the verdict in the clio-owned marker. Off-win32 setup is
  a typed no-op: codex uses Seatbelt/bubblewrap there, so there is nothing to provision.

HARD SAFETY RULE — the self-elevating ``codex sandbox`` run MUTATES the machine (creates the
``codexsandbox*`` accounts) and pops a UAC prompt, so it is guarded to run ONLY on real win32 and
is NEVER invoked from unit tests: every test injects a fake ``elevator`` / ``verifier`` /
``gate``. :func:`_elevated_codex_setup` asserts ``sys.platform == "win32"`` before touching
ShellExecute — it is the owner-gated manual live gate (``clio sandbox setup``), not a unit path.

PRECONDITIONS ARE TYPED, NEVER RAW: codex absent / below the validated floor yields a typed
reason + the exact install pointer (``npm install -g @openai/codex``), never a stack trace.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The exact pointer surfaced to a user missing codex (typed guidance, never a raw error).
CODEX_INSTALL_POINTER = "npm install -g @openai/codex"

# Setup OUTCOME statuses (the actions setup can report — a typed verdict the CLI branches on).
STATUS_NOT_WINDOWS = "not_windows"
OUTCOME_ALREADY_PROVISIONED = "already_provisioned"
OUTCOME_PROVISIONED = "provisioned"
OUTCOME_CODEX_ABSENT = "codex_absent"
OUTCOME_SETUP_FAILED = "codex_setup_failed"
OUTCOME_ENFORCEMENT_UNVERIFIED = "codex_enforcement_unverified"

# Typed reasons (no silent fallback — every state explains itself).
REASON_NOT_WINDOWS = "not_windows"
REASON_ALREADY_PROVISIONED = "codex_already_provisioned"
REASON_PROVISIONED = "codex_windows_provisioned"
REASON_SETUP_FAILED = "codex_windows_setup_failed"


@dataclass(frozen=True)
class CodexProvisionResult:
    """Outcome of a ``clio sandbox setup`` run (a typed, loggable record).

    Attributes:
        ok: Whether the Codex Windows fence is provisioned + enforcement-verified after this call
            (True for both a fresh provision AND an idempotent already-provisioned no-op).
        status: One of the ``OUTCOME_*`` / :data:`STATUS_NOT_WINDOWS` tokens.
        reason: A machine-stable typed reason.
        detail: A short human-readable evidence string.
        next_action: The guided next step when action is still required (else "No action ...").
        elevated: Whether this call actually invoked the self-elevation (a UAC prompt).
    """

    ok: bool
    status: str
    reason: str
    detail: str = ""
    next_action: str = ""
    elevated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The self-elevating Codex setup step (guarded — win32 only, never unit-run).   #
# --------------------------------------------------------------------------- #

# The ``ShellExecuteExW`` SEE_MASK + info struct + the elevation function live at MODULE scope
# inside a win32 guard (the struct is never inside a *function* — the no-class-in-function
# ratchet stays at 0). ``ctypes.wintypes`` is Windows-only, so the whole real implementation is
# defined ONLY on win32; the non-win32 branch defines a raising stub of the SAME name so the
# module-scope caller resolves it on every platform (mypy analyses the Linux branch) while the
# win32 names are never referenced off Windows.
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
if sys.platform == "win32":  # pragma: no cover - win32 live gate only (CI runs on Linux)
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _ShellExecuteInfoW(_ctypes.Structure):
        """The Win32 ``SHELLEXECUTEINFOW`` struct for the ``runas`` self-elevation."""

        _fields_ = [
            ("cbSize", _wintypes.DWORD),
            ("fMask", _wintypes.ULONG),
            ("hwnd", _wintypes.HWND),
            ("lpVerb", _wintypes.LPCWSTR),
            ("lpFile", _wintypes.LPCWSTR),
            ("lpParameters", _wintypes.LPCWSTR),
            ("lpDirectory", _wintypes.LPCWSTR),
            ("nShow", _ctypes.c_int),
            ("hInstApp", _wintypes.HINSTANCE),
            ("lpIDList", _ctypes.c_void_p),
            ("lpClass", _wintypes.LPCWSTR),
            ("hkeyClass", _wintypes.HKEY),
            ("dwHotKey", _wintypes.DWORD),
            ("hIcon", _wintypes.HANDLE),
            ("hProcess", _wintypes.HANDLE),
        ]

    def _elevated_codex_setup(binary: str) -> tuple[bool, str]:
        """Fire codex's one-time elevated Windows sandbox setup under a SINGLE UAC. win32 only.

        THE machine-mutating, UAC-popping step: a trivial ELEVATED ``codex sandbox`` run creates
        codex's dedicated ``codexsandbox*`` accounts (codex's setup helper runs on the first
        elevated use). An ``[windows] sandbox = "elevated"`` ``-p`` layer over a throwaway allow
        dir is materialized, and ``codex sandbox … -- cmd /c exit`` is launched via
        ``ShellExecuteExW`` with the ``runas`` verb. Returns ``(ok, detail)``; never raises for a
        non-zero exit. The owner-gated MANUAL LIVE GATE — never exercised by CI (unit tests inject
        a fake ``elevator``).
        """
        import tempfile  # noqa: PLC0415 - only on the live-gate path

        from clio_agent.runtime import sandbox_codex as scx  # noqa: PLC0415

        with tempfile.TemporaryDirectory(prefix="clio-codex-setup-") as allow:
            profile = scx.synthesize_codex_profile([allow])
            layer = scx.write_codex_layer("clio-setup", profile, elevated=True)
            argv = [
                *scx.codex_prefix(binary, "clio-setup", allow, layer_name=layer),
                "cmd",
                "/c",
                "exit",
            ]
        # cmd.exe running the codex shim so a .cmd launcher resolves under elevation.
        params = " ".join(f'"{a}"' if " " in a else a for a in argv)
        info = _ShellExecuteInfoW()
        info.cbSize = _ctypes.sizeof(info)
        info.fMask = _SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = "runas"
        info.lpFile = "cmd.exe"
        info.lpParameters = f"/c {params}"
        info.nShow = 0  # SW_HIDE
        if not _ctypes.windll.shell32.ShellExecuteExW(_ctypes.byref(info)):
            return False, (
                f"self-elevation refused or failed (GetLastError={_ctypes.get_last_error()})"
            )
        _ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = _wintypes.DWORD()
        _ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, _ctypes.byref(code))
        _ctypes.windll.kernel32.CloseHandle(info.hProcess)
        if code.value != 0:
            return False, f"elevated codex sandbox setup exited {code.value}"
        return True, "codex Windows sandbox setup completed under elevation"

else:

    def _elevated_codex_setup(binary: str) -> tuple[bool, str]:  # pragma: no cover
        """Non-win32 stub: codex setup elevation is win32-only; the caller guards on platform."""
        raise RuntimeError("codex Windows sandbox setup elevation is win32-only")


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
        inherit: ``True`` → ``(OI)(CI)(RX)`` container/object inheritance + ``/T`` recursion for a
            whole dir tree; ``False`` → a bare ``(RX)`` traverse grant on the dir itself (Temp).
        exists: Whether the path currently exists (a missing path is a typed skip, never a failure).
    """

    label: str
    path: str
    users: tuple[str, ...]
    inherit: bool
    exists: bool

    def icacls_argv(self) -> list[list[str]]:
        """The ``icacls`` argv — one invocation PER user — this grant would run.

        Recursive-inherit grants emit ``icacls <path> /grant "<User>:(OI)(CI)(RX)" /T``; a
        traverse-only grant emits ``icacls <path> /grant "<User>:(RX)"`` (no inheritance, no ``/T``).
        """
        perm = "(OI)(CI)(RX)" if self.inherit else "(RX)"
        cmds: list[list[str]] = []
        for user in self.users:
            argv = ["icacls", self.path, "/grant", f"{user}:{perm}"]
            if self.inherit:
                argv.append("/T")
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


def _resolve_fleet_runtime_paths() -> list[tuple[str, str, bool]]:
    """Resolve the Windows fleet-runtime ``(label, path, inherit)`` specs the grant plan covers.

    ``(OI)(CI)(RX)`` ``/T`` on: the uv tool bin dir (the clio-kit launcher), the uv tools tree, the
    uv-managed python tree, and clio-kit's cache; a bare ``(RX)`` traverse (no ``/T``) on the
    per-user Temp (the confined workspace lives under it). Paths resolve via env vars / ``Path.home``
    robustly; a path that does not currently exist is STILL returned (the executor skips it with a
    typed reason). Windows-shaped, but pure — safe to call anywhere for the plan.
    """
    home = Path.home()
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    specs: list[tuple[str, str, bool]] = []
    bin_dir = _resolve_uv_tool_bin_dir()
    if bin_dir is not None:
        specs.append(("uv_tool_bin", str(bin_dir), True))
    specs.append(("uv_tools", str(roaming / "uv" / "tools"), True))
    specs.append(("uv_python", str(roaming / "uv" / "python"), True))
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


def _run_icacls(argv: list[str]) -> tuple[int, str]:  # pragma: no cover - live win32 only
    """Run one ``icacls`` invocation; return ``(returncode, combined_output)``. Never raises."""
    import subprocess  # noqa: PLC0415 - only on the live-gate path

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def grant_fleet_runtime_access(
    *,
    runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
    plan: Optional[Sequence[FleetGrant]] = None,
    platform: str = sys.platform,
) -> list[dict[str, Any]]:
    """Grant the codex restricted sandbox users read+exec on the fleet runtime (Windows only).

    After the ``codexsandbox*`` accounts exist, ``CreateProcessAsUserW`` still fails ``WinError 5``
    unless those users can read+exec the launcher + uv-managed python/tools + clio-kit cache the
    fleet execs (the per-user profile tree denies them by default). This runs ``icacls`` per path
    per user (:meth:`FleetGrant.icacls_argv`), emitting a STRUCTURED reason record per grant
    (``granted`` / ``skipped_missing`` / ``failed`` / ``skipped_not_windows``) — never a silent
    step, never fails setup. A missing path is a typed skip; a non-zero ``icacls`` is a typed
    failure that is logged and stepped past (the paths that DID grant still help; a residual
    denial surfaces loudly at spawn, never swallowed here). ``runner`` / ``plan`` are injected by
    tests so no real ``icacls`` runs. Returns the per-grant reason records (for the caller/trace).
    """
    reasons: list[dict[str, Any]] = []
    if not platform.startswith("win"):
        logger.info("fleet-runtime grant skipped reason=not_windows")
        reasons.append({"grant": "fleet_runtime_access", "status": "skipped_not_windows"})
        return reasons
    grants = list(plan) if plan is not None else build_fleet_runtime_grant_plan()
    run = runner if runner is not None else _run_icacls
    for grant in grants:
        if not grant.exists:
            logger.info(
                "fleet-runtime grant skipped reason=path_missing label=%s path=%s",
                grant.label,
                grant.path,
            )
            reasons.append({"grant": grant.label, "path": grant.path, "status": "skipped_missing"})
            continue
        for argv in grant.icacls_argv():
            user = argv[3].split(":", 1)[0]
            rc, out = run(argv)
            record: dict[str, Any] = {
                "grant": grant.label,
                "path": grant.path,
                "user": user,
                "status": "granted" if rc == 0 else "failed",
                "rc": rc,
            }
            if rc == 0:
                logger.info(
                    "fleet-runtime grant ok label=%s path=%s user=%s", grant.label, grant.path, user
                )
            else:
                record["detail"] = out.strip()[:200]
                logger.warning(
                    "fleet-runtime grant FAILED label=%s path=%s user=%s rc=%s: %s",
                    grant.label,
                    grant.path,
                    user,
                    rc,
                    out.strip()[:200],
                )
            reasons.append(record)
    return reasons


def provision_codex_windows(
    *,
    detection: Any = None,
    elevator: Any = None,
    verifier: Any = None,
    marker_writer: Any = None,
    gate: Any = None,
    grantor: Any = None,
    platform: str = sys.platform,
) -> CodexProvisionResult:
    """Idempotently provision the Codex Windows write fence (``clio sandbox setup``) (B-codex-5).

    Flow:

    * off-win32 → typed no-op (:data:`STATUS_NOT_WINDOWS`); codex fences automatically there;
    * codex absent / below the validated floor → the typed install pointer, NO elevation;
    * already provisioned + enforcement-verified → idempotent no-op
      (:data:`OUTCOME_ALREADY_PROVISIONED`), ZERO prompts;
    * otherwise → one self-elevating ``codex sandbox`` setup (creates the ``codexsandbox*``
      accounts), GRANT those restricted users read+exec on the fleet runtime they must launch
      (:func:`grant_fleet_runtime_access` — else ``CreateProcessAsUserW`` fails ``WinError 5``),
      then run the real enforcement check
      (:func:`~clio_agent.runtime.sandbox_codex.verify_codex_enforcement`), persist its verdict in
      the marker, and RE-GATE. A provisioned account whose fence does NOT actually enforce a
      confined write is the honest :data:`OUTCOME_ENFORCEMENT_UNVERIFIED` (advisory-policy
      degrade), never a false green (#1026).

    Every machine-mutating step is injectable so the whole flow is unit-pinnable with fakes:
    ``elevator`` (the UAC elevation), ``verifier`` (the enforcement probe), ``marker_writer`` (the
    marker persist), ``gate`` (the cached provisioned+verified probe) and ``grantor`` (the
    fleet-runtime RX grants) are NEVER their real machine-touching implementations in tests. The
    per-grant reasons land on ``result.extra['fleet_runtime_grants']``. Returns a typed
    :class:`CodexProvisionResult`.
    """
    from clio_agent.runtime import sandbox_codex as scx  # noqa: PLC0415

    if not platform.startswith("win"):
        return CodexProvisionResult(
            ok=False,
            status=STATUS_NOT_WINDOWS,
            reason=REASON_NOT_WINDOWS,
            detail="Not Windows; codex uses Seatbelt/bubblewrap — there is nothing to provision.",
            next_action="No action required on this platform.",
        )

    det = detection if detection is not None else scx.detect_codex(platform=platform)
    if not (det.installed and det.reason == scx.REASON_CODEX_DETECTED):
        return CodexProvisionResult(
            ok=False,
            status=OUTCOME_CODEX_ABSENT,
            reason=det.reason,  # codex_not_installed / codex_version_unsupported
            detail=f"codex runtime not usable ({det.reason}); the Windows fence needs it.",
            next_action=f"Install/upgrade codex: `{CODEX_INSTALL_POINTER}`, then `clio sandbox setup`.",
        )

    gate_fn = gate if gate is not None else scx.codex_windows_gate
    grant_fn = grantor if grantor is not None else grant_fleet_runtime_access
    ready, _reason = gate_fn(platform=platform)
    if ready:
        # Idempotent: already provisioned + enforcement-verified. STILL (re)apply the fleet-runtime
        # RX grants — a box provisioned by a PRIOR clio version has the accounts but NOT the grants,
        # so its confined fleet children fail CreateProcessAsUserW/WinError 5 until granted. icacls RX
        # is idempotent (no-op when already present), so this is a safe, prompt-free upgrade path.
        grant_reasons = grant_fn()
        logger.info(
            "fleet-runtime grants (already-provisioned) statuses=%s",
            [r.get("status") for r in grant_reasons],
        )
        return CodexProvisionResult(
            ok=True,
            status=OUTCOME_ALREADY_PROVISIONED,
            reason=REASON_ALREADY_PROVISIONED,
            detail="Codex Windows write fence already provisioned; fleet-runtime access ensured.",
            next_action="No action required.",
            extra={"fleet_runtime_grants": grant_reasons},
        )

    # Not provisioned (or unverified) → the one-time self-elevating codex setup.
    elevate = elevator if elevator is not None else _elevated_codex_setup
    ok, detail = elevate(det.binary_path)
    if not ok:
        return CodexProvisionResult(
            ok=False,
            status=OUTCOME_SETUP_FAILED,
            reason=REASON_SETUP_FAILED,
            detail=detail,
            next_action="Re-run `clio sandbox setup` and approve the UAC prompt.",
            elevated=True,
        )

    # The codexsandbox* accounts now exist; grant them read+exec on the fleet runtime they must
    # launch (the launcher + uv-managed python/tools + clio-kit cache + Temp traverse) BEFORE the
    # enforcement probe spawns a confined child — CreateProcessAsUserW fails WinError 5 otherwise
    # (proven live). Own-profile DACL edit → no admin. Structured reasons kept on the result.
    grant_reasons = grant_fn()
    logger.info(
        "fleet-runtime grants applied statuses=%s",
        [r.get("status") for r in grant_reasons],
    )

    # The accounts are provisioned; now PROVE codex can actually enforce a confined write before
    # claiming the fence (#1026 — no false-green). Persist the verdict in the marker so the
    # ladder/doctor read an honest cached state without re-spawning codex every boot.
    verify = verifier if verifier is not None else scx.verify_codex_enforcement
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    enforced, enforce_reason = verify(
        det.binary_path, str(paths.user_cache_dir()), platform=platform
    )
    (marker_writer if marker_writer is not None else scx.write_codex_provision_marker)(
        det.version, enforcement_verified=enforced, enforcement_reason=enforce_reason
    )
    if enforced:
        return CodexProvisionResult(
            ok=True,
            status=OUTCOME_PROVISIONED,
            reason=REASON_PROVISIONED,
            detail="Codex Windows write fence provisioned + enforcement verified (one-time UAC).",
            next_action="No action required.",
            elevated=True,
            extra={"fleet_runtime_grants": grant_reasons},
        )
    # Setup ran and the accounts are real, but codex could NOT enforce a confined write on this
    # host. Report the honest degrade — clio runs on the advisory file policy.
    return CodexProvisionResult(
        ok=False,
        status=OUTCOME_ENFORCEMENT_UNVERIFIED,
        reason=enforce_reason or scx.REASON_CODEX_ENFORCEMENT_UNVERIFIED,
        detail=(
            "codex Windows sandbox setup ran and the accounts are provisioned, but codex could "
            f"NOT enforce a confined write ({enforce_reason}). clio runs with the advisory file "
            "policy (honest degrade — no OS write fence in force on this host)."
        ),
        next_action="Re-run `clio sandbox setup` to re-verify enforcement.",
        elevated=True,
        extra={"fleet_runtime_grants": grant_reasons},
    )


# --------------------------------------------------------------------------- #
# The CLI verb: `clio sandbox setup` / `clio sandbox status` (cli.py dispatches).#
# --------------------------------------------------------------------------- #


def run_sandbox_cli(
    action: Optional[str], *, json_output: bool = False, assume_yes: bool = False
) -> int:
    """Dispatch the ``sandbox`` CLI verb (``setup`` / ``status``). Returns the process exit code.

    ``ui/cli.py`` only parses argv and calls this (no accretion). ``status`` (the default) renders
    the ``sandbox`` doctor row standalone; ``setup`` runs the idempotent one-time Codex Windows
    provisioning. ``assume_yes`` is accepted for CLI-parity (Codex setup pops a native UAC prompt,
    not an in-terminal consent). Both emit typed, guided output — never a raw traceback.
    """
    act = (action or "status").strip().lower()
    if act == "status":
        return _sandbox_status_cli(json_output=json_output)
    if act == "setup":
        return _sandbox_setup_cli(json_output=json_output)
    _print(f"unknown sandbox action: {act!r} (expected 'setup' or 'status')", json_output, err=True)
    return 2


def _sandbox_status_cli(*, json_output: bool) -> int:
    """Render the ``sandbox`` doctor row standalone (reuses :func:`probe_sandbox`)."""
    from clio_agent.runtime.sandbox import install_sandbox, probe_sandbox  # noqa: PLC0415

    install_sandbox()  # resolve THIS process's backend so the row is real (no server needed)
    row = probe_sandbox()
    if json_output:
        import json  # noqa: PLC0415

        print(json.dumps(row.to_dict(), indent=2))
        return 0
    _render_status_row(row)
    return 0


def _sandbox_setup_cli(*, json_output: bool) -> int:
    """Run ``clio sandbox setup`` — the one-time Codex Windows provisioning (typed, guided).

    Off-Windows this is a typed no-op (codex fences automatically). On Windows it fires codex's
    one-time elevated setup (a native UAC prompt), verifies enforcement, and reports the typed
    verdict.
    """
    if not sys.platform.startswith("win"):
        msg = (
            "No setup needed on this platform: codex resolves an automatic per-process fence "
            "(Seatbelt/bubblewrap). `clio sandbox setup` provisions the one-time Codex Windows "
            "write fence only."
        )
        _print(
            msg, json_output, payload={"status": STATUS_NOT_WINDOWS, "reason": REASON_NOT_WINDOWS}
        )
        return 0
    result = provision_codex_windows()
    if json_output:
        import json  # noqa: PLC0415

        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "status": result.status,
                    "reason": result.reason,
                    "detail": result.detail,
                    "next_action": result.next_action,
                    "elevated": result.elevated,
                },
                indent=2,
            )
        )
        return 0 if result.ok else 1
    from rich.console import Console  # noqa: PLC0415

    console = Console()
    style = "green" if result.ok else "yellow"
    console.print(f"[{style}]sandbox setup: {result.status}[/{style}] ({result.reason})")
    if result.detail:
        console.print(f"  {result.detail}")
    if result.next_action:
        console.print(f"  next: {result.next_action}")
    return 0 if result.ok else 1


def _render_status_row(row: Any) -> None:
    """Print the single ``sandbox`` integration row as a compact table."""
    from rich.console import Console  # noqa: PLC0415
    from rich.markup import escape  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415

    styles = {"ready": "green", "degraded": "yellow", "skipped": "cyan"}
    state = row.state.value
    table = Table(title="CLIO Sandbox", show_header=True)
    table.add_column("Mechanism", style="cyan")
    table.add_column("Status")
    table.add_column("Summary")
    table.add_column("Next Action")
    table.add_row(
        escape(str(row.details.get("mechanism", "?"))),
        f"[{styles.get(state, 'white')}]{state}[/{styles.get(state, 'white')}]",
        escape(row.summary),
        escape(row.next_action),
    )
    Console().print(table)


def _print(
    message: str,
    json_output: bool,
    *,
    err: bool = False,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Emit a one-line message honoring ``--json`` (typed payload) or rich text."""
    if json_output:
        import json  # noqa: PLC0415

        print(json.dumps(payload if payload is not None else {"message": message}))
        return
    from rich.console import Console  # noqa: PLC0415

    Console().print(f"[{'red' if err else 'white'}]{message}[/]")


__all__ = [
    "CODEX_INSTALL_POINTER",
    "STATUS_NOT_WINDOWS",
    "OUTCOME_ALREADY_PROVISIONED",
    "OUTCOME_PROVISIONED",
    "OUTCOME_CODEX_ABSENT",
    "OUTCOME_SETUP_FAILED",
    "OUTCOME_ENFORCEMENT_UNVERIFIED",
    "REASON_NOT_WINDOWS",
    "REASON_ALREADY_PROVISIONED",
    "REASON_PROVISIONED",
    "REASON_SETUP_FAILED",
    "CodexProvisionResult",
    "CODEX_SANDBOX_USERS",
    "FleetGrant",
    "build_fleet_runtime_grant_plan",
    "grant_fleet_runtime_access",
    "provision_codex_windows",
    "run_sandbox_cli",
]
