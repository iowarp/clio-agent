"""The four ways install.sh installs CLIO, and their pins.

install.sh                 latest stable release
install.sh --dev           latest release, pre-releases included
install.sh --source        source at main (or the checkout it runs from, in place)
install.sh --dev --source  source at develop
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.test_scripts.installer_shims import (
    ROOT,
    STOP,
    run_installer,
    write_release,
    write_releases,
)

pytestmark = pytest.mark.skipif(
    os.name == "nt" and not Path("C:/Program Files/Git/bin/bash.exe").exists(),
    reason="needs bash",
)


def _release_fixtures(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    # Published in this order. v0.10.0a1 is an old alpha that sorts above the newest
    # beta; v0.9.5-beta.9 is a draft, not published.
    write_releases(
        fixtures,
        ["v0.10.0a1", "v0.9.4.23", "v0.9.4.24", "v0.9.5-beta.7", "v0.9.5-beta.8", "v0.9.5-beta.9"],
        drafts=("v0.9.5-beta.9",),
    )
    write_release(fixtures, "v0.9.4.24", python=None)  # before .python-version existed
    write_release(fixtures, "v0.9.5-beta.8", python="3.14")
    write_release(fixtures, "v0.9.5-beta.2", python="3.14")


def _call(calls: list[str], prefix: str) -> str:
    matches = [call for call in calls if call.startswith(prefix)]
    assert matches, f"no call starting {prefix!r} in {calls}"
    return matches[-1]


def test_default_installs_the_latest_stable_release_locked_by_its_tag(tmp_path: Path) -> None:
    _release_fixtures(tmp_path)
    result, calls = run_installer(tmp_path)
    assert result.returncode == STOP, result.stdout + result.stderr
    assert "--python 3.13" in _call(calls, "uv venv")  # 0.9.4.24 predates .python-version
    install = _call(calls, "uv pip install")
    assert "clio-agent[argonne,flowcept]==0.9.4.24" in install
    assert "--constraint" in install
    assert "curl https://raw.githubusercontent.com/iowarp/clio-agent/v0.9.4.24/uv.lock" in calls


def test_dev_installs_the_latest_release_including_pre_releases(tmp_path: Path) -> None:
    _release_fixtures(tmp_path)
    result, calls = run_installer(tmp_path, "--dev")
    assert result.returncode == STOP, result.stdout + result.stderr
    assert "--python 3.14" in _call(calls, "uv venv")
    assert "clio-agent[argonne,flowcept]==0.9.5b8" in _call(calls, "uv pip install")
    assert "curl https://raw.githubusercontent.com/iowarp/clio-agent/v0.9.5-beta.8/uv.lock" in calls


@pytest.mark.parametrize(
    ("args", "env"),
    [(("--version", "0.9.5b2"), {}), ((), {"CLIO_VERSION": "v0.9.5-beta.2"})],
)
def test_a_pinned_release_installs_exactly_that_version(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str]
) -> None:
    _release_fixtures(tmp_path)
    result, calls = run_installer(tmp_path, *args, env=env)
    assert result.returncode == STOP, result.stdout + result.stderr
    assert "clio-agent[argonne,flowcept]==0.9.5b2" in _call(calls, "uv pip install")
    assert not any("api.github.com" in call for call in calls), "a pinned version is not resolved"


@pytest.mark.parametrize(
    ("args", "branch"), [(("--source",), "main"), (("--dev", "--source"), "develop")]
)
def test_source_from_a_stream_clones_the_branch_and_installs_its_lock(
    tmp_path: Path, args: tuple[str, ...], branch: str
) -> None:
    streamed = tmp_path / "streamed" / "install.sh"  # not inside a checkout
    streamed.parent.mkdir()
    shutil.copy(ROOT / "install/install.sh", streamed)
    result, calls = run_installer(tmp_path, *args, script=streamed)
    assert result.returncode == STOP, result.stdout + result.stderr
    assert f"--branch {branch}" in _call(calls, "git clone")
    sync = _call(calls, "uv sync")
    assert "--locked" in sync and "--python 3.14" in sync
    assert sync.endswith(f"(in {tmp_path / 'prefix' / 'clio-agent'})")


def test_source_ref_pins_a_commit(tmp_path: Path) -> None:
    streamed = tmp_path / "streamed" / "install.sh"
    streamed.parent.mkdir()
    shutil.copy(ROOT / "install/install.sh", streamed)
    commit = "0123456789abcdef0123456789abcdef01234567"
    result, calls = run_installer(tmp_path, "--source", "--ref", commit, script=streamed)
    assert result.returncode == STOP, result.stdout + result.stderr
    # A commit is not a branch: the clone by name fails, and it is fetched by id.
    assert any(call.endswith(f"fetch -q --depth 1 origin {commit}") for call in calls), calls
    assert "--locked" in _call(calls, "uv sync")


def test_source_from_a_checkout_installs_it_in_place(tmp_path: Path) -> None:
    checkout = tmp_path / "my-clone"
    (checkout / "install").mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "clio-agent"\n')
    (checkout / ".python-version").write_text("3.14\n")
    shutil.copy(ROOT / "install/install.sh", checkout / "install/install.sh")
    result, calls = run_installer(tmp_path, "--source", script=checkout / "install/install.sh")
    assert result.returncode == STOP, result.stdout + result.stderr
    assert not any(call.startswith("git clone") for call in calls)
    assert f"git -C {checkout} submodule update --init --recursive" in calls
    sync = _call(calls, "uv sync")
    assert sync.endswith(f"(in {checkout})") and "--python 3.14" in sync
    link = tmp_path / "prefix" / "clio-agent"
    assert link.is_symlink() and link.resolve() == checkout.resolve()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--source", "--version", "0.9.5b8"), "use --ref"),
        (("--ref", "develop"), "add --source"),
        (("--bogus",), "unknown option"),
    ],
)
def test_contradictory_options_fail_and_say_why(
    tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    result, calls = run_installer(tmp_path, *args)
    assert result.returncode != 0
    assert message in result.stderr
    assert not any(call.startswith(("uv pip", "uv sync", "git clone")) for call in calls)


def test_clio_ref_still_means_source(tmp_path: Path) -> None:
    streamed = tmp_path / "streamed" / "install.sh"
    streamed.parent.mkdir()
    shutil.copy(ROOT / "install/install.sh", streamed)
    result, calls = run_installer(tmp_path, script=streamed, env={"CLIO_REF": "develop"})
    assert result.returncode == STOP, result.stdout + result.stderr
    assert "--branch develop" in _call(calls, "git clone")


@pytest.mark.parametrize("agent_home", ["", "/srv/clio/root/user"])
def test_the_launcher_keeps_the_data_home_it_was_installed_for(
    tmp_path: Path, agent_home: str
) -> None:
    """An SSH deployment installs with CLIO_AGENT_HOME=<root>/user; starting that install
    by hand must use the same data, not the default home."""
    checkout = tmp_path / "my-clone"
    (checkout / "install").mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "clio-agent"\n')
    for name in ("install.sh", "clio", "uninstall.sh"):
        shutil.copy(ROOT / "install" / name, checkout / "install" / name)
    # A host with go builds the TUI from the checkout; this fixture's build succeeds.
    tui = checkout / "scripts/build_clio_tui.sh"
    tui.parent.mkdir()
    tui.write_text("#!/bin/sh\nexit 0\n")
    tui.chmod(0o755)
    env = {"TEST_INSTALL_CONTINUE": "1"}
    if agent_home:
        env["CLIO_AGENT_HOME"] = agent_home
    result, _calls = run_installer(
        tmp_path, "--source", script=checkout / "install/install.sh", env=env
    )
    wrapper = tmp_path / "bin" / "clio"
    assert wrapper.is_file(), result.stdout + result.stderr
    text = wrapper.read_text()
    if agent_home:
        assert f'export CLIO_AGENT_HOME="${{CLIO_AGENT_HOME:-{agent_home}}}"' in text
    else:
        assert "CLIO_AGENT_HOME" not in text
