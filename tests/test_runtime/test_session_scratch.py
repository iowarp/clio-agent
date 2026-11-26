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
        assert Path(env[key]).resolve() == mine  # a short alias when the folder is deep
        assert len(env[key]) <= max(len(str(mine)), 64)
    assert mine.is_dir()
    assert shell_environment(workspace).get("TMPDIR") != str(mine)  # off-session: unchanged


@pytest.mark.parametrize(
    ("host", "kernel", "socket_budget", "uv_cache_budget"),
    [
        ("linux", (5, 15), 64, 192),
        ("linux", (4, 18), 64, 64),
        ("darwin", (25, 0), 60, 448),
        ("windows", (0, 0), 64, 48),
        ("posix", (14, 1), 60, 192),
    ],
)
def test_each_os_gets_its_own_short_root_budgets(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    kernel: tuple[int, int],
    socket_budget: int,
    uv_cache_budget: int,
) -> None:
    """sun_path is 108 bytes on Linux and Windows, 104 on macOS; MAX_PATH/PATH_MAX differ,
    and a uv entry point's ``#!`` line must fit the kernel's buffer (128 before Linux 5.1)."""

    monkeypatch.setattr(session_scratch, "_host_os", lambda: host)
    monkeypatch.setattr(session_scratch, "_kernel_release", lambda: kernel)
    assert session_scratch.short_root_budget(session_scratch.SOCKET_ROLE) == socket_budget
    assert session_scratch.short_root_budget(session_scratch.UV_CACHE_ROLE) == uv_cache_budget
    assert session_scratch.short_root_budget(session_scratch.UV_CACHE_ROLE) == uv_cache_budget


@pytest.mark.skipif(os.name != "posix", reason="a POSIX symlink in /tmp")
def test_a_deep_scratch_folder_gets_a_short_private_link_for_unix_sockets(
    tmp_path: Path,
) -> None:
    import socket as socket_module

    deep = tmp_path.joinpath(*(["d" * 20] * 6), "sess_deep")
    deep.mkdir(parents=True)
    alias = session_scratch.socket_safe_dir(deep)
    try:
        assert alias != deep and len(str(alias)) <= 64
        assert alias.is_symlink() and alias.resolve() == deep.resolve()
        assert session_scratch.socket_safe_dir(deep) == alias  # reused, not duplicated
        server = socket_module.socket(socket_module.AF_UNIX)
        server.bind(tempfile.mkdtemp(dir=str(alias)) + "/cm.sock")
        server.close()
        changed = session_scratch.socket_safe_temp(
            {"TMPDIR": str(deep), "TEMP": str(deep), "TMP": "/elsewhere"}
        )
        assert changed == {"TMPDIR": str(alias), "TEMP": str(alias)}
        short = tmp_path / "s"
        short.mkdir()
        if len(str(short)) <= 60:
            assert session_scratch.socket_safe_dir(short) == short
    finally:
        session_scratch._remove_short_root(deep, session_scratch.SOCKET_ROLE)
    assert not alias.exists() and not alias.is_symlink()


@pytest.fixture
def short_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A short system temp folder for stand-ins and Windows links."""

    temp = tmp_path / "t"
    temp.mkdir()
    monkeypatch.setattr(session_scratch.tempfile, "gettempdir", lambda: str(temp))
    return temp


@pytest.mark.skipif(os.name != "nt", reason="a Windows junction in the system temp")
def test_a_deep_scratch_folder_gets_a_short_junction_on_windows(
    tmp_path: Path, short_temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An AF_UNIX bind follows a junction, and files made through it stay in the folder."""

    deep = tmp_path / ("d" * 40) / "sess_deep"  # deeper than its junction in short_temp
    deep.mkdir(parents=True)
    alias = session_scratch.socket_safe_dir(deep)
    assert alias.parent == short_temp and alias.is_junction()
    assert session_scratch.socket_safe_dir(deep) == alias  # reused, not duplicated
    (alias / "made-through-the-link").write_text("x", encoding="utf-8")
    assert (deep / "made-through-the-link").is_file()
    assert session_scratch.socket_safe_temp({"TEMP": str(deep), "TMP": str(deep)}) == {
        "TEMP": str(alias),
        "TMP": str(alias),
    }
    session_scratch._remove_short_root(deep, session_scratch.SOCKET_ROLE)
    assert not os.path.lexists(alias) and (deep / "made-through-the-link").is_file()
    monkeypatch.setenv("CLIO_ALLOW_SYSTEM_TEMP", "0")
    assert session_scratch.socket_safe_dir(deep) == deep  # the fence would not grant it


