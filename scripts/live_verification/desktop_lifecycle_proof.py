#!/usr/bin/env python3
"""Drive the INSTALLED CLIO desktop app (Tauri 2 + WebView2, Windows) through
its full lifecycle and record OS-level evidence, so a release candidate can be
proven without a human clicking through it (#B11).

The desktop app is launched with WebView2 remote debugging enabled
(``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<port>``),
which exposes the embedded shell's page over Chrome DevTools Protocol (CDP):
``_desktop_cdp.py`` drives clicks/keys via ``Runtime.evaluate``/
``Input.dispatchKeyEvent`` and captures ``Page.captureScreenshot`` evidence per
step. Process-level ground truth (owned tree, the bundled GACT server's
LISTEN port, teardown) comes from psutil, never from the app's own prose --
``_desktop_procs.py`` holds that decision logic, split into pure functions
tested directly against synthetic snapshots.

Steps (each writes ``NN-<step>.png`` + a JSON record under ``--out``):

  1. preflight     -- no clio-desktop.exe / clio-agent-*.exe / bundled
                       python.exe / (unless --allow-shared-core) clio_run.exe
                       already running.
  2. launch         -- boot with the debug port, wait for the CDP page + the
                       workspace UI, record the owned process tree and the
                       GACT server's port, confirm /v1/capabilities answers
                       (loopback-open, no bearer needed).
  3. close_escape   -- click the title bar's Close button, assert the "Keep
                       CLIO running?" dialog appears, press Escape, assert the
                       dialog is gone and the window/process are still there.
  4. close_keep_running -- Close -> "Keep running": assert the owned tree and
                       GACT port survive, and the window hides (CDP
                       ``document.visibilityState`` and/or Win32
                       ``IsWindowVisible``).
  5. relaunch       -- relaunch the exe; assert single-instance (no second
                       desktop pid) and the window re-shows.
  6. close_quit     -- Close -> "Quit CLIO": within (at least) 40s assert
                       every owned pid is gone, the GACT port is closed, the
                       runtime-client registry has no entry for the GACT
                       python pid, clio_run.exe is gone (unless
                       --allow-shared-core, in which case it must survive),
                       and no leftover PTY shells remain.
  7. report.json    -- steps, pids, ports, timings, screenshots; the process
                       exits non-zero if any step failed.

``--no-quit`` stops after step 4 (screenshot-only through "Keep running";
never drives the Quit CLIO path). Regardless of outcome, the script
best-effort tree-kills any leftover owned process on exit so a re-run never
trips the preflight conflict check.

Usage::

    uv run python scripts/live_verification/desktop_lifecycle_proof.py --help
    uv run python scripts/live_verification/desktop_lifecycle_proof.py
    uv run python scripts/live_verification/desktop_lifecycle_proof.py \\
        --app "D:\\CLIO Desktop\\clio-desktop.exe" --no-quit
    uv run python scripts/live_verification/desktop_lifecycle_proof.py \\
        --allow-shared-core --timeout 90

Report JSON: ``<out>/report.json`` (default out dir:
``out/live-verification/desktop_lifecycle_proof/<timestamp>/``).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as common  # noqa: E402
import _desktop_cdp as cdp  # noqa: E402
import _desktop_procs as procs  # noqa: E402

#: The custom title bar's close button (gact-tui, merged on main).
TITLE_BAR_CLOSE_SELECTOR = '[aria-label="Close"]'
#: The reui AlertDialog's heading text raised by a Close click.
DIALOG_TEXT = "Keep CLIO running?"
#: The dialog's two action buttons.
KEEP_RUNNING_BUTTON_TEXT = "Keep running"
QUIT_BUTTON_TEXT = "Quit CLIO"
#: Minimum wait budget for the Quit CLIO teardown (task requirement: "within 40s").
MIN_QUIT_TEARDOWN_TIMEOUT = 40.0


class LifecycleAssertionError(RuntimeError):
    """One step's assertion failed; carries whatever evidence was gathered."""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class RunState:
    """Mutable state threaded through the step functions as the run progresses."""

    app_path: Path
    debug_port: int
    timeout: float
    allow_shared_core: bool
    out_dir: Path
    desktop_proc: "subprocess.Popen[bytes] | None" = None
    session: cdp.CDPSession | None = None
    gact_pid: int | None = None
    gact_port: int | None = None
    owned_pids: list[int] = field(default_factory=list)
    ever_seen_pids: set[int] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# Report shape (pure -- unit-tested for shape)
