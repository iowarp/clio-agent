"""iowarp/clio-agent#20: user-defined hooks subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.runtime.hooks import HookRegistry, build_hook_registry, install_global_registry


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def forward(self, question: str, session_id: str = "default"):
        self.calls.append(question)
        return _Pred()


def _hook_dir(tmp_path: Path, **events: str) -> Path:
    """Write one hook file per event into ``tmp_path/hooks``;
    returns the directory."""

    d = tmp_path / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    for event, body in events.items():
        (d / f"{event}.py").write_text(body)
    return d


def test_pre_tool_hook_can_block(tmp_path: Path) -> None:
    """A pre_tool hook that raises PermissionError vetoes the call."""

    d = _hook_dir(
        tmp_path,
        pre_tool="""
def pre_tool(name, args):
    if name.startswith("hdf5_"):
        raise PermissionError("no hdf5 today")
""",
    )
    reg = HookRegistry(hooks_dir=d)
    with pytest.raises(PermissionError, match="no hdf5"):
        reg.fire("pre_tool", "hdf5_list_datasets", {"path": "/tmp/x"})
    # A non-matching tool passes.
    reg.fire("pre_tool", "fs_read_file", {"path": "/tmp/x"})


def test_post_tool_hook_swallows_exceptions(tmp_path: Path) -> None:
    """post_* hooks must NEVER crash a turn; exceptions are
    swallowed + logged."""

    d = _hook_dir(
        tmp_path,
        post_tool="""
def post_tool(name, args, result=None, error=None):
    raise RuntimeError("boom")
""",
    )
    reg = HookRegistry(hooks_dir=d)
    # Should not raise.
    reg.fire("post_tool", "fs_read_file", {"x": 1}, result="ok")


def test_pre_message_hook_blocks_via_app(tmp_path: Path) -> None:
    """A pre_message hook raising PermissionError blocks the turn
    end-to-end through GACT — the assistant message comes back
    with error_info.error == permission_error."""

    d = _hook_dir(
        tmp_path,
        pre_message="""
def pre_message(session_id, text):
    if "secret" in text.lower():
        raise PermissionError("blocked by policy")
""",
    )
    install_global_registry(HookRegistry(hooks_dir=d))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            ack = c.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "tell me a secret"}]},
            )
            assert ack.status_code == 200
            # Wait for the background turn to settle into error.
            import time as _t

            for _ in range(30):
                sess = c.get(f"/v1/sessions/{sid}").json()
                if sess["status"] == "error":
                    break
                _t.sleep(0.05)
            assert sess["status"] == "error"
    finally:
        install_global_registry(None)


def test_post_message_hook_runs_after_settle(tmp_path: Path) -> None:
    """post_message hooks see the assistant message + can side-effect
    (write a marker file here)."""

    marker = tmp_path / "post_message_fired.txt"
    d = _hook_dir(
        tmp_path,
        post_message=f"""
def post_message(session_id, assistant):
    open({str(marker)!r}, "w").write(assistant['id'])
""",
    )
    install_global_registry(HookRegistry(hooks_dir=d))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            from .conftest import complete_turn

            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            assistant = complete_turn(c, sid, "hello")
            # Marker exists + contains the assistant's id.
            assert marker.exists()
            assert marker.read_text() == assistant["id"]
    finally:
        install_global_registry(None)


def test_no_hooks_dir_is_no_op(tmp_path: Path) -> None:
    """Missing hooks dir is fine; fire returns []."""

    reg = HookRegistry(hooks_dir=tmp_path / "nothing")
    assert reg.fire("pre_tool", "x", {}) == []


def test_hook_registry_factory_uses_configured_local_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "configured_hook.txt"
    hooks_dir = _hook_dir(
        tmp_path,
        post_tool=f"""
def post_tool(name, args, result=None, error=None):
    open({str(marker)!r}, "w", encoding="utf-8").write(name)
""",
    )
    monkeypatch.setenv("CLIO_HOOKS_BACKEND", "local_python")
    monkeypatch.setenv("CLIO_HOOKS_DIR", str(hooks_dir))

    reg = build_hook_registry()
    reg.fire("post_tool", "fs_read_file", {}, result={"ok": True})

    assert marker.read_text(encoding="utf-8") == "fs_read_file"
    assert reg.metadata()["backend"] == "local_python"
    assert reg.metadata()["handler_counts"]["post_tool"] == 1


def test_hook_registry_factory_can_disable_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_HOOKS_BACKEND", "none")

    reg = build_hook_registry()

    assert reg.fire("pre_tool", "fs_read_file", {}) == []
    assert reg.metadata()["backend"] == "none"
    assert reg.metadata()["enabled"] is False


def test_scoped_hooks_only_fire_for_matching_scope(tmp_path: Path) -> None:
    marker = tmp_path / "workspace_hook.txt"
    scoped = tmp_path / "hooks" / "workspaces" / "ws_science"
    scoped.mkdir(parents=True)
    (scoped / "pre_message.py").write_text(
        f"""
def pre_message(session_id, text):
    open({str(marker)!r}, "w", encoding="utf-8").write(session_id + ":" + text)
""",
        encoding="utf-8",
    )
    reg = HookRegistry(hooks_dir=tmp_path / "hooks")

    reg.fire(
        "pre_message",
        "sess_other",
        "ignored",
        hook_scope={"workspace_id": "ws_other", "session_id": "sess_other"},
    )
    assert not marker.exists()

    reg.fire(
        "pre_message",
        "sess_science",
        "accepted",
        hook_scope={"workspace_id": "ws_science", "session_id": "sess_science"},
    )
    assert marker.read_text(encoding="utf-8") == "sess_science:accepted"
    assert reg.metadata()["scoped_handler_counts"]["workspace_id:ws_science"] == 1


def test_pre_hook_timeout_fails_closed(tmp_path: Path) -> None:
    hooks_dir = _hook_dir(
        tmp_path,
        pre_tool="""
import time

def pre_tool(name, args):
    time.sleep(0.2)
""",
    )
    reg = HookRegistry(hooks_dir=hooks_dir, timeout_s=0.01)

    with pytest.raises(PermissionError, match="exceeded timeout"):
        reg.fire("pre_tool", "fs_read_file", {})


def test_capability_advertised(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        body = c.get("/v1/capabilities").json()
        assert body["capabilities"]["hooks"] is True


def test_capability_reports_runtime_hook_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks_dir = _hook_dir(
        tmp_path,
        pre_tool="""
def pre_tool(name, args):
    return None
""",
    )
    monkeypatch.setenv("CLIO_HOOKS_BACKEND", "local_python")
    monkeypatch.setenv("CLIO_HOOKS_DIR", str(hooks_dir))
    install_global_registry(None)
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as c:
            body = c.get("/v1/capabilities").json()
            assert body["capabilities"]["x_clio_hook_backend"] == "local_python"
            assert body["capabilities"]["x_clio_hook_events"]["pre_tool"] == 1
    finally:
        install_global_registry(None)
