"""MXC owns its native package profile; CLIO still redirects library caches."""

from pathlib import Path

import pytest

from clio_agent.runtime import sandbox


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
    assert all(
        Path(env[key]).resolve() == cache.resolve() for key in sandbox.CHILD_CACHE_DIR_ENV_KEYS
    )