# --------------------------------------------------------------------------- #
def build_report(
    *,
    app_path: str,
    debug_port: int,
    allow_shared_core: bool,
    no_quit: bool,
    out_dir: str,
) -> dict[str, Any]:
    """The ``report.json`` skeleton every run starts from."""

    return {
        "script": "desktop_lifecycle_proof",
        "app_path": app_path,
        "debug_port": debug_port,
        "allow_shared_core": allow_shared_core,
        "no_quit": no_quit,
        "out_dir": out_dir,
        "steps": [],
        "screenshots": [],
        "pass": False,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def default_app_path() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if not local_appdata:
        raise LifecycleAssertionError("LOCALAPPDATA is not set; pass --app explicitly")
    return Path(local_appdata) / "Programs" / "CLIO Desktop" / "clio-desktop.exe"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--app",
        default=None,
        help="Path to clio-desktop.exe "
        "(default: %%LOCALAPPDATA%%\\Programs\\CLIO Desktop\\clio-desktop.exe)",
    )
    parser.add_argument(
        "--debug-port",
        type=int,
        default=9223,
        help="WebView2 remote-debugging port (default: 9223)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory for screenshots + report.json "
        "(default: out/live-verification/desktop_lifecycle_proof/<timestamp>)",
    )
    parser.add_argument(
        "--allow-shared-core",
        action="store_true",
        help="Do not require clio_run.exe absent at preflight/teardown; assert it "
        "SURVIVES Quit CLIO instead of being torn down (another client holds it).",
    )
    parser.add_argument(
        "--no-quit",
        action="store_true",
        help="Stop after step 4 (Keep running); never drive the Quit CLIO path.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-wait timeout in seconds for most steps (default: 60). The final "
        f"Quit-CLIO teardown wait always uses at least {MIN_QUIT_TEARDOWN_TIMEOUT:g}s.",
    )
    return parser


# --------------------------------------------------------------------------- #
# Step 1: preflight
# --------------------------------------------------------------------------- #
def step_preflight(app_path: Path, *, allow_shared_core: bool) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "app_path_exists": app_path.is_file(),
        "allow_shared_core": allow_shared_core,
    }
    if not detail["app_path_exists"]:
        raise LifecycleAssertionError(f"app not found at {app_path}", detail=detail)

    processes = procs.list_all_processes()
    conflicts = procs.find_preflight_conflicts(
        processes, install_dir=str(app_path.parent), allow_shared_core=allow_shared_core
    )
    detail["conflicting_processes"] = [
        {"pid": p.pid, "name": p.name, "exe": p.exe} for p in conflicts
    ]
    if conflicts:
        raise LifecycleAssertionError(
            f"{len(conflicts)} CLIO process(es) already running before launch", detail=detail
        )
    return detail


# --------------------------------------------------------------------------- #
# Step 2: launch
# --------------------------------------------------------------------------- #
def _wait_for_cdp_page(debug_port: int, *, max_elapsed: float) -> dict[str, Any]:
    def _check() -> dict[str, Any] | None:
        try:
            targets = cdp.list_targets(debug_port)
        except requests.RequestException:
            return None
        return cdp.find_page_target(targets)

    target = common.expanding_wait(
        _check, what=f"CDP page target on port {debug_port}", max_elapsed=max_elapsed
    )
    if target is None:
        raise LifecycleAssertionError(
            f"no CDP page target appeared on port {debug_port} within {max_elapsed:g}s"
        )
    return target