@pytest.mark.skipif(os.name != "nt", reason="a Windows junction in the system temp")
def test_a_uv_cache_through_the_socket_junction_still_gets_a_stand_in(
    tmp_path: Path, short_temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DLL load resolves the junction, so the cache is as deep as its real folder."""

    deep = tmp_path / ("d" * 40) / "sess_deep"
    deep.mkdir(parents=True)
    alias = session_scratch.socket_safe_dir(deep)
    assert alias.is_junction()
    through_link = alias / "uv-cache"
    limits = {
        session_scratch.SOCKET_LIMIT: 108,
        session_scratch.PATH_LIMIT: len(str(through_link)) + 212,  # the link name fits
    }
    monkeypatch.setattr(session_scratch, "_os_limits", lambda: limits)
    short = session_scratch.short_uv_cache(through_link)
    assert short not in (through_link, deep / "uv-cache") and not short.is_junction()
    assert short == session_scratch.short_uv_cache(deep / "uv-cache")  # one root per folder
    session_scratch._remove_short_root(deep / "uv-cache", session_scratch.UV_CACHE_ROLE)
    session_scratch._remove_short_root(deep, session_scratch.SOCKET_ROLE)
    assert not short.exists()


@pytest.mark.skipif(os.name != "posix", reason="a POSIX symlink in /tmp")
def test_a_socket_link_never_follows_a_name_it_does_not_own(tmp_path: Path) -> None:
    deep = tmp_path.joinpath(*(["e" * 20] * 6), "sess_squat")
    deep.mkdir(parents=True)
    alias = session_scratch._short_path(deep, session_scratch.SOCKET_ROLE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, alias)  # squatted: the name points somewhere else
    try:
        assert session_scratch.socket_safe_dir(deep) == deep
        assert session_scratch.socket_safe_temp({"TMPDIR": str(deep)}) == {}
        session_scratch._remove_short_root(deep, session_scratch.SOCKET_ROLE)
        assert alias.is_symlink()  # someone else's link is left alone
    finally:
        alias.unlink()


@pytest.mark.skipif(os.name != "posix", reason="a POSIX symlink in /tmp")
def test_deleting_a_session_removes_its_socket_link(root: Path) -> None:
    folder = session_scratch.session_scratch_dir("sess_" + "a" * 60)
    alias = session_scratch.socket_safe_dir(folder)
    assert alias != folder and alias.is_symlink()
    assert session_scratch.remove_session_scratch("sess_" + "a" * 60, why="session_deleted")
    assert not alias.is_symlink()


@pytest.mark.parametrize("host", ["windows", "darwin", "linux"])
def test_a_uv_cache_past_its_os_budget_gets_a_short_owned_stand_in(
    tmp_path: Path, short_temp: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """uv imports ``--with`` packages from its cache; each OS bounds a library's path."""

    monkeypatch.setattr(session_scratch, "_host_os", lambda: host)
    monkeypatch.setattr(session_scratch, "_kernel_release", lambda: (5, 15))
    budget = session_scratch.short_root_budget(session_scratch.UV_CACHE_ROLE)
    base = Path(tmp_path.anchor) / "v"  # a name only: a cache is keyed before it exists
    at_budget = base / ("d" * (budget - len(str(base)) - 1))
    assert len(str(at_budget)) == budget
    assert session_scratch.short_uv_cache(at_budget) == at_budget  # fits: stays put
    pad = "x" * (len(str(short_temp)) + 40)  # past the budget and longer than its stand-in
    cache = Path(str(at_budget) + pad) / "uv-cache"
    short = session_scratch.short_uv_cache(cache)
    assert short.parent == short_temp and short.is_dir() and not short.is_symlink()
    assert (short / "owner.txt").read_text(encoding="utf-8").strip() == str(cache)
    assert session_scratch.short_uv_cache(cache) == short  # reused, not duplicated
    other_cache = Path(str(at_budget) + pad + "y") / "uv-cache"
    other = session_scratch.short_uv_cache(other_cache)
    assert other not in (short, cache)  # each session keeps its own cache
    session_scratch._remove_short_root(cache, session_scratch.UV_CACHE_ROLE)
    session_scratch._remove_short_root(other_cache, session_scratch.UV_CACHE_ROLE)
    assert not short.exists() and not other.exists()
    monkeypatch.setenv("CLIO_ALLOW_SYSTEM_TEMP", "0")
    assert session_scratch.short_uv_cache(cache) == cache  # temp not granted: stays put


@pytest.mark.parametrize("host", ["windows", "darwin", "linux"])
def test_a_fenced_folder_inherits_its_parents_grants_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Python 3.13+ on Windows turns mode 0o700 into a private ACL the fence cannot reach."""

    monkeypatch.setattr(session_scratch, "_host_os", lambda: host)
    modes: list[int] = []
    real_mkdir = Path.mkdir

    def recording_mkdir(self: Path, mode: int = 0o777, *args: Any, **kwargs: Any) -> None:
        modes.append(mode)
        real_mkdir(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", recording_mkdir)
    session_scratch._make_folder(tmp_path / "fenced", fenced=True)
    session_scratch._make_folder(tmp_path / "own", fenced=False)
    fenced_mode = 0o777 if host == "windows" else 0o700
    assert modes == [fenced_mode, 0o700]


def test_a_private_stand_in_is_given_back_its_parents_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(session_scratch, "_dacl_protected", lambda folder: True)
    monkeypatch.setattr(
        "subprocess.run", lambda argv, **kwargs: calls.append(argv) or SimpleNamespace()
    )
    session_scratch._inherit_parent_grants(tmp_path, session_scratch.UV_CACHE_ROLE)
    session_scratch._inherit_parent_grants(tmp_path, session_scratch.SOCKET_ROLE)  # a link
    session_scratch._inherit_parent_grants(tmp_path, session_scratch.CORE_IPC_ROLE)  # unfenced
    assert calls == [["icacls", str(tmp_path), "/inheritancelevel:e", "/Q"]]


def test_short_roots_whose_folder_is_gone_are_swept(
    tmp_path: Path, short_temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home deleted outright leaves its short roots; the boot sweep removes only those."""

    monkeypatch.setattr(session_scratch, "_base_dir", lambda kind: short_temp)
    owner = os.getuid() if hasattr(os, "getuid") else "w"

    def name(n: int) -> Path:
        return short_temp / f"clio-{owner}-{n:016x}"

    def link(short: Path, target: Path) -> None:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(short))
        else:
            os.symlink(target, short)

    def stand_in(short: Path, target: Path) -> None:
        short.mkdir()
        (short / "owner.txt").write_text(f"{target}\n", encoding="utf-8")

    gone, here = tmp_path / "gone", tmp_path / "here"
    gone.mkdir()
    here.mkdir()
    link(name(1), gone)
    link(name(2), here)
    stand_in(name(3), gone / "uv-cache")
    stand_in(name(4), here / "uv-cache")  # not filled yet: its folder still exists
    foreign = short_temp / "clio-x-0000000000000005"
    stand_in(foreign, gone / "uv-cache")
    gone.rmdir()
    assert session_scratch.sweep_short_roots() == [name(1), name(3)]
    assert not os.path.lexists(name(1)) and not name(3).exists()
    assert os.path.lexists(name(2)) and name(4).is_dir() and foreign.is_dir() and here.is_dir()


