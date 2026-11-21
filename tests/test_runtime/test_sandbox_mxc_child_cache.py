"""MXC owns its native package profile; CLIO still redirects library caches."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.runtime import sandbox, sandbox_net

MXC = sandbox.SandboxResult(
    mechanism=sandbox.MECHANISM_CODEX,
    active=True,
    reason="fence_active",
    details={"codex": {"implementation": "mxc"}},
)


@pytest.mark.parametrize("profile", [sandbox.PROFILE_FLEET, sandbox.PROFILE_SHELL])
def test_mxc_preserves_native_profile_and_temp_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: sandbox.Profile
) -> None:
    """Nested uv must see MXC's real private TEMP rather than an absent package tree."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(sandbox.paths, "workspace_cache_dir", lambda root: cache)
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX,
        active=True,
        reason="fence_active",
        details={"codex": {"implementation": "mxc"}},
    )
    env = sandbox._child_cache_env([tmp_path], state=state, profile=profile)
    assert cache.is_dir()
    assert env["FASTMCP_HOME"] == str(cache)
    assert env["XDG_CACHE_HOME"] == str(cache)
    assert env["FASTMCP_CHECK_FOR_UPDATES"] == "off"
    assert not {"LOCALAPPDATA", "APPDATA", "TEMP", "TMP"}.intersection(env)


def test_elevated_backend_keeps_its_existing_cache_redirection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedicated-user backend still needs its explicitly writable profile."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(sandbox.paths, "workspace_cache_dir", lambda root: cache)
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX,
        active=True,
        reason="fence_active",
        details={"codex": {"implementation": "elevated"}},
    )
    env = sandbox._child_cache_env([tmp_path], state=state, profile=sandbox.PROFILE_FLEET)
    assert all(env[key] == str(cache) for key in sandbox.CHILD_CACHE_DIR_ENV_KEYS)


def test_mxc_container_env_points_at_the_granted_persistent_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside MXC, LOCALAPPDATA/TEMP/TMP go to the child's granted cache, not AC."""
    cache = tmp_path / "home" / "cache" / "sandbox" / "abc"
    monkeypatch.setattr(sandbox.paths, "workspace_cache_dir", lambda root: cache)
    env = sandbox._mxc_container_env([tmp_path / "ws", tmp_path / "home"], state=MXC)
    assert env == {
        "LOCALAPPDATA": str(cache),
        "TEMP": str(cache / "Temp"),
        "TMP": str(cache / "Temp"),
    }
    assert (cache / "Temp").is_dir()
    assert "APPDATA" not in env  # MXC keeps the real roaming folder (uv tools, settings)


def test_mxc_container_env_keeps_the_callers_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(sandbox.paths, "workspace_cache_dir", lambda root: cache)
    scratch = str(tmp_path / "scratch")
    env = sandbox._mxc_container_env(
        [tmp_path], state=MXC, chosen={"TEMP": scratch, "TMP": scratch, "PATH": "x"}
    )
    assert env["TEMP"] == env["TMP"] == scratch
    assert env["LOCALAPPDATA"] == str(cache)
    assert "PATH" not in env


def test_mxc_container_env_skips_an_ungranted_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache outside the write territory would not be writable: MXC's profile stays."""
    monkeypatch.setattr(sandbox.paths, "workspace_cache_dir", lambda root: tmp_path / "elsewhere")
    assert sandbox._mxc_container_env([tmp_path / "ws"], state=MXC) == {}


@pytest.mark.parametrize("implementation", ["elevated", ""])
def test_container_env_is_mxc_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, implementation: str
) -> None:
    monkeypatch.setattr(sandbox.paths, "workspace_cache_dir", lambda root: tmp_path / "c")
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX,
        active=True,
        reason="fence_active",
        details={"codex": {"implementation": implementation}},
    )
    assert sandbox._mxc_container_env([tmp_path], state=state) == {}


def test_wrap_confined_runs_an_mxc_child_through_the_container_env_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(sandbox.paths, "workspace_cache_dir", lambda root: cache)
    monkeypatch.setattr(sandbox_net, "open_child_egress", lambda *a, **k: ("", None, {}))
    monkeypatch.setattr(
        sandbox, "_compose_fence_prefix", lambda state, profile, cmd, args, *a, **k: (cmd, args)
    )
    spawn = sandbox.wrap_confined(
        "tool.exe",
        ["a b", 'c"d'],
        write_roots=[tmp_path],
        profile=sandbox.PROFILE_SHELL,
        state=MXC,
        cwd=tmp_path,
    )
    assert spawn.command == sys.executable
    assert spawn.args[:4] == ["-I", "-S", "-B", str(sandbox._CONTAINER_ENV_SHIM)]
    assert json.loads(spawn.args[4])["LOCALAPPDATA"] == str(cache)
    assert spawn.args[5] == subprocess.list2cmdline(["tool.exe", "a b", 'c"d'])
    assert "LOCALAPPDATA" not in spawn.env_overlay  # set inside, never before MXC starts


@pytest.mark.skipif(sys.platform != "win32", reason="the shim runs inside Windows' MXC fence")
def test_container_env_shim_sets_env_and_passes_argv_stdio_and_exit_code(tmp_path: Path) -> None:
    script = (
        "import os, sys; data = sys.stdin.read(); "
        "print(os.environ['LOCALAPPDATA'], sys.argv[1:], data); sys.exit(7)"
    )
    command, args = sandbox._with_container_env(
        sys.executable, ["-c", script, "a  b", 'q"t', "tail\\"], {"LOCALAPPDATA": str(tmp_path)}
    )
    done = subprocess.run(
        [command, *args], input="in — 20°", capture_output=True, encoding="utf-8", timeout=60
    )
    assert done.returncode == 7
    assert done.stdout.strip() == f"{tmp_path} ['a  b', 'q\"t', 'tail\\\\'] in — 20°"
