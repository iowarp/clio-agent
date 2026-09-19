"""Focused contract tests for the desktop lifecycle proof (#B11).

``scripts/live_verification/desktop_lifecycle_proof.py`` drives the INSTALLED
CLIO desktop app over CDP + psutil -- a real run needs the Windows-installed
app, WebView2, and a live process tree, none of which are available (or
desirable) here. What IS unit-testable, and what this file covers, is
everything the script's own docstring calls out as pure: owned-tree
filtering, GACT port discovery, preflight conflict detection, and the Quit
CLIO teardown assertion logic (all in ``_desktop_procs.py``); the CDP
JS-snippet builders and the message-correlation half of ``CDPSession.send``
(``_desktop_cdp.py``, with a fake websocket/session standing in for a real
CDP transport); and the orchestration script's own report shape, argument
handling, per-step bookkeeping (``_run_step``), and a couple of full step
functions driven end-to-end with every ``cdp``/``procs`` call mocked.

Two distinct module-loading styles are used deliberately, and never mixed
within one test: ``_desktop_procs.py`` and ``_desktop_cdp.py`` have no
sibling flat-imports, so they are imported the normal (dotted, cached) way;
``desktop_lifecycle_proof.py`` flat-imports ``_common``/``_desktop_cdp``/
``_desktop_procs`` as siblings (the package's existing convention -- see
``_common.py``'s own ``sys.path.insert`` + ``import _common as common``), so
it is loaded via ``importlib.util.spec_from_file_location`` with a temporary
``sys.path`` insertion, exactly like ``test_leg_b_web_fetch.py::_load_leg``.
Reaching into its internals through ``mod.cdp`` / ``mod.procs`` (rather than
importing ``_desktop_cdp``/``_desktop_procs`` separately) keeps every
monkeypatch pointed at the SAME module object the script itself calls.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
import types
from collections import namedtuple
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import scripts.live_verification._desktop_cdp as cdp_mod
import scripts.live_verification._desktop_procs as procs_mod

# --------------------------------------------------------------------------- #
# Loader for the orchestration script (flat-import convention; see the module
# docstring above for why this differs from the two dotted imports).
# --------------------------------------------------------------------------- #
_SCRIPT_DIR = Path(__file__).parents[2] / "scripts" / "live_verification"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "desktop_lifecycle_proof", _SCRIPT_DIR / "desktop_lifecycle_proof.py"
    )
    assert spec is not None and spec.loader is not None
    sys.path.insert(0, str(_SCRIPT_DIR))
    try:
        module = importlib.util.module_from_spec(spec)
        # Registered in sys.modules (the standard importlib recipe) before exec:
        # the module defines `@dataclass` classes under `from __future__ import
        # annotations`, and dataclasses resolves string annotations via
        # `sys.modules[cls.__module__].__dict__` -- an unregistered module makes
        # that lookup return None and crashes the class definition itself.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_SCRIPT_DIR))


# ============================================================================
# _desktop_procs.py -- pure owned-tree / port-discovery / teardown logic
# ============================================================================
def test_descendant_pids_walks_multi_level_tree_excluding_unrelated_processes() -> None:
    P = procs_mod.ProcSnapshot
    processes = [
        P(pid=1, ppid=0, name="clio-desktop.exe"),
        P(pid=2, ppid=1, name="clio-agent-launcher.exe"),
        P(pid=3, ppid=2, name="python.exe"),
        P(pid=99, ppid=0, name="unrelated.exe"),
    ]
    assert procs_mod.descendant_pids(1, processes) == [2, 3]


def test_descendant_pids_survives_a_root_pid_absent_from_the_snapshot() -> None:
    P = procs_mod.ProcSnapshot
    processes = [P(pid=2, ppid=1, name="python.exe")]
    assert procs_mod.descendant_pids(1, processes) == [2]


def test_owned_tree_includes_root_first_then_descendants() -> None:
    P = procs_mod.ProcSnapshot
    processes = [
        P(pid=1, ppid=0, name="clio-desktop.exe"),
        P(pid=2, ppid=1, name="python.exe"),
    ]
    tree = procs_mod.owned_tree(1, processes)
    assert [p.pid for p in tree] == [1, 2]


def test_owned_tree_is_empty_when_the_root_pid_is_unknown() -> None:
    assert procs_mod.owned_tree(404, []) == []


def test_find_preflight_conflicts_flags_desktop_launcher_installed_python_and_cte() -> None:
    P = procs_mod.ProcSnapshot
    install_dir = "D:/Programs/CLIO Desktop"
    processes = [
        P(pid=1, ppid=0, name="clio-desktop.exe"),
        P(pid=2, ppid=0, name="clio-agent-2.3.1.exe"),
        P(pid=3, ppid=0, name="python.exe", exe=install_dir + "/python.exe"),
        P(pid=4, ppid=0, name="python.exe", exe="C:/Users/dev/venv/python.exe"),
        P(pid=5, ppid=0, name="clio_run.exe"),
        P(pid=6, ppid=0, name="notepad.exe"),
    ]
    conflicts = procs_mod.find_preflight_conflicts(
        processes, install_dir=install_dir, allow_shared_core=False
    )
    assert {p.pid for p in conflicts} == {1, 2, 3, 5}


def test_find_preflight_conflicts_allows_clio_run_when_shared_core_is_allowed() -> None:
    processes = [procs_mod.ProcSnapshot(pid=5, ppid=0, name="clio_run.exe")]
    conflicts = procs_mod.find_preflight_conflicts(
        processes, install_dir="D:/Programs/CLIO Desktop", allow_shared_core=True
    )
    assert conflicts == []


def test_find_preflight_conflicts_ignores_an_unrelated_python_outside_install_dir() -> None:
    processes = [procs_mod.ProcSnapshot(pid=4, ppid=0, name="python.exe", exe="C:/dev/python.exe")]
    conflicts = procs_mod.find_preflight_conflicts(
        processes, install_dir="D:/Programs/CLIO Desktop", allow_shared_core=False
    )
    assert conflicts == []


def test_is_gact_server_process_matches_only_the_bundled_module_invocation() -> None:
    assert procs_mod.is_gact_server_process(
        "python.exe", ["python.exe", "-m", "clio_agent.gact", "--no-agent"]
    )
    assert not procs_mod.is_gact_server_process("python.exe", ["python.exe", "-m", "pip", "list"])
    assert not procs_mod.is_gact_server_process("node.exe", ["node.exe", "-m", "clio_agent.gact"])


def test_find_gact_server_pid_returns_the_match_or_none() -> None:
    P = procs_mod.ProcSnapshot
    processes = [
        P(pid=1, ppid=0, name="python.exe", cmdline=("python.exe", "-m", "pip")),
        P(
            pid=2,
            ppid=0,
            name="python.exe",
            cmdline=("python.exe", "-m", "clio_agent.gact", "--no-agent"),
        ),
    ]
    assert procs_mod.find_gact_server_pid(processes) == 2
    assert procs_mod.find_gact_server_pid(processes[:1]) is None


def test_discover_gact_port_returns_the_first_loopback_listen_port_owned_by_pid() -> None:
    C = procs_mod.ConnSnapshot
    conns = [
        C(pid=99, status="LISTEN", ip="127.0.0.1", port=1111),  # not owned
        C(pid=42, status="ESTABLISHED", ip="127.0.0.1", port=2222),  # not LISTEN
        C(pid=42, status="LISTEN", ip="0.0.0.0", port=3333),  # not loopback
        C(pid=42, status="LISTEN", ip="127.0.0.1", port=54321),  # the match
    ]
    assert procs_mod.discover_gact_port(conns, {42}) == 54321


def test_discover_gact_port_returns_none_without_a_match() -> None:
    assert procs_mod.discover_gact_port([], {42}) is None


def test_find_leftover_pty_shells_only_flags_shells_parented_by_ever_seen_pids() -> None:
    P = procs_mod.ProcSnapshot
    processes = [
        P(pid=10, ppid=2, name="pwsh.exe"),
        P(pid=11, ppid=999, name="powershell.exe"),  # unrelated parent
        P(pid=12, ppid=2, name="notepad.exe"),  # not a shell
    ]
    leftovers = procs_mod.find_leftover_pty_shells(processes, ever_seen_pids={1, 2, 3})
    assert [p.pid for p in leftovers] == [10]


def _teardown_check(**overrides: Any) -> procs_mod.TeardownCheck:
    base: dict[str, Any] = {
        "owned_pids": (2, 3),
        "alive_pids": frozenset(),
        "python_pid": 3,
        "registry_pids": frozenset(),
        "clio_run_pid": None,
        "allow_shared_core": False,
    }
    base.update(overrides)
    return procs_mod.TeardownCheck(**base)


def test_assert_quit_teardown_passes_on_a_fully_torn_down_snapshot() -> None:
    assert procs_mod.assert_quit_teardown(_teardown_check()) == []


def test_assert_quit_teardown_flags_a_surviving_owned_pid() -> None:
    violations = procs_mod.assert_quit_teardown(_teardown_check(alive_pids=frozenset({2})))
    assert any("pid 2" in v for v in violations)


def test_assert_quit_teardown_flags_a_registry_entry_for_the_python_pid() -> None:
    violations = procs_mod.assert_quit_teardown(_teardown_check(registry_pids=frozenset({3})))
    assert any("registry" in v for v in violations)


def test_assert_quit_teardown_requires_clio_run_gone_without_allow_shared_core() -> None:
    violations = procs_mod.assert_quit_teardown(
        _teardown_check(clio_run_pid=77, alive_pids=frozenset({77}))
    )
    assert any("clio_run.exe is still alive" in v for v in violations)


def test_assert_quit_teardown_requires_clio_run_survive_under_allow_shared_core() -> None:
    violations = procs_mod.assert_quit_teardown(
        _teardown_check(allow_shared_core=True, clio_run_pid=77, alive_pids=frozenset())
    )
    assert any("exited despite --allow-shared-core" in v for v in violations)


def test_assert_quit_teardown_passes_when_clio_run_survives_under_allow_shared_core() -> None:
    violations = procs_mod.assert_quit_teardown(
        _teardown_check(allow_shared_core=True, clio_run_pid=77, alive_pids=frozenset({77}))
    )
    assert violations == []


def test_assert_quit_teardown_flags_leftover_pty_shells() -> None:
    violations = procs_mod.assert_quit_teardown(_teardown_check(leftover_pty_shells=("pwsh.exe",)))
    assert any("leftover PTY shell: pwsh.exe" in v for v in violations)


def test_laddr_ip_port_normalizes_a_namedtuple_like_and_a_plain_tuple() -> None:
    Addr = namedtuple("Addr", ["ip", "port"])
    assert procs_mod._laddr_ip_port(Addr("127.0.0.1", 8080)) == ("127.0.0.1", 8080)
    assert procs_mod._laddr_ip_port(("127.0.0.1", 8080)) == ("127.0.0.1", 8080)
    assert procs_mod._laddr_ip_port(None) is None
    assert procs_mod._laddr_ip_port(()) is None


# ============================================================================
# _desktop_cdp.py -- pure JS-snippet builders + CDPSession's message
# correlation logic (a fake session/websocket stands in for a real CDP peer)
# ============================================================================
def test_find_page_target_picks_the_first_page_with_a_websocket_url() -> None:
    targets = [
        {"type": "background_page", "webSocketDebuggerUrl": "ws://x"},
        {"type": "page", "webSocketDebuggerUrl": ""},
        {"type": "page", "webSocketDebuggerUrl": "ws://real"},
    ]
    target = cdp_mod.find_page_target(targets)
    assert target is not None
    assert target["webSocketDebuggerUrl"] == "ws://real"


def test_find_page_target_returns_none_when_no_page_target_exists() -> None:
    assert cdp_mod.find_page_target([{"type": "worker"}]) is None


class _RecordingEvaluateSession:
    """Stands in for ``CDPSession``: records the JS it was asked to evaluate."""

    def __init__(self, value: Any) -> None:
        self.calls: list[str] = []
        self._value = value

    def evaluate(self, expression: str, *, timeout: float = 15.0) -> Any:
        self.calls.append(expression)
        return self._value


def test_click_selector_embeds_the_selector_as_a_json_literal_and_returns_result() -> None:
    session = _RecordingEvaluateSession(True)
    assert cdp_mod.click_selector(session, '[aria-label="Close"]') is True
    assert json.dumps('[aria-label="Close"]') in session.calls[0]


def test_click_button_with_text_embeds_the_text_and_returns_result() -> None:
    session = _RecordingEvaluateSession(False)
    assert cdp_mod.click_button_with_text(session, "Quit CLIO") is False
    assert json.dumps("Quit CLIO") in session.calls[0]


def test_selector_present_wraps_query_selector_not_null() -> None:
    session = _RecordingEvaluateSession(True)
    assert cdp_mod.selector_present(session, "#root") is True
    assert "document.querySelector" in session.calls[0]
    assert "!== null" in session.calls[0]


def test_text_present_checks_body_innertext_includes() -> None:
    session = _RecordingEvaluateSession(True)
    assert cdp_mod.text_present(session, "Keep CLIO running?") is True
    assert "innerText.includes" in session.calls[0]
    assert json.dumps("Keep CLIO running?") in session.calls[0]


def test_document_visibility_state_returns_the_evaluated_string() -> None:
    session = _RecordingEvaluateSession("hidden")
    assert cdp_mod.document_visibility_state(session) == "hidden"


class _FakeWebSocket:
    """A canned sequence of inbound frames for ``CDPSession.send``'s recv loop."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self, timeout: float | None = None) -> str:
        if not self._frames:
            raise TimeoutError("no more frames")
        return self._frames.pop(0)

    def close(self) -> None:
        pass


