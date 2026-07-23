"""Windows one-time sandbox provisioning: ``clio sandbox setup`` / ``status`` (#977/B3).

The Windows write fence is srt's ACL/WFP backend. Unlike Linux/macOS — where the
:mod:`clio_agent.runtime.sandbox` ladder activates a fence *per process, unprivileged, with
no host mutation* — the Windows backend needs a **one-time, self-elevating, idempotent**
provisioning step (owner decision #974.2): a single UAC prompt that runs
``srt windows-install`` to create the ``srt-sandbox`` principal + the WFP filters. After that
one-time setup, every per-session use is unprivileged, and every Windows fence record is
labeled write-fence-grade.

This owner module holds ALL the provisioning logic (owner decision: no accretion into
``ui/cli.py`` — the CLI only parses + dispatches to :func:`run_sandbox_cli`, exactly as it
does for ``doctor``). It is the SINGLE SOURCE of the Windows provisioning verdict
(:func:`windows_sandbox_state`) that three consumers read: the ``clio sandbox status`` CLI,
the ``sandbox`` doctor row, and the :mod:`clio_agent.runtime.sandbox` ladder's Windows rung.

HARD SAFETY RULE — the self-elevation + ``srt windows-install`` MUTATE the machine and pop a
UAC prompt, so they are **guarded to run only on real win32 with srt present** and are NEVER
invoked from unit tests: every test injects a fake ``installer`` / ``provisioned_probe`` and
exercises the flow around the real call. :func:`_elevated_srt_windows_install` asserts
``sys.platform == "win32"`` before touching ShellExecute — it is the owner-gated manual live
gate (``clio sandbox setup``), not a unit path.

PRECONDITIONS ARE TYPED, NEVER RAW (owner direction on #977): node/npm/srt absent yields a
typed reason + the exact install pointer (``npm install -g @anthropic-ai/sandbox-runtime``),
never a stack trace — so ``sandbox setup`` is a first-class, guided flow on every install
channel (uv/pip console entry point AND the desktop ``clio`` launcher passthrough).

IDEMPOTENCE: a re-run detects the already-provisioned state and no-ops WITHOUT elevating
(``already_provisioned``, zero prompts) — safe for a curious user on any channel to run twice.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from clio_agent.runtime.sandbox import (
    MECHANISM_SRT_WINDOWS,
    REASON_SRT_NOT_INSTALLED,
    REASON_WINDOWS_UNPROVISIONED,
    SRT_PACKAGE_NAME,
    SrtDetection,
    detect_srt,
)
from clio_agent.runtime.sandbox_srt import is_srt_version_supported

logger = logging.getLogger(__name__)

# The Windows principal + WFP filters `srt windows-install` provisions (owner note #974).
SRT_WINDOWS_PRINCIPAL = "srt-sandbox"
#: The srt subcommand that provisions / removes the Windows fence ("one UAC prompt").
SRT_WINDOWS_INSTALL_SUBCOMMAND = "windows-install"
SRT_WINDOWS_UNINSTALL_SUBCOMMAND = "windows-uninstall"
#: The clio-owned marker recording a successful provision (under the config dir, not a 5th
#: store — a single small file in the existing config tree). Its presence + a live principal
#: is the idempotence signal.
SRT_WINDOWS_MARKER_NAME = "windows-provisioned.json"
#: The exact pointer surfaced to a user missing srt (owner direction on #977 — never a raw error).
SRT_INSTALL_POINTER = f"npm install -g {SRT_PACKAGE_NAME}"

# Provisioning STATUS literals (the typed verdict the three consumers branch on).
STATUS_PROVISIONED = "provisioned"
STATUS_UNPROVISIONED = "unprovisioned"
STATUS_SRT_ABSENT = "srt_absent"
STATUS_NOT_WINDOWS = "not_windows"

# Setup OUTCOME statuses (a superset — the actions setup can report).
OUTCOME_ALREADY_PROVISIONED = "already_provisioned"
OUTCOME_PROVISIONED = "provisioned"
OUTCOME_PROVISION_FAILED = "provision_failed"
OUTCOME_PROVISION_VERIFY_FAILED = "provision_verify_failed"

# Typed reasons (no silent fallback — every state explains itself).
REASON_NOT_WINDOWS = "not_windows"
REASON_WINDOWS_PROVISIONED = "windows_provisioned"
REASON_WINDOWS_PROVISION_INCOMPLETE = "windows_provisioning_incomplete"
REASON_ALREADY_PROVISIONED = "already_provisioned"
REASON_PROVISION_FAILED = "srt_windows_install_failed"
REASON_PROVISION_VERIFY_FAILED = "srt_windows_install_unverified"
REASON_SRT_VERSION_UNSUPPORTED = "srt_version_unsupported"


@dataclass(frozen=True)
class WindowsSandboxState:
    """The Windows provisioning verdict — the single source the three consumers read.

    Attributes:
        status: One of :data:`STATUS_PROVISIONED`, :data:`STATUS_UNPROVISIONED`,
            :data:`STATUS_SRT_ABSENT`, :data:`STATUS_NOT_WINDOWS`.
        reason: A machine-stable typed reason token for the verdict.
        srt: The srt detection this verdict was computed from (``None`` off-Windows).
        detail: A short human-readable evidence string.
        next_action: The exact guided next step (an install pointer, ``clio sandbox setup``,
            or "no action") — never a raw error.
    """

    status: str
    reason: str
    srt: Optional[SrtDetection] = None
    detail: str = ""
    next_action: str = ""


@dataclass(frozen=True)
class WindowsProvisionResult:
    """Outcome of a ``clio sandbox setup`` run (a typed, loggable record).

    Attributes:
        ok: Whether the fence is provisioned after this call (True for both a fresh
            provision AND an idempotent already-provisioned no-op).
        status: One of the ``OUTCOME_*`` / ``STATUS_*`` tokens.
        reason: A machine-stable typed reason.
        detail: A short human-readable evidence string.
        next_action: The guided next step when action is still required (else "").
        elevated: Whether this call actually invoked the self-elevation (a UAC prompt).
    """

    ok: bool
    status: str
    reason: str
    detail: str = ""
    next_action: str = ""
    elevated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _marker_path() -> Path:
    """The clio-owned provisioning marker path (under the existing config dir, no new store)."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    return paths.user_config_dir() / "sandbox" / SRT_WINDOWS_MARKER_NAME