def _capabilities_reachable(port: int, *, timeout: float) -> bool:
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/v1/capabilities", timeout=timeout)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def step_launch(state: RunState) -> dict[str, Any]:
    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={state.debug_port}"
    proc = subprocess.Popen([str(state.app_path)], env=env)  # noqa: S603 - fixed launcher path
    state.desktop_proc = proc
    state.ever_seen_pids.add(proc.pid)

    target = _wait_for_cdp_page(state.debug_port, max_elapsed=state.timeout)
    session = cdp.CDPSession(target["webSocketDebuggerUrl"])
    state.session = session
    session.send("Page.enable")
    session.send("Runtime.enable")

    detail: dict[str, Any] = {"desktop_pid": proc.pid, "cdp_target_id": target.get("id")}

    ready = common.expanding_wait(
        lambda: cdp.selector_present(session, TITLE_BAR_CLOSE_SELECTOR) or None,
        what=f"workspace UI ready ({TITLE_BAR_CLOSE_SELECTOR} present)",
        max_elapsed=state.timeout,
    )
    if not ready:
        raise LifecycleAssertionError("workspace UI never became ready", detail=detail)

    processes = procs.list_all_processes()
    owned = procs.owned_tree(proc.pid, processes)
    owned_pids = [p.pid for p in owned]
    state.owned_pids = owned_pids
    state.ever_seen_pids.update(owned_pids)
    detail["owned_processes"] = [{"pid": p.pid, "ppid": p.ppid, "name": p.name} for p in owned]

    gact_pid = procs.find_gact_server_pid(owned)
    state.gact_pid = gact_pid
    detail["gact_server_pid"] = gact_pid
    if gact_pid is None:
        raise LifecycleAssertionError(
            "bundled GACT python.exe child not found in owned tree", detail=detail
        )

    connections = procs.list_connections_for_pids([gact_pid])
    port = procs.discover_gact_port(connections, {gact_pid})
    state.gact_port = port
    detail["gact_port"] = port
    if port is None:
        raise LifecycleAssertionError("GACT server LISTEN port not discovered", detail=detail)

    capabilities_ok = _capabilities_reachable(port, timeout=min(state.timeout, 10.0))
    detail["capabilities_reachable"] = capabilities_ok
    if not capabilities_ok:
        raise LifecycleAssertionError(
            f"/v1/capabilities not reachable on port {port}", detail=detail
        )
    return detail


# --------------------------------------------------------------------------- #
# Steps 3-6 share the Close-button + dialog choreography
# --------------------------------------------------------------------------- #
def _click_close_and_wait_dialog(session: cdp.CDPSession, timeout: float) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "clicked_close": cdp.click_selector(session, TITLE_BAR_CLOSE_SELECTOR)
    }
    if not detail["clicked_close"]:
        raise LifecycleAssertionError(
            f"{TITLE_BAR_CLOSE_SELECTOR} not found to click", detail=detail
        )

    shown = common.expanding_wait(
        lambda: cdp.text_present(session, DIALOG_TEXT) or None,
        what="'Keep CLIO running?' dialog to appear",
        max_elapsed=timeout,
    )
    detail["dialog_shown"] = bool(shown)
    if not shown:
        raise LifecycleAssertionError("dialog never appeared after Close click", detail=detail)
    return detail


def step_close_dialog_escape(state: RunState) -> dict[str, Any]:
    assert state.session is not None and state.desktop_proc is not None
    session = state.session
    detail = _click_close_and_wait_dialog(session, state.timeout)

    cdp.press_escape(session)

    dismissed = common.expanding_wait(
        lambda: True if not cdp.text_present(session, DIALOG_TEXT) else None,
        what="dialog to dismiss after Escape",
        max_elapsed=state.timeout,
    )
    detail["dialog_dismissed"] = bool(dismissed)
    if not dismissed:
        raise LifecycleAssertionError("dialog still present after Escape", detail=detail)

    targets = cdp.list_targets(state.debug_port)
    window_open = cdp.find_page_target(targets) is not None
    process_alive = procs.is_process_alive(state.desktop_proc.pid)
    detail["window_open"] = window_open
    detail["process_alive"] = process_alive
    if not window_open or not process_alive:
        raise LifecycleAssertionError("window/process gone after Escape dismiss", detail=detail)
    return detail


def _visibility_signals(state: RunState) -> dict[str, Any]:
    assert state.session is not None and state.desktop_proc is not None
    return {
        "document_visibility_state": cdp.document_visibility_state(state.session),
        "win32_any_window_visible": cdp.any_window_visible_for_pid(state.desktop_proc.pid),
    }