def _bare_session(frames: list[str]) -> tuple[cdp_mod.CDPSession, _FakeWebSocket]:
    """A ``CDPSession`` with its real websocket swapped for :class:`_FakeWebSocket`.

    Returns both the session and the fake transport (rather than exposing the
    fake through ``session._ws``): ``CDPSession.__init__`` types that attribute
    as the real ``websockets`` ``ClientConnection``, so a caller wanting the
    fake's recorded ``.sent`` frames keeps its own reference instead of
    reading a private attribute mypy would (rightly) reject as the wrong type.
    """

    session = object.__new__(cdp_mod.CDPSession)
    session._next_id = itertools.count(1)  # noqa: SLF001 - test constructs the object directly
    session.events = []
    fake_ws = _FakeWebSocket(frames)
    session._ws = fake_ws  # type: ignore[assignment]  # noqa: SLF001 - fake transport for tests
    return session, fake_ws


def test_press_escape_dispatches_a_keydown_then_keyup_with_vk_escape() -> None:
    session, fake_ws = _bare_session(
        [json.dumps({"id": 1, "result": {}}), json.dumps({"id": 2, "result": {}})]
    )
    cdp_mod.press_escape(session)
    sent = [json.loads(raw) for raw in fake_ws.sent]
    assert [frame["method"] for frame in sent] == [
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
    ]
    event_types = [frame["params"]["type"] for frame in sent]
    assert event_types == ["keyDown", "keyUp"]
    assert all(
        frame["params"]["key"] == "Escape"
        and frame["params"]["windowsVirtualKeyCode"] == cdp_mod.VK_ESCAPE
        for frame in sent
    )