def _read_marker() -> Optional[dict[str, Any]]:
    """Read the provisioning marker, or ``None`` when absent/unreadable (honest empty)."""
    import json  # noqa: PLC0415 - only on this path

    path = _marker_path()
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.info("windows sandbox marker unreadable reason=marker_unreadable error=%r", exc)
        return None


def write_provision_marker(version: str) -> Path:
    """Persist the provisioning marker after a successful ``srt windows-install``.

    Records the srt version + timestamp + principal so a later idempotent re-run can verify
    provisioning WITHOUT elevating. Returns the written path.
    """
    import json  # noqa: PLC0415 - only on this path
    from datetime import datetime, timezone  # noqa: PLC0415

    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "principal": SRT_WINDOWS_PRINCIPAL,
        "srt_version": version,
        "provisioned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _srt_principal_exists(*, platform: str = sys.platform) -> bool:
    """Whether the ``srt-sandbox`` Windows principal exists (a non-mutating ``net user`` query).

    Guarded to win32 (``net user`` is Windows-only); off-Windows it is vacuously ``False``.
    Best-effort + short timeout — a query failure is treated as "cannot confirm" (not
    provisioned), never a raised error.
    """
    if not platform.startswith("win"):
        return False
    import subprocess  # noqa: PLC0415 - only on this path

    try:
        proc = subprocess.run(
            ["net", "user", SRT_WINDOWS_PRINCIPAL],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("srt principal probe failed reason=principal_probe_failed error=%r", exc)
        return False
    return proc.returncode == 0


def _default_provisioned_probe(*, platform: str = sys.platform) -> tuple[bool, str]:
    """Default provisioned-state probe: marker present AND the ``srt-sandbox`` principal exists.

    Returns ``(provisioned, reason)``. A marker with a missing principal is the honest
    ``windows_provisioning_incomplete`` (someone removed the account) — NOT a false green.
    """
    marker = _read_marker()
    if marker is None:
        return False, REASON_WINDOWS_UNPROVISIONED
    if not _srt_principal_exists(platform=platform):
        return False, REASON_WINDOWS_PROVISION_INCOMPLETE
    return True, REASON_WINDOWS_PROVISIONED


def windows_sandbox_state(
    *,
    platform: str = sys.platform,
    detection: Optional[SrtDetection] = None,
    provisioned_probe: Any = None,
    env: Optional[Mapping[str, str]] = None,
) -> WindowsSandboxState:
    """Compute the Windows provisioning verdict — the SINGLE source the consumers read (#977).

    Off-Windows → :data:`STATUS_NOT_WINDOWS` (the ladder uses its automatic per-process fence
    there; there is nothing to provision). On Windows: srt/node absent or below the validated
    floor → :data:`STATUS_SRT_ABSENT` with the exact install pointer (never a raw error); srt
    ready but the fence not provisioned → :data:`STATUS_UNPROVISIONED` (``clio sandbox setup``);
    fence provisioned → :data:`STATUS_PROVISIONED`.

    Every probe is injectable (``detection`` / ``provisioned_probe``) so the whole matrix is
    unit-pinnable against faked system state — the real elevation/provisioning is never touched.
    """
    if not platform.startswith("win"):
        return WindowsSandboxState(
            status=STATUS_NOT_WINDOWS,
            reason=REASON_NOT_WINDOWS,
            detail="Not Windows; the confinement ladder resolves an automatic per-process fence.",
            next_action="No action required on this platform.",
        )

    det = detection if detection is not None else detect_srt(env=env, platform=platform)
    from clio_agent.runtime.sandbox import REASON_SRT_DETECTED_DEFERRED  # noqa: PLC0415

    if det.reason != REASON_SRT_DETECTED_DEFERRED:
        # A precondition gap (srt/node absent or too old). Typed + guided, never a raw error.
        return WindowsSandboxState(
            status=STATUS_SRT_ABSENT,
            reason=det.reason,
            srt=det,
            detail=f"srt runtime not usable ({det.reason}); the Windows fence needs it.",
            next_action=_srt_absent_next_action(det.reason),
        )
    if not is_srt_version_supported(det.version):
        return WindowsSandboxState(
            status=STATUS_SRT_ABSENT,
            reason=REASON_SRT_VERSION_UNSUPPORTED,
            srt=det,
            detail=f"Installed {SRT_PACKAGE_NAME} v{det.version or '?'} is below the validated floor.",
            next_action=f"Upgrade srt: {SRT_INSTALL_POINTER}, then run `clio sandbox setup`.",
        )

    probe = provisioned_probe if provisioned_probe is not None else _default_provisioned_probe
    provisioned, reason = probe()
    if provisioned:
        return WindowsSandboxState(
            status=STATUS_PROVISIONED,
            reason=reason,
            srt=det,
            detail=f"Windows write fence provisioned (principal={SRT_WINDOWS_PRINCIPAL}).",
            next_action="No action required.",
        )
    return WindowsSandboxState(
        status=STATUS_UNPROVISIONED,
        reason=reason,
        srt=det,
        detail="srt is installed but the Windows write fence is not provisioned.",
        next_action="Run `clio sandbox setup` (one UAC prompt) to provision the Windows write fence.",
    )


def _srt_absent_next_action(reason: str) -> str:
    """The exact guided install pointer for a Windows srt precondition gap (never a raw error)."""
    from clio_agent.runtime.sandbox import REASON_SRT_NODE_MISSING  # noqa: PLC0415

    if reason == REASON_SRT_NODE_MISSING:
        return (
            "Install Node.js (>=20.11) from https://nodejs.org, then "
            f"`{SRT_INSTALL_POINTER}`, then run `clio sandbox setup`."
        )
    return f"Install srt: `{SRT_INSTALL_POINTER}`, then run `clio sandbox setup`."


# --------------------------------------------------------------------------- #
# The self-elevating provisioning step (guarded — win32 only, never unit-run).  #
# --------------------------------------------------------------------------- #


def _elevated_srt_windows_install(srt_binary: str) -> tuple[bool, str]:
    """Run ``srt windows-install`` under a SINGLE self-elevation (one UAC). GUARDED: win32 only.

    THE machine-mutating, UAC-popping step — provisions the ``srt-sandbox`` principal + WFP
    filters. It asserts ``sys.platform == 'win32'`` so it can never fire on Linux/macOS or from
    a unit test (which inject a fake ``installer`` instead). Uses ``ShellExecuteExW`` with the
    ``runas`` verb (the standard Windows self-elevation), waits for the elevated process, and
    reports its exit code. Returns ``(ok, detail)``; never raises for a non-zero exit.

    This is the owner-gated MANUAL LIVE GATE (`clio sandbox setup`) — not exercised by CI.
    """
    if sys.platform != "win32":  # pragma: no cover - guarded; never reached off win32
        raise RuntimeError(
            "srt windows-install self-elevation is win32-only (owner decision #974.2)"
        )
    import ctypes  # pragma: no cover - win32 live gate only
    from ctypes import wintypes  # pragma: no cover

    class _SHELLEXECUTEINFOW(ctypes.Structure):  # pragma: no cover - win32 live gate only
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess = 0x00000040
    # srt on Windows resolves via a .cmd shim; elevate cmd.exe running it so a shim works too.
    params = f'/c ""{srt_binary}" {SRT_WINDOWS_INSTALL_SUBCOMMAND}"'
    info = _SHELLEXECUTEINFOW()  # pragma: no cover
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess
    info.lpVerb = "runas"
    info.lpFile = "cmd.exe"
    info.lpParameters = params
    info.nShow = 0  # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):  # pragma: no cover
        err = ctypes.get_last_error()
        return False, f"self-elevation refused or failed (GetLastError={err})"
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)  # pragma: no cover
    code = wintypes.DWORD()  # pragma: no cover
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    if code.value != 0:  # pragma: no cover
        return False, f"srt windows-install exited {code.value}"
    return True, "srt windows-install completed under elevation"