@pytest.mark.parametrize("host", ["windows", "darwin", "linux"])
def test_clio_core_ipc_past_the_socket_limit_gets_a_short_owned_folder(
    tmp_path: Path, short_temp: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """clio-core binds ``clio_zmq_<port>.ipc`` in its per-user folder; a deep Windows temp
    or a long ``USER`` puts it past sun_path, so the daemon is pointed at a short folder."""

    from clio_agent.arc import clio_core_config

    monkeypatch.setattr(session_scratch, "_host_os", lambda: host)
    monkeypatch.setattr(session_scratch, "_kernel_release", lambda: (5, 15))
    budget = session_scratch.short_root_budget(session_scratch.CORE_IPC_ROLE)
    assert budget == (84 if host == "darwin" else 88)
    monkeypatch.setattr(
        session_scratch,
        "_base_dir",
        lambda kind: short_temp if kind == session_scratch.SYSTEM_TEMP else None,
    )
    limit = len(str(short_temp)) + 60  # a test temp can be deep itself; scale sun_path to it
    limits = {session_scratch.SOCKET_LIMIT: limit, session_scratch.PATH_LIMIT: 4096}
    monkeypatch.setattr(session_scratch, "_os_limits", lambda: limits)
    budget = limit - 20
    fits = tmp_path / ("f" * max(1, budget - len(str(tmp_path)) - 1))
    assert session_scratch.short_core_ipc_dir(fits) is None  # clio-core's own folder stays
    deep = tmp_path / ("d" * 100) / "clio_someone"
    short = session_scratch.short_core_ipc_dir(deep)
    assert short is not None and short.parent == short_temp and short.is_dir()
    assert not short.is_symlink() and len(str(short / "clio_zmq_29934.ipc")) < limit - 1
    assert session_scratch.short_core_ipc_dir(deep) == short  # daemon and client agree
    monkeypatch.setenv("CLIO_ALLOW_SYSTEM_TEMP", "0")
    assert session_scratch.short_core_ipc_dir(deep) == short  # not a fenced child's root

    monkeypatch.setattr(clio_core_config, "core_default_memfd_dir", lambda env: deep)
    env: dict[str, str] = {}
    clio_core_config.pin_core_memfd_dir(env)
    assert env == {"CLIO_MEMFD_DIR": str(short)}
    explicit = {"CLIO_MEMFD_DIR": "/srv/mine"}
    clio_core_config.pin_core_memfd_dir(explicit)
    assert explicit == {"CLIO_MEMFD_DIR": "/srv/mine"}  # an explicit value is respected


def test_a_missing_temp_folder_is_created_for_the_clio_core_ipc_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.arc import clio_core_config  # noqa: PLC0415
    from clio_agent.arc.init_degradation import classify_init_failure  # noqa: PLC0415

    gone = tmp_path / "deleted-temp" / "clio_someone"
    monkeypatch.setattr(clio_core_config, "core_default_memfd_dir", lambda env: gone)
    assert clio_core_config.ensure_core_memfd_dir({}) == gone and gone.is_dir()
    pinned = tmp_path / "pinned" / "ipc"
    assert clio_core_config.ensure_core_memfd_dir({"CLIO_MEMFD_DIR": str(pinned)}) == pinned
    assert pinned.is_dir()

    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(clio_core_config.ClioCoreIpcDirUnavailableError) as info:
        clio_core_config.ensure_core_memfd_dir({"CLIO_MEMFD_DIR": str(blocker / "ipc")})
    assert str(blocker / "ipc") in str(info.value) and "CLIO_MEMFD_DIR" in str(info.value)
    assert classify_init_failure(info.value) == "clio_core_ipc_dir_unavailable"


def test_a_short_uv_cache_never_takes_a_folder_it_does_not_own(
    tmp_path: Path, short_temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_scratch, "_host_os", lambda: "windows")
    cache = tmp_path.joinpath(*(["e" * 20] * 4), "sess_squat", "uv-cache")
    squatted = session_scratch._short_path(cache, session_scratch.UV_CACHE_ROLE)
    squatted.mkdir(parents=True)
    (squatted / "theirs").write_text("x", encoding="utf-8")
    assert session_scratch.short_uv_cache(cache) == cache
    session_scratch._remove_short_root(cache, session_scratch.UV_CACHE_ROLE)
    assert (squatted / "theirs").is_file()  # someone else's folder is left alone


def test_deleting_a_session_removes_its_short_uv_cache(
    root: Path, short_temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_scratch, "_host_os", lambda: "windows")
    sid = "sess_" + "b" * 60
    folder = session_scratch.session_scratch_dir(sid)
    short = session_scratch.short_uv_cache(folder / "uv-cache")
    assert short.parent == short_temp
    (short / "archive-v0").mkdir()
    assert session_scratch.remove_session_scratch(sid, why="session_deleted")
    assert not short.exists()


def test_a_session_shell_uses_the_short_uv_cache(
    root: Path, workspace: Path, short_temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_scratch, "_host_os", lambda: "windows")
    sid = "sess_" + "c" * 60
    with bound(sid):
        environment = shell_environment(workspace)
    short = Path(environment["UV_CACHE_DIR"])
    assert short.parent == short_temp
    assert (short / "owner.txt").read_text(encoding="utf-8").strip().endswith("uv-cache")


def test_a_kernel_starts_with_its_own_session_environment(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Path, str]] = []

    def environment(where: Path, session_id: str, *, read_only: bool = False) -> Any:
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
        "sess_p": SimpleNamespace(
            id="sess_p", parent_session_id="", workspace_id="ws_1", metadata={}
        ),
        "sess_n": SimpleNamespace(
            id="sess_n",
            parent_session_id="",
            workspace_id="ws_1",
            metadata={NODE_FOLDER_KEY: str(node)},
        ),
    }
    app = _app(sessions, workspace)
    for sid in ("sess_p", "sess_n"):  # the allowed-roots branch and the node-folder branch
        text = _runtime_active_workspace_context(app, session_id=sid)
        folder = (root / "node-a" / sid).resolve()
        assert "Machine: node-a" in text
        assert (
            f"Scratch folder for this session: {folder}. It is yours alone and the person "
            'never sees it: their files, and "this folder", are the workspace. Use it for '
            "your own temporary files (extracted copies, intermediate results); it is "
            "removed after the session."
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
    shutdown (a startup test hung 180 s under load). Orphaned short roots need no store, so
    they are swept before the wait: a store that never attaches must not keep them.
    SABOTAGE: run ``sweep_sessions`` on a worker straight away, or sweep the short roots
    only after the store is readable, and this fails."""
    import asyncio

    threads: list[str] = []
    roots: list[str] = []
    monkeypatch.setattr(
        session_scratch_sweep, "sweep_sessions", lambda sessions: threads.append("ran") or []
    )
    monkeypatch.setattr(session_scratch, "sweep_short_roots", lambda: roots.append("ran") or [])
    monkeypatch.setattr(session_scratch_sweep, "_READY_POLL_S", 0.01)

    class Attaching:
        readable = False

    async def scenario() -> None:
        store = Attaching()
        task = asyncio.create_task(session_scratch_sweep.sweep_when_readable(store))
        await asyncio.sleep(0.05)
        assert threads == [] and roots == ["ran"] and not task.done()
        store.readable = True
        assert await asyncio.wait_for(task, 1.0) == []
        assert threads == ["ran"]
        waiting = asyncio.create_task(session_scratch_sweep.sweep_when_readable(Attaching()))
        await asyncio.sleep(0.03)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert threads == ["ran"] and roots == ["ran", "ran"]

    asyncio.run(scenario())


@pytest.mark.parametrize("sid", ["../escape", "a/b", ".hidden", "", "x" * 200])
def test_an_unsafe_session_id_names_no_folder(root: Path, workspace: Path, sid: str) -> None:
    assert session_scratch.session_scratch_dir(sid) is None
    assert session_scratch.remove_session_scratch(sid, why="test") is False
    with bound(sid):
        assert session_scratch.active_scratch_dir() is None
        assert "TMPDIR" not in shell_environment(workspace)
    assert not root.exists()