def test_cdpsession_send_correlates_the_reply_by_id_and_drains_unrelated_events() -> None:
    session, fake_ws = _bare_session(
        [
            json.dumps({"method": "Page.someEvent", "params": {}}),
            json.dumps({"id": 1, "result": {"ok": True}}),
        ]
    )
    result = session.send("Page.enable")
    assert result == {"ok": True}
    assert session.events == [{"method": "Page.someEvent", "params": {}}]
    assert json.loads(fake_ws.sent[0])["method"] == "Page.enable"


def test_cdpsession_send_raises_cdperror_on_an_error_reply() -> None:
    session, _fake_ws = _bare_session([json.dumps({"id": 1, "error": {"message": "boom"}})])
    with pytest.raises(cdp_mod.CDPError):
        session.send("Runtime.evaluate")


def test_cdpsession_send_raises_cdperror_on_timeout() -> None:
    session, _fake_ws = _bare_session([])
    with pytest.raises(cdp_mod.CDPError):
        session.send("Page.enable", timeout=0.01)


def test_cdpsession_evaluate_raises_on_exception_details() -> None:
    session, _fake_ws = _bare_session(
        [json.dumps({"id": 1, "result": {"exceptionDetails": {"text": "boom"}}})]
    )
    with pytest.raises(cdp_mod.CDPError):
        session.evaluate("1 + 1")