def step_close_keep_running(state: RunState) -> dict[str, Any]:
    assert state.session is not None
    session = state.session
    detail = _click_close_and_wait_dialog(session, state.timeout)

    detail["clicked_keep_running"] = cdp.click_button_with_text(session, KEEP_RUNNING_BUTTON_TEXT)
    if not detail["clicked_keep_running"]:
        raise LifecycleAssertionError("'Keep running' button not found/clickable", detail=detail)

    def _hidden_check() -> bool | None:
        signals = _visibility_signals(state)
        detail["visibility_signals"] = signals
        hidden = (
            signals["document_visibility_state"] == "hidden"
            or signals["win32_any_window_visible"] is False
        )
        return True if hidden else None

    hidden = common.expanding_wait(
        _hidden_check,
        what="window to hide to the tray after Keep running",
        max_elapsed=state.timeout,
    )
    detail["window_hidden"] = bool(hidden)
    if not hidden:
        raise LifecycleAssertionError("window did not hide after Keep running", detail=detail)

    missing = [pid for pid in state.owned_pids if not procs.is_process_alive(pid)]
    detail["owned_pids_missing"] = missing
    if missing:
        raise LifecycleAssertionError(f"owned pids exited while hidden: {missing}", detail=detail)

    port_listening = state.gact_port is not None and not common.port_is_free(state.gact_port)
    detail["gact_port_listening"] = port_listening
    if not port_listening:
        raise LifecycleAssertionError("GACT port not listening while hidden", detail=detail)
    return detail


# --------------------------------------------------------------------------- #
# Step 5: relaunch -> single instance
# --------------------------------------------------------------------------- #
def step_relaunch_single_instance(state: RunState) -> dict[str, Any]:
    assert state.desktop_proc is not None
    detail: dict[str, Any] = {}

    relaunch_proc = subprocess.Popen([str(state.app_path)])  # noqa: S603 - fixed launcher path
    exited = common.expanding_wait(
        lambda: True if relaunch_proc.poll() is not None else None,
        what="relaunching process to exit (single-instance hand-off)",
        max_elapsed=state.timeout,
    )
    detail["relaunch_process_exited"] = bool(exited)
    detail["relaunch_exit_code"] = relaunch_proc.poll()
    if not exited:
        with contextlib.suppress(Exception):
            relaunch_proc.kill()
        raise LifecycleAssertionError(
            "relaunching process did not exit (single-instance hand-off failed)", detail=detail
        )

    desktop_pids = {
        p.pid for p in procs.find_by_name(procs.list_all_processes(), procs.DESKTOP_PROCESS_NAME)
    }
    detail["desktop_pids_after_relaunch"] = sorted(desktop_pids)
    if desktop_pids != {state.desktop_proc.pid}:
        raise LifecycleAssertionError(
            f"expected exactly one clio-desktop.exe ({state.desktop_proc.pid}); "
            f"found {sorted(desktop_pids)}",
            detail=detail,
        )

    def _visible_check() -> bool | None:
        signals = _visibility_signals(state)
        detail["visibility_signals"] = signals
        visible = (
            signals["document_visibility_state"] == "visible"
            or signals["win32_any_window_visible"] is True
        )
        return True if visible else None

    visible = common.expanding_wait(
        _visible_check, what="window to re-show after relaunch", max_elapsed=state.timeout
    )
    detail["window_visible"] = bool(visible)
    if not visible:
        raise LifecycleAssertionError(
            "window did not re-show after relaunch (single-instance)", detail=detail
        )
    return detail


# --------------------------------------------------------------------------- #
# Step 6: Close -> Quit CLIO
# --------------------------------------------------------------------------- #
def _registry_pids() -> set[int]:
    from clio_agent.arc.storage import (
        _client_registry_dir,  # noqa: PLC0415 - matches _common's lazy-import pattern
    )

    reg_dir = _client_registry_dir()
    if not reg_dir.is_dir():
        return set()
    return {int(entry.name) for entry in reg_dir.iterdir() if entry.name.isdigit()}


def _teardown_violations(state: RunState) -> list[str]:
    processes = procs.list_all_processes()
    alive_pids = {p.pid for p in processes}
    clio_run_matches = procs.find_by_name(processes, procs.CTE_DAEMON_PROCESS_NAME)
    clio_run_pid = clio_run_matches[0].pid if clio_run_matches else None
    leftovers = procs.find_leftover_pty_shells(processes, state.ever_seen_pids)
    check = procs.TeardownCheck(
        owned_pids=tuple(state.owned_pids),
        alive_pids=frozenset(alive_pids),
        python_pid=state.gact_pid if state.gact_pid is not None else -1,
        registry_pids=frozenset(_registry_pids()),
        clio_run_pid=clio_run_pid,
        allow_shared_core=state.allow_shared_core,
        leftover_pty_shells=tuple(p.name for p in leftovers),
    )
    return procs.assert_quit_teardown(check)


