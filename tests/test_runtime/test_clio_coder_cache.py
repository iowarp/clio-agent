"""Instance cache/temp writes must succeed inside the real, unchanged Codex fence."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.runtime import sandbox
from clio_agent.runtime.clio_coder.delegate import DelegationRequest
from clio_agent.runtime.clio_coder.interactive import prepare_interactive_session
from clio_agent.runtime.clio_coder.launch import (
    ClioCoderInstall,
    InstanceHome,
    close_egress,
    spawn_instance,
)
from clio_agent.runtime.clio_coder.workspace import create_worktree, remove_worktree

CACHE_KEYS = ("XDG_CACHE_HOME", "TMP", "TMPDIR", "TEMP")
PROBE = """
import json, os, sys
from pathlib import Path
if "--help" in sys.argv:
    print("--session-file")
    raise SystemExit(0)
result = {"paths": {}, "writes": {}}
for key in ("XDG_CACHE_HOME", "TMP", "TMPDIR", "TEMP"):
    directory = Path(os.environ[key])
    result["paths"][key] = str(directory)
    try:
        target = directory / ("cache-probe-" + key)
        target.mkdir()
        (target / "payload").write_text("cache extraction works")
        result["writes"][key] = "ALLOWED"
    except OSError as exc:
        result["writes"][key] = "DENIED:" + str(exc.errno)
for key, path in {
    "outside": Path(os.environ["CACHE_FIX_OUTSIDE"]),
    "git": Path.cwd() / ".git",
}.items():
    try:
        with path.open("a") as stream:
            stream.write("")
        result["writes"][key] = "ALLOWED"
    except OSError as exc:
        result["writes"][key] = "DENIED:" + str(exc.errno)
print(json.dumps(result), flush=True)
raise SystemExit(0 if all(result["writes"][key] == "ALLOWED" for key in result["paths"]) else 1)
"""


def test_instance_directories_override_ambient_and_child_overlays(tmp_path: Path) -> None:
    outside = {key: str(tmp_path / "ungranted" / key) for key in CACHE_KEYS}
    home = InstanceHome(tmp_path / "instance", extra_env=outside)
    home.prepare(None)
    env = home.environment(outside)
    assert env["XDG_CACHE_HOME"] == str(home.root / "cache")
    assert all(env[key] == str(home.root / "tmp") for key in CACHE_KEYS[1:])


@pytest.mark.skipif(sys.platform != "linux", reason="live Codex bwrap qualification is Linux-only")
@pytest.mark.parametrize("surface", ["headless", "tui"])
async def test_real_fenced_instance_cache_and_temp_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    parent_home = tmp_path / "agent-home"
    monkeypatch.setenv("CLIO_AGENT_HOME", str(parent_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    outside = {key: str(parent_home / "ungranted" / key) for key in CACHE_KEYS}
    for key, value in outside.items():
        if key != "TMPDIR":
            monkeypatch.setenv(key, value)
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "seed.txt").write_text("unchanged parent\n")
    for args in (
        ["init", "-q", str(repo)],
        ["-C", str(repo), "add", "."],
        [
            "-C",
            str(repo),
            "-c",
            "user.name=cache-test",
            "-c",
            "user.email=cache-test@local.invalid",
            "commit",
            "-qm",
            "test: seed cache fixture",
        ],
    ):
        subprocess.run(["git", *args], check=True, capture_output=True)
    install_file = tmp_path / "probe.py"
    install_file.write_text(PROBE)
    install = ClioCoderInstall((sys.executable, "-B", str(install_file)))
    monkeypatch.setenv("CACHE_FIX_OUTSIDE", str(repo / "forbidden"))
    state = sandbox.install_sandbox()
    if not state.active or state.mechanism != sandbox.MECHANISM_CODEX:
        pytest.skip(f"real Codex unavailable: {state.reason}")
    state_root = tmp_path / "state"
    spawned = None
    process = None
    confinement = {}
    if surface == "headless":
        home = InstanceHome(state_root / "home", extra_env=outside)
        home.prepare(None)
        workspace = create_worktree(repo, state_root / "worktrees", "cache")
    else:
        session = prepare_interactive_session(
            DelegationRequest(
                task="Cache qualification", repo=repo, state_root=state_root, install=install
            )
        )
        workspace = session.workspace
        home = InstanceHome(state_root / "home")
        confinement = session.confinement
    try:
        if surface == "headless":
            spawned = await spawn_instance(
                install, [], cwd=workspace.path, home=home, stdin_pipe=False
            )
            process = spawned.process
            confinement = spawned.confinement
        else:
            process = await asyncio.create_subprocess_exec(
                *session.launch.argv,
                cwd=str(session.launch.cwd),
                env=dict(session.launch.env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert confinement["active"] and confinement["mechanism"] == "codex"
        assert process.returncode == 0, (stdout.decode(), stderr.decode())
        facts = json.loads(stdout)
        assert facts["paths"]["XDG_CACHE_HOME"] == str(home.root / "cache")
        assert all(facts["paths"][key] == str(home.root / "tmp") for key in CACHE_KEYS[1:])
        assert all(facts["writes"][key] == "ALLOWED" for key in CACHE_KEYS)
        assert facts["writes"]["outside"] == facts["writes"]["git"] == "DENIED:30"
        assert not (repo / "forbidden").exists()
        print(json.dumps({"surface": surface, "confinement": confinement, **facts}))
    finally:
        if spawned is not None:
            await spawned.stop()
        elif process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        close_egress(confinement)
        remove_worktree(workspace)