def test_cdpsession_evaluate_returns_the_wire_value() -> None:
    session, _fake_ws = _bare_session([json.dumps({"id": 1, "result": {"result": {"value": 42}}})])
    assert session.evaluate("40 + 2") == 42


# ============================================================================
# desktop_lifecycle_proof.py -- report shape, CLI, and step functions
# ============================================================================
def test_build_report_has_the_expected_shape() -> None:
    mod = _load_script()
    report = mod.build_report(
        app_path="C:/CLIO Desktop/clio-desktop.exe",
        debug_port=9223,
        allow_shared_core=False,
        no_quit=True,
        out_dir="C:/out",
    )
    assert report == {
        "script": "desktop_lifecycle_proof",
        "app_path": "C:/CLIO Desktop/clio-desktop.exe",
        "debug_port": 9223,
        "allow_shared_core": False,
        "no_quit": True,
        "out_dir": "C:/out",
        "steps": [],
        "screenshots": [],
        "pass": False,
    }


def test_default_app_path_derives_from_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_script()
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/tester/AppData/Local")
    path = mod.default_app_path()
    assert (
        path
        == Path("C:/Users/tester/AppData/Local") / "Programs" / "CLIO Desktop" / "clio-desktop.exe"
    )


def test_default_app_path_raises_without_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_script()
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(mod.LifecycleAssertionError):
        mod.default_app_path()