def step_close_quit(state: RunState) -> dict[str, Any]:
    assert state.session is not None
    session = state.session
    detail = _click_close_and_wait_dialog(session, state.timeout)

    detail["clicked_quit"] = cdp.click_button_with_text(session, QUIT_BUTTON_TEXT)
    if not detail["clicked_quit"]:
        raise LifecycleAssertionError("'Quit CLIO' button not found/clickable", detail=detail)

    last_violations: list[str] = []

    def _check() -> bool | None:
        nonlocal last_violations
        last_violations = _teardown_violations(state)
        return True if not last_violations else None

    converged = common.expanding_wait(
        _check,
        what="owned process tree + port + registry teardown after Quit CLIO",
        max_elapsed=max(state.timeout, MIN_QUIT_TEARDOWN_TIMEOUT),
    )
    detail["teardown_violations"] = last_violations
    if not converged:
        raise LifecycleAssertionError(
            f"teardown incomplete after Quit CLIO: {last_violations}", detail=detail
        )
    return detail


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _capture_step_screenshot(state: RunState, index: int, name: str) -> str | None:
    if state.session is None:
        return None
    try:
        png = state.session.capture_screenshot_png()
    except cdp.CDPError:
        return None
    path = state.out_dir / f"{index:02d}-{name}.png"
    path.write_bytes(png)
    return path.name


def _run_step(
    report: dict[str, Any],
    state: RunState,
    index: int,
    name: str,
    fn: Callable[[], dict[str, Any]],
) -> None:
    start = time.monotonic()
    try:
        detail = fn()
        ok = True
        error: str | None = None
    except LifecycleAssertionError as exc:
        detail = exc.detail
        ok = False
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - any escaping exception still becomes step evidence
        detail = {}
        ok = False
        error = f"{type(exc).__name__}: {exc}"
    duration = time.monotonic() - start
    screenshot = _capture_step_screenshot(state, index, name)
    record: dict[str, Any] = {
        "index": index,
        "name": name,
        "ok": ok,
        "duration_s": round(duration, 3),
        "detail": detail,
        "screenshot": screenshot,
    }
    if error is not None:
        record["error"] = error
    report["steps"].append(record)
    if screenshot is not None:
        report["screenshots"].append(screenshot)
    if not ok:
        raise LifecycleAssertionError(error or f"step {name!r} failed", detail=detail)


def _best_effort_cleanup(state: RunState) -> None:
    """Never leaves an owned process behind, whatever the run's outcome."""

    if state.session is not None:
        state.session.close()
    if state.desktop_proc is not None and procs.is_process_alive(state.desktop_proc.pid):
        try:
            from clio_agent.serve import _terminate_tree  # noqa: PLC0415 - lazy, matches _common

            _terminate_tree(state.desktop_proc.pid, record_create_time=None, trusted=True)
        except Exception:  # noqa: BLE001 - best-effort teardown must never raise
            with contextlib.suppress(Exception):
                state.desktop_proc.kill()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        app_path = Path(args.app) if args.app else default_app_path()
    except LifecycleAssertionError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - parser.error() already exits

    out_dir = (
        Path(args.out)
        if args.out
        else common.OUT_ROOT
        / "desktop_lifecycle_proof"
        / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    report = build_report(
        app_path=str(app_path),
        debug_port=args.debug_port,
        allow_shared_core=args.allow_shared_core,
        no_quit=args.no_quit,
        out_dir=str(out_dir),
    )
    state = RunState(
        app_path=app_path,
        debug_port=args.debug_port,
        timeout=args.timeout,
        allow_shared_core=args.allow_shared_core,
        out_dir=out_dir,
    )

    step_defs: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("preflight", lambda: step_preflight(app_path, allow_shared_core=args.allow_shared_core)),
        ("launch", lambda: step_launch(state)),
        ("close_escape", lambda: step_close_dialog_escape(state)),
        ("close_keep_running", lambda: step_close_keep_running(state)),
        ("relaunch_single_instance", lambda: step_relaunch_single_instance(state)),
    ]
    if not args.no_quit:
        step_defs.append(("close_quit", lambda: step_close_quit(state)))

    exit_code = 0
    try:
        for index, (name, fn) in enumerate(step_defs, start=1):
            _run_step(report, state, index, name, fn)
    except LifecycleAssertionError as exc:
        exit_code = 1
        report["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced in report, never a bare crash with no evidence
        exit_code = 1
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _best_effort_cleanup(state)
        report["pass"] = exit_code == 0
        common.write_verdict(out_dir / "report.json", report)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