def provision_windows_sandbox(
    *,
    state: Optional[WindowsSandboxState] = None,
    installer: Any = None,
    marker_writer: Any = None,
    state_reader: Any = None,
) -> WindowsProvisionResult:
    """Idempotently provision the Windows write fence (``clio sandbox setup``) (#977/B3).

    Flow (owner decision #974.2, single UAC + zero-prompt re-run):

    * off-Windows → typed no-op (:data:`STATUS_NOT_WINDOWS`); the ladder fences automatically;
    * srt absent/too old → typed guided reason + the install pointer, NO elevation attempted;
    * already provisioned → idempotent no-op (:data:`OUTCOME_ALREADY_PROVISIONED`), ZERO prompts;
    * otherwise → one self-elevating ``srt windows-install``, then persist the marker and
      RE-PROBE to verify (principal + marker) before claiming success.

    ``installer`` / ``marker_writer`` / ``state_reader`` are injectable so the whole flow is
    unit-pinnable with fakes; the real :func:`_elevated_srt_windows_install` (a UAC prompt) is
    NEVER called from tests. Returns a typed :class:`WindowsProvisionResult`.
    """
    st = state if state is not None else windows_sandbox_state()
    if st.status == STATUS_NOT_WINDOWS:
        return WindowsProvisionResult(
            ok=False,
            status=STATUS_NOT_WINDOWS,
            reason=REASON_NOT_WINDOWS,
            detail=st.detail,
            next_action=st.next_action,
        )
    if st.status == STATUS_SRT_ABSENT:
        # Precondition gap: guide the user, DO NOT elevate or attempt to install srt ourselves.
        return WindowsProvisionResult(
            ok=False,
            status=STATUS_SRT_ABSENT,
            reason=st.reason,
            detail=st.detail,
            next_action=st.next_action,
        )
    if st.status == STATUS_PROVISIONED:
        # Idempotent: already provisioned → no-op, zero prompts (safe to re-run on any channel).
        return WindowsProvisionResult(
            ok=True,
            status=OUTCOME_ALREADY_PROVISIONED,
            reason=REASON_ALREADY_PROVISIONED,
            detail=st.detail,
            next_action="No action required.",
        )

    # Unprovisioned + srt ready → the one-time self-elevating install.
    srt = st.srt
    binary = srt.binary_path if srt is not None else ""
    version = srt.version if srt is not None else ""
    install = installer if installer is not None else _elevated_srt_windows_install
    ok, detail = install(binary)
    if not ok:
        return WindowsProvisionResult(
            ok=False,
            status=OUTCOME_PROVISION_FAILED,
            reason=REASON_PROVISION_FAILED,
            detail=detail,
            next_action="Re-run `clio sandbox setup` and approve the UAC prompt.",
            elevated=True,
        )

    (marker_writer if marker_writer is not None else write_provision_marker)(version)
    reprobe = (state_reader if state_reader is not None else windows_sandbox_state)()
    if reprobe.status == STATUS_PROVISIONED:
        return WindowsProvisionResult(
            ok=True,
            status=OUTCOME_PROVISIONED,
            reason=REASON_WINDOWS_PROVISIONED,
            detail="Windows write fence provisioned (one-time UAC). Per-session use is unprivileged.",
            next_action="No action required.",
            elevated=True,
        )
    return WindowsProvisionResult(
        ok=False,
        status=OUTCOME_PROVISION_VERIFY_FAILED,
        reason=REASON_PROVISION_VERIFY_FAILED,
        detail=f"srt windows-install ran but the fence did not verify ({reprobe.reason}).",
        next_action="Re-run `clio sandbox setup`; if it persists, check the srt-sandbox account.",
        elevated=True,
    )