def test_step_preflight_passes_when_the_app_exists_and_nothing_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_script()
    app_path = tmp_path / "clio-desktop.exe"
    app_path.write_bytes(b"")
    monkeypatch.setattr(mod.procs, "list_all_processes", lambda: [])
    detail = mod.step_preflight(app_path, allow_shared_core=False)
    assert detail["app_path_exists"] is True
    assert detail["conflicting_processes"] == []


def test_step_preflight_raises_when_the_app_binary_is_missing(tmp_path: Path) -> None:
    mod = _load_script()
    with pytest.raises(mod.LifecycleAssertionError) as exc_info:
        mod.step_preflight(tmp_path / "missing.exe", allow_shared_core=False)
    assert exc_info.value.detail["app_path_exists"] is False


def test_step_preflight_raises_when_a_conflicting_process_is_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_script()
    app_path = tmp_path / "clio-desktop.exe"
    app_path.write_bytes(b"")
    conflict = mod.procs.ProcSnapshot(pid=1, ppid=0, name="clio-desktop.exe")
    monkeypatch.setattr(mod.procs, "list_all_processes", lambda: [conflict])
    with pytest.raises(mod.LifecycleAssertionError) as exc_info:
        mod.step_preflight(app_path, allow_shared_core=False)
    assert exc_info.value.detail["conflicting_processes"] == [
        {"pid": 1, "name": "clio-desktop.exe", "exe": ""}
    ]


