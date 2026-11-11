"""Per-session scratch folders (TD56): path, write roots, TMPDIR, prompt and lifecycle.

Each session has ``<scratch root>/<machine name>/<session id>``; nothing is shared with
another session, another host on the same root, or a child session.

Sabotage (each fails with its fix removed): the machine segment dropped from
:func:`session_scratch.machine_dir`; the scratch append removed from
``file_policy._with_active_workspace``; the scratch add removed from
``write_scope.narrowed_write_roots`` or the fence's narrowed branch; the fleet exclusion;
``_session_temp`` removed from
``shell_environment``; the kernel's ``session_id`` not passed; the prompt lines not
appended; ``remove_session_scratch`` not called on delete; the retention branch of
:func:`session_scratch.sweep`; the safe-id check.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as gact_context
from clio_agent.gact import session_scratch_sweep
from clio_agent.gact.agents.composition import _runtime_active_workspace_context
from clio_agent.gact.code_mode import kernels
from clio_agent.gact.node_folders import NODE_FOLDER_KEY
from clio_agent.gact.session_tool_output import delete_session_tool_output
from clio_agent.runtime import sandbox_roots, session_scratch
from clio_agent.runtime.execution_environment import shell_environment
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.file_policy import FileAccessPolicy
from clio_agent.tools.write_scope import tool_write_root_context

DAY = 86400.0


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("CLIO_SCRATCH_ROOT", str(scratch))
    monkeypatch.setenv("CLIO_MACHINE_NAME", "node-a")
    return scratch


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = (tmp_path / "ws").resolve()
    folder.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(folder))
    return folder


@contextmanager
def bound(session_id: str, *, tool: bool = False) -> Iterator[None]:
    """Bind ``session_id`` as the active (or the tool-call) session."""

    setter = gact_context.set_tool_session_id if tool else gact_context.set_session_id
    token = setter(session_id)
    try:
        yield
    finally:
        gact_context.reset(token)


def test_two_sessions_and_two_machines_share_a_root_without_sharing_folders(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = session_scratch.session_scratch_dir("sess_a")
    b = session_scratch.session_scratch_dir("sess_b")
    assert a == (root / "node-a" / "sess_a").resolve() and a.is_dir()
    assert b == (root / "node-a" / "sess_b").resolve() and b.is_dir()
    if os.name != "nt":
        assert a.stat().st_mode & 0o777 == 0o700
    monkeypatch.setenv("CLIO_MACHINE_NAME", "node b/../x")  # sanitized to one segment
    other = session_scratch.session_scratch_dir("sess_a")
    assert other is not None and other.parent.parent == root.resolve()
    assert other.parent.name == "node_b_.._x"
    assert other != a


def test_the_machine_name_defaults_to_the_hostname(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIO_MACHINE_NAME")
    monkeypatch.setattr(session_scratch.socket, "gethostname", lambda: "login01.cluster")
    assert session_scratch.machine_name() == "login01.cluster"
    monkeypatch.setattr(session_scratch.socket, "gethostname", lambda: "...")
    assert session_scratch.machine_name() == "host"


def test_a_child_session_has_its_own_folder(root: Path) -> None:
    with bound("sess_parent"):
        parent = session_scratch.active_scratch_dir()
        with bound("sess_child"):
            child = session_scratch.active_scratch_dir()
    with bound("sess_child", tool=True):  # a child's tool calls carry its tool session
        tool_child = session_scratch.active_scratch_dir()
    assert parent == (root / "node-a" / "sess_parent").resolve()
    assert child == tool_child == (root / "node-a" / "sess_child").resolve()
    assert session_scratch.active_scratch_dir() is None


def test_the_write_roots_include_the_session_folder_and_no_other(
    root: Path, workspace: Path
) -> None:
    mine = session_scratch.session_scratch_dir("sess_w")
    theirs = session_scratch.session_scratch_dir("sess_other")
    (mine / "extract").mkdir()
    with tool_workspace_context(workspace), bound("sess_w"):
        policy = FileAccessPolicy.from_env()
        assert mine in policy.allowed_roots
        assert policy.validate_write(str(mine / "extract" / "a.txt")) == mine / "extract" / "a.txt"
        assert theirs not in policy.allowed_roots
        fence = sandbox_roots.effective_write_roots(
            sandbox_roots.PROFILE_SHELL, workspace_root=str(workspace)
        )
        assert mine in fence and theirs not in fence
    with tool_workspace_context(workspace):
        assert mine not in FileAccessPolicy.from_env().allowed_roots


def test_a_node_session_writes_its_scratch_folder(root: Path, workspace: Path) -> None:
    folder = workspace / "flows" / "node"
    folder.mkdir(parents=True)
    mine = session_scratch.session_scratch_dir("sess_node")
    with tool_workspace_context(workspace), tool_write_root_context(folder), bound("sess_node"):
        policy = FileAccessPolicy.from_env()
        assert mine in policy.write_roots
        assert policy.validate_write(str(mine / "a.txt")) == mine / "a.txt"
        fence = sandbox_roots.effective_write_roots(
            sandbox_roots.PROFILE_SHELL, workspace_root=str(workspace)
        )
        assert mine in fence
        given = FileAccessPolicy(allowed_roots=(workspace,), write_roots=(folder,))
        fence = sandbox_roots.effective_write_roots(
            sandbox_roots.PROFILE_SHELL, policy=given, workspace_root=str(workspace)
        )
        assert mine in fence  # whichever narrowed policy the shell is given


def test_the_shared_fleet_never_gets_a_session_folder(root: Path, workspace: Path) -> None:
    mine = session_scratch.session_scratch_dir("sess_f")
    with tool_workspace_context(workspace), bound("sess_f"):
        assert mine in FileAccessPolicy.from_env().allowed_roots
        fleet = sandbox_roots.effective_write_roots(
            sandbox_roots.PROFILE_FLEET, workspace_root=str(workspace)
        )
    assert mine not in fleet


def test_the_system_temp_dir_leaves_the_write_roots_when_turned_off(
    root: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.tools import file_policy

    temp = Path(tempfile.gettempdir())
    folder = workspace / "node"
    folder.mkdir()

    def fences() -> list[tuple[Path, ...]]:
        with tool_workspace_context(workspace), bound("sess_t"):
            shell = sandbox_roots.effective_write_roots(
                sandbox_roots.PROFILE_SHELL, workspace_root=str(workspace)
            )
            with tool_write_root_context(folder):
                node = FileAccessPolicy.from_env().write_roots
                narrowed = sandbox_roots.effective_write_roots(
                    sandbox_roots.PROFILE_SHELL, workspace_root=str(workspace)
                )
        return [file_policy._default_allowed_roots(), shell, node, narrowed]

    assert all(temp in roots or temp.resolve() in roots for roots in fences())
    monkeypatch.setenv("CLIO_ALLOW_SYSTEM_TEMP", "false")
    for roots in fences():
        assert temp not in roots and temp.resolve() not in roots, roots


def test_tmpdir_points_at_the_session_folder_for_the_kernel(root: Path, workspace: Path) -> None:
    mine = (root / "node-a" / "sess_k").resolve()
    _python, env = kernels._environment(workspace, "sess_k")
    for key in ("TMPDIR", "TEMP", "TMP"):
        assert env[key] == str(mine)
    assert mine.is_dir()
    assert shell_environment(workspace).get("TMPDIR") != str(mine)  # off-session: unchanged


def test_a_kernel_starts_with_its_own_session_environment(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Path, str]] = []

    def environment(where: Path, session_id: str) -> Any:
        seen.append((where, session_id))
        raise RuntimeError("stop before spawning")

    monkeypatch.setattr(kernels, "_environment", environment)
    with bound("sess_other"), pytest.raises(RuntimeError, match="stop before spawning"):
        kernels.Kernel(("sess_kernel", "default"), workspace).start()
    assert seen == [(workspace, "sess_kernel")]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX shell prints $TMPDIR")
async def test_tmpdir_points_at_the_session_folder_for_the_shell(
    root: Path, workspace: Path
) -> None:
    from fastmcp import Client

    from clio_agent.tools.servers.shell_server import shell_server

    mine = (root / "node-a" / "sess_sh").resolve()
    command = f"'{sys.executable}' -c \"import tempfile; print(tempfile.gettempdir())\""
    with tool_workspace_context(str(workspace)), bound("sess_sh", tool=True):
        async with Client(shell_server) as client:
            result = await client.call_tool("bash", {"command": command, "timeout_s": 10})
    data = getattr(result, "data", result)
    data = data if isinstance(data, dict) else json.loads(data)
    assert data["exit_code"] == 0, data
    assert Path(data["stdout"].strip()).resolve() == mine


def _app(sessions: dict[str, Any], workspace: Path) -> Any:
    workspaces = {"ws_1": SimpleNamespace(root_path=str(workspace))}
    return SimpleNamespace(state=SimpleNamespace(sessions=sessions, workspaces=workspaces))


def test_the_prompt_names_the_machine_and_the_scratch_folder(root: Path, workspace: Path) -> None:
    node = workspace / "node"
    sessions = {
        "sess_p": SimpleNamespace(workspace_id="ws_1", metadata={}),
        "sess_n": SimpleNamespace(workspace_id="ws_1", metadata={NODE_FOLDER_KEY: str(node)}),
    }
    app = _app(sessions, workspace)
    for sid in ("sess_p", "sess_n"):  # the allowed-roots branch and the node-folder branch
        text = _runtime_active_workspace_context(app, session_id=sid)
        folder = (root / "node-a" / sid).resolve()
        assert "Machine: node-a" in text
        assert (
            f"Scratch folder for this session: {folder}. Use it for temporary files "
            "(extracted copies, intermediate results); it is yours alone and is removed "
            "after the session."
        ) in text
        assert folder.is_dir()  # naming it is the first need: the file tools can write there


def test_deleting_a_session_removes_its_folder(
    root: Path, workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gone = session_scratch.session_scratch_dir("sess_del")
    kept = session_scratch.session_scratch_dir("sess_keep")
    (gone / "extracted").mkdir()
    (gone / "extracted" / "old.py").write_text("x", encoding="utf-8")
    app = _app({}, workspace)
    delete_session_tool_output(app, "ws_1", "sess_del")
    assert not gone.exists()
    assert kept.is_dir()


def test_the_sweep_removes_gone_and_idle_sessions_on_this_machine(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    for sid in ("sess_gone", "sess_old", "sess_fresh"):
        session_scratch.session_scratch_dir(sid)
    activity = {"sess_old": now - 8 * DAY, "sess_fresh": now - 6 * DAY}
    monkeypatch.setenv("CLIO_MACHINE_NAME", "node-b")
    elsewhere = session_scratch.session_scratch_dir("sess_gone")
    monkeypatch.setenv("CLIO_MACHINE_NAME", "node-a")

    removed = session_scratch.sweep(activity.get, now=now)

    assert sorted(removed) == ["sess_gone", "sess_old"]
    assert (root / "node-a" / "sess_fresh").is_dir()
    assert elsewhere is not None and elsewhere.is_dir()  # another host's folder is not ours
    monkeypatch.setenv("CLIO_SCRATCH_RETENTION_DAYS", "5")
    assert session_scratch.sweep(activity.get, now=now) == ["sess_fresh"]


def test_the_boot_sweep_reads_last_activity_from_the_session_store(root: Path) -> None:
    old = "2020-01-01T00:00:00+00:00"
    new = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    sessions = {
        "sess_idle": SimpleNamespace(last_interaction_at=old, created_at=new),
        "sess_created": SimpleNamespace(last_interaction_at="", created_at=new),
        "sess_unreadable": SimpleNamespace(last_interaction_at="soon", created_at=""),
    }
    for sid in (*sessions, "sess_deleted"):
        session_scratch.session_scratch_dir(sid)
    removed = session_scratch_sweep.sweep_sessions(sessions)
    assert sorted(removed) == ["sess_deleted", "sess_idle"]

    class Unready:
        def get(self, _sid: str) -> Any:
            from clio_agent.gact.session_records import SessionRecordsNotReadyError

            raise SessionRecordsNotReadyError("clio-core did not attach")

    assert session_scratch_sweep.sweep_sessions(Unready()) == []
    assert (root / "node-a" / "sess_created").is_dir()


def test_the_boot_sweep_waits_for_the_store_on_the_loop(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the store can be read the sweep waits on the server loop, starts no worker,
    and is cancelled with the server: a worker blocked on the clio-core attach held the
    shutdown (a startup test hung 180 s under load).
    SABOTAGE: run ``sweep_sessions`` on a worker straight away and this fails."""
    import asyncio

    threads: list[str] = []
    monkeypatch.setattr(
        session_scratch_sweep, "sweep_sessions", lambda sessions: threads.append("ran") or []
    )
    monkeypatch.setattr(session_scratch_sweep, "_READY_POLL_S", 0.01)

    class Attaching:
        readable = False

    async def scenario() -> None:
        store = Attaching()
        task = asyncio.create_task(session_scratch_sweep.sweep_when_readable(store))
        await asyncio.sleep(0.05)
        assert threads == [] and not task.done()
        store.readable = True
        assert await asyncio.wait_for(task, 1.0) == []
        assert threads == ["ran"]
        waiting = asyncio.create_task(session_scratch_sweep.sweep_when_readable(Attaching()))
        await asyncio.sleep(0.03)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert threads == ["ran"]

    asyncio.run(scenario())


@pytest.mark.parametrize("sid", ["../escape", "a/b", ".hidden", "", "x" * 200])
def test_an_unsafe_session_id_names_no_folder(root: Path, workspace: Path, sid: str) -> None:
    assert session_scratch.session_scratch_dir(sid) is None
    assert session_scratch.remove_session_scratch(sid, why="test") is False
    with bound(sid):
        assert session_scratch.active_scratch_dir() is None
        assert "TMPDIR" not in shell_environment(workspace)
    assert not root.exists()