# --------------------------------------------------------------------------- #
# The CLI verb: `clio sandbox setup` / `clio sandbox status` (cli.py dispatches).#
# --------------------------------------------------------------------------- #


def run_sandbox_cli(action: Optional[str], *, json_output: bool = False) -> int:
    """Dispatch the ``sandbox`` CLI verb (``setup`` / ``status``). Returns the process exit code.

    ``ui/cli.py`` only parses argv and calls this (no accretion). ``status`` (the default)
    renders the ``sandbox`` doctor row standalone; ``setup`` runs the idempotent one-time
    provisioning. Both emit typed, guided output — never a raw traceback.
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
    """Run ``clio sandbox setup`` — the one-time Windows provisioning (typed, guided)."""
    if not sys.platform.startswith("win"):
        msg = (
            "No setup needed on this platform: the confinement ladder resolves an automatic "
            "per-process fence (srt/Landlock). `clio sandbox setup` provisions the one-time "
            "Windows write fence only."
        )
        _print(
            msg, json_output, payload={"status": STATUS_NOT_WINDOWS, "reason": REASON_NOT_WINDOWS}
        )
        return 0
    result = provision_windows_sandbox()
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
    "MECHANISM_SRT_WINDOWS",
    "REASON_SRT_NOT_INSTALLED",
    "SRT_INSTALL_POINTER",
    "SRT_WINDOWS_INSTALL_SUBCOMMAND",
    "SRT_WINDOWS_MARKER_NAME",
    "SRT_WINDOWS_PRINCIPAL",
    "STATUS_NOT_WINDOWS",
    "STATUS_PROVISIONED",
    "STATUS_SRT_ABSENT",
    "STATUS_UNPROVISIONED",
    "OUTCOME_ALREADY_PROVISIONED",
    "OUTCOME_PROVISIONED",
    "OUTCOME_PROVISION_FAILED",
    "OUTCOME_PROVISION_VERIFY_FAILED",
    "REASON_ALREADY_PROVISIONED",
    "REASON_NOT_WINDOWS",
    "REASON_PROVISION_FAILED",
    "REASON_PROVISION_VERIFY_FAILED",
    "REASON_WINDOWS_PROVISIONED",
    "REASON_WINDOWS_PROVISION_INCOMPLETE",
    "WindowsProvisionResult",
    "WindowsSandboxState",
    "provision_windows_sandbox",
    "run_sandbox_cli",
    "windows_sandbox_state",
    "write_provision_marker",
]