def _run_state(tmp_path: Path, **overrides: Any) -> Any:
    mod = _load_script()
    kwargs = {
        "app_path": tmp_path / "app.exe",
        "debug_port": 9223,
        "timeout": 0.0,
        "allow_shared_core": False,
        "out_dir": tmp_path,
    }
    kwargs.update(overrides)
    return mod, mod.RunState(**kwargs)


def test_run_step_records_success_and_writes_a_screenshot(tmp_path: Path) -> None:
    mod, state = _run_state(tmp_path)
    state.session = types.SimpleNamespace(capture_screenshot_png=lambda: b"PNGDATA")
    report = mod.build_report(
        app_path="x", debug_port=9223, allow_shared_core=False, no_quit=False, out_dir=str(tmp_path)
    )
    mod._run_step(report, state, 2, "launch", lambda: {"ok": True})
    assert report["steps"][0]["ok"] is True
    assert report["steps"][0]["screenshot"] == "02-launch.png"
    assert (tmp_path / "02-launch.png").read_bytes() == b"PNGDATA"
    assert report["screenshots"] == ["02-launch.png"]


def test_run_step_records_failure_and_reraises(tmp_path: Path) -> None:
    mod, state = _run_state(tmp_path)

    def _boom() -> dict[str, Any]:
        raise mod.LifecycleAssertionError("nope", detail={"x": 1})

    report = mod.build_report(
        app_path="x", debug_port=9223, allow_shared_core=False, no_quit=False, out_dir=str(tmp_path)
    )
    with pytest.raises(mod.LifecycleAssertionError):
        mod._run_step(report, state, 1, "preflight", _boom)
    assert report["steps"][0]["ok"] is False
    assert report["steps"][0]["error"] == "nope"
    assert report["steps"][0]["detail"] == {"x": 1}
    assert report["screenshots"] == []


def test_run_step_converts_an_unexpected_exception_into_a_failed_step(tmp_path: Path) -> None:
    """A raw exception escaping a step (e.g. an un-wrapped CDPError) still becomes
    step evidence + a non-zero exit, instead of vanishing from ``report["steps"]``."""

    mod, state = _run_state(tmp_path)

    def _boom() -> dict[str, Any]:
        raise mod.cdp.CDPError("Runtime.evaluate timed out after 15s")

    report = mod.build_report(
        app_path="x", debug_port=9223, allow_shared_core=False, no_quit=False, out_dir=str(tmp_path)
    )
    with pytest.raises(mod.LifecycleAssertionError):
        mod._run_step(report, state, 3, "close_escape", _boom)
    assert report["steps"][0]["ok"] is False
    assert "CDPError" in report["steps"][0]["error"]
    assert report["steps"][0]["detail"] == {}


def test_run_step_skips_the_screenshot_when_no_session_is_open(tmp_path: Path) -> None:
    mod, state = _run_state(tmp_path)
    report = mod.build_report(
        app_path="x", debug_port=9223, allow_shared_core=False, no_quit=False, out_dir=str(tmp_path)
    )
    mod._run_step(report, state, 1, "preflight", lambda: {"ok": True})
    assert report["steps"][0]["screenshot"] is None
    assert report["screenshots"] == []


def test_run_step_treats_a_screenshot_capture_failure_as_non_fatal(tmp_path: Path) -> None:
    mod, state = _run_state(tmp_path)

    class _RaisingSession:
        def capture_screenshot_png(self) -> bytes:
            raise mod.cdp.CDPError("boom")

    state.session = _RaisingSession()
    report = mod.build_report(
        app_path="x", debug_port=9223, allow_shared_core=False, no_quit=False, out_dir=str(tmp_path)
    )
    mod._run_step(report, state, 1, "launch", lambda: {"ok": True})
    assert report["steps"][0]["ok"] is True
    assert report["steps"][0]["screenshot"] is None


