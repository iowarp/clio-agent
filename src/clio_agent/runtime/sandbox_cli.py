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
import sys
from dataclasses import dataclass, field
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


def provision_codex_windows(
    *,
    detection: Any = None,
    elevator: Any = None,
    verifier: Any = None,
    marker_writer: Any = None,
    gate: Any = None,
    platform: str = sys.platform,
) -> CodexProvisionResult:
    """Idempotently provision the Codex Windows write fence (``clio sandbox setup``) (B-codex-5).

    Flow:

    * off-win32 → typed no-op (:data:`STATUS_NOT_WINDOWS`); codex fences automatically there;
    * codex absent / below the validated floor → the typed install pointer, NO elevation;
    * already provisioned + enforcement-verified → idempotent no-op
      (:data:`OUTCOME_ALREADY_PROVISIONED`), ZERO prompts;
    * otherwise → one self-elevating ``codex sandbox`` setup (creates the ``codexsandbox*``
      accounts), then run the real enforcement check
      (:func:`~clio_agent.runtime.sandbox_codex.verify_codex_enforcement`), persist its verdict in
      the marker, and RE-GATE. A provisioned account whose fence does NOT actually enforce a
      confined write is the honest :data:`OUTCOME_ENFORCEMENT_UNVERIFIED` (advisory-policy
      degrade), never a false green (#1026).

    Every machine-mutating step is injectable so the whole flow is unit-pinnable with fakes:
    ``elevator`` (the UAC elevation), ``verifier`` (the enforcement probe), ``marker_writer`` (the
    marker persist) and ``gate`` (the cached provisioned+verified probe) are NEVER their real
    machine-touching implementations in tests. Returns a typed :class:`CodexProvisionResult`.
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
    ready, _reason = gate_fn(platform=platform)
    if ready:
        # Idempotent: already provisioned + enforcement-verified → no-op, zero prompts.
        return CodexProvisionResult(
            ok=True,
            status=OUTCOME_ALREADY_PROVISIONED,
            reason=REASON_ALREADY_PROVISIONED,
            detail="Codex Windows write fence already provisioned + enforcement verified.",
            next_action="No action required.",
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
    "provision_codex_windows",
    "run_sandbox_cli",
]