def test_click_close_and_wait_dialog_raises_when_the_selector_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_script()
    monkeypatch.setattr(mod.cdp, "click_selector", lambda session, selector: False)
    with pytest.raises(mod.LifecycleAssertionError) as exc_info:
        mod._click_close_and_wait_dialog(object(), 0.0)
    assert exc_info.value.detail["clicked_close"] is False


def test_click_close_and_wait_dialog_raises_when_the_dialog_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_script()
    monkeypatch.setattr(mod.cdp, "click_selector", lambda session, selector: True)
    monkeypatch.setattr(mod.cdp, "text_present", lambda session, text: False)
    with pytest.raises(mod.LifecycleAssertionError) as exc_info:
        mod._click_close_and_wait_dialog(object(), 0.0)
    assert exc_info.value.detail["dialog_shown"] is False


def test_click_close_and_wait_dialog_succeeds_when_the_dialog_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_script()
    monkeypatch.setattr(mod.cdp, "click_selector", lambda session, selector: True)
    monkeypatch.setattr(mod.cdp, "text_present", lambda session, text: True)
    detail = mod._click_close_and_wait_dialog(object(), 0.0)
    assert detail == {"clicked_close": True, "dialog_shown": True}


def test_step_close_dialog_escape_happy_path_with_every_cdp_call_mocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, state = _run_state(tmp_path)
    state.session = object()
    state.desktop_proc = types.SimpleNamespace(pid=4242)

    dialog_present = {"value": True}
    monkeypatch.setattr(mod.cdp, "click_selector", lambda session, selector: True)
    monkeypatch.setattr(mod.cdp, "text_present", lambda session, text: dialog_present["value"])
    monkeypatch.setattr(
        mod.cdp, "press_escape", lambda session: dialog_present.__setitem__("value", False)
    )
    monkeypatch.setattr(
        mod.cdp,
        "list_targets",
        lambda debug_port: [{"type": "page", "webSocketDebuggerUrl": "ws://x"}],
    )
    monkeypatch.setattr(mod.procs, "is_process_alive", lambda pid: True)

    detail = mod.step_close_dialog_escape(state)
    assert detail == {
        "clicked_close": True,
        "dialog_shown": True,
        "dialog_dismissed": True,
        "window_open": True,
        "process_alive": True,
    }


def test_teardown_violations_wires_process_and_registry_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, state = _run_state(tmp_path)
    state.owned_pids = [11]
    state.gact_pid = 11
    state.ever_seen_pids = {1, 11}

    still_alive = mod.procs.ProcSnapshot(pid=11, ppid=1, name="python.exe")
    monkeypatch.setattr(mod.procs, "list_all_processes", lambda: [still_alive])
    monkeypatch.setattr(mod, "_registry_pids", lambda: {11})

    violations = mod._teardown_violations(state)
    assert any("pid 11" in v for v in violations)
    assert any("registry" in v for v in violations)


def test_registry_pids_reads_numeric_entries_from_the_client_registry_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_script()
    reg_dir = tmp_path / "clio-runtime.clients"
    reg_dir.mkdir()
    (reg_dir / "1234").write_text("", encoding="utf-8")
    (reg_dir / "not-a-pid").write_text("", encoding="utf-8")

    from clio_agent.arc import storage

    monkeypatch.setattr(storage, "_client_registry_dir", lambda: reg_dir)
    assert mod._registry_pids() == {1234}


def test_registry_pids_returns_an_empty_set_when_the_directory_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_script()
    from clio_agent.arc import storage

    monkeypatch.setattr(storage, "_client_registry_dir", lambda: tmp_path / "does-not-exist")
    assert mod._registry_pids() == set()
