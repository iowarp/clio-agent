"""The four ways install.sh installs CLIO, and their pins.

install.sh                 latest stable release
install.sh --dev           latest release, pre-releases included
install.sh --source        source at main (or the checkout it runs from, in place)
install.sh --dev --source  source at develop
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.installer_shims import (
    RELEASE_STOP,
    ROOT,
    STOP,
    run_installer,
    shell_path,
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
    assert result.returncode == RELEASE_STOP, result.stdout + result.stderr
    assert "could not install clio-agent" in result.stderr
    assert "--python 3.13" in _call(calls, "uv venv")  # 0.9.4.24 predates .python-version
    install = _call(calls, "uv pip install")
    assert "clio-agent[argonne,flowcept]==0.9.4.24" in install
    assert "--constraint" in install
    assert "curl https://raw.githubusercontent.com/iowarp/clio-agent/v0.9.4.24/uv.lock" in calls


def test_dev_installs_the_latest_release_including_pre_releases(tmp_path: Path) -> None:
    _release_fixtures(tmp_path)
    result, calls = run_installer(tmp_path, "--dev")
    assert result.returncode == RELEASE_STOP, result.stdout + result.stderr
    assert "could not install clio-agent" in result.stderr
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
    assert result.returncode == RELEASE_STOP, result.stdout + result.stderr
    assert "could not install clio-agent" in result.stderr
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
    assert sync.endswith(f"(in {shell_path(tmp_path / 'prefix' / 'clio-agent')})")


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
    assert f"git -C {shell_path(checkout)} submodule update --init --recursive" in calls
    sync = _call(calls, "uv sync")
    assert sync.endswith(f"(in {shell_path(checkout)})") and "--python 3.14" in sync
    link = tmp_path / "prefix" / "clio-agent"
    assert f"ln -sfn {shell_path(checkout)} {shell_path(link)}" in calls
    if os.name != "nt":
        assert link.is_symlink() and link.resolve() == checkout.resolve()
    else:
        # Git Bash can emulate ln with a directory copy; Windows installs use
        # install.ps1. The Bash installer must still request the exact link.
        assert link.is_dir()


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
    # Retained TUI source must not be built, even on a host with Go.
    tui = checkout / "scripts/build_clio_tui.sh"
    tui.parent.mkdir()
    tui.write_text("#!/bin/sh\necho UNEXPECTED_TUI_BUILD >&2\nexit 95\n")
    tui.chmod(0o755)
    env = {"TEST_INSTALL_CONTINUE": "1"}
    if agent_home:
        env["CLIO_AGENT_HOME"] = agent_home
    result, _calls = run_installer(
        tmp_path, "--source", script=checkout / "install/install.sh", env=env
    )
    wrapper = tmp_path / "bin" / "clio"
    assert wrapper.is_file(), result.stdout + result.stderr
    assert "UNEXPECTED_TUI_BUILD" not in result.stdout + result.stderr
    assert not (tmp_path / "prefix" / "gact").exists()
    text = wrapper.read_text()
    if agent_home:
        assert f'export CLIO_AGENT_HOME="${{CLIO_AGENT_HOME:-{agent_home}}}"' in text
    else:
        assert "CLIO_AGENT_HOME" not in text


def test_the_path_note_reads_the_callers_path_not_the_one_the_installer_extended() -> None:
    # The installer prepends its bin dir to PATH for its own use; the finishing note
    # must still tell someone whose shell lacks it to add it.
    text = (ROOT / "install/install.sh").read_text()
    captured = text.index('CALLER_PATH="${PATH:-}"')
    assert captured < text.index('export PATH="$BIN_DIR:$PATH"')
    assert 'case ":$CALLER_PATH:" in\n  *":$BIN_DIR:"*) path_step=' in text


def test_the_finishing_note_offers_web_only_when_the_web_bundle_is_installed() -> None:
    text = (ROOT / "install/install.sh").read_text()
    notes = text[text.index('if [ -f "$PREFIX/clio-agent/web/index.html" ]; then') :]
    web, rest = notes.split("else", 1)
    assert "clio (Web)" in web
    assert "clio (Web)" not in rest.split("fi", 1)[0]
    assert "  2. $run_step\n" in notes


def _install_name_tool_stand_in() -> str:
    text = (ROOT / "install/install.sh").read_text()
    return text[text.index("macos_developer_tools() {") : text.index("# uv installs Python,")]


def test_a_mac_without_developer_tools_never_runs_the_install_name_tool_placeholder() -> None:
    # uv patches each Python it installs with install_name_tool; on a Mac without the
    # Command Line Tools /usr/bin/install_name_tool opens their install dialog
    # (gact-tui#739). The failing stand-in is on PATH before the first uv call.
    text = (ROOT / "install/install.sh").read_text()
    block = _install_name_tool_stand_in()
    assert 'export PATH="$tool_shim:$PATH"' in block
    assert text.index(block) < text.index("curl -LsSf https://astral.sh/uv/install.sh")
    assert text.index(block) < text.index("uv tool install")
    assert '[ -z "${tool_shim:-}" ] || rm -rf "$tool_shim"' in text[text.index("on_exit() {") :]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
@pytest.mark.parametrize("developer_dir", [True, False])
def test_the_stand_in_is_used_only_without_a_developer_directory(
    tmp_path: Path, developer_dir: bool
) -> None:
    developer = tmp_path / "Developer"
    if developer_dir:
        (developer / "usr/bin").mkdir(parents=True)
    script = (
        f"OS=darwin\n{_install_name_tool_stand_in()}\n"
        'printf "%s\\n" "$tool_shim"\n'
        '[ -z "$tool_shim" ] || { "$tool_shim/install_name_tool"; echo "status $?"; }\n'
    )
    # Isolated from the machine's own developer tools: only DEVELOPER_DIR can supply them.
    script = script.replace(
        "/Applications/Xcode.app/Contents/Developer \\\n      /Library/Developer/CommandLineTools",
        f"{tmp_path}/no-xcode {tmp_path}/no-clt",
    ).replace("readlink /var/db/xcode_select_link", f"readlink {tmp_path}/no-link")
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-c", script],
        env={**os.environ, "DEVELOPER_DIR": str(developer), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    )
    shim = result.stdout.splitlines()[0]
    if developer_dir:
        assert shim == ""
    else:
        assert shim.startswith(str(tmp_path / "clio-tools."))
        assert "status 1" in result.stdout
        assert "needs the Command Line Tools" in result.stderr


# ---- the release's prebuilt agent: a plain macOS install unpacks the bundled runtime


def _runtime_block() -> str:
    text = (ROOT / "install/install.sh").read_text()
    helpers = text[text.index("manifest_sha() {") : text.index("# fetch_hashed URL FILE")]
    block = text[
        text.index("# ---------- the release's prebuilt agent") : text.index(
            "# ---------- clio-kit MCP runtime"
        )
    ]
    return helpers + 'is_sha() { [[ "$1" =~ ^[0-9a-f]{64}$ ]]; }\n' + block


_RUNTIME_ASSET = "CLIO.Desktop-aarch64-apple-darwin-bundled.app.tar.gz"


def _runtime_archive(tmp_path: Path, version: str) -> tuple[Path, str]:
    import hashlib
    import tarfile

    app = tmp_path / "pkg/CLIO Desktop.app"
    runtime = app / "Contents/Resources/gact-runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "python/bin").mkdir(parents=True)
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/clio-desktop").write_text("the app itself\n")
    (runtime / "runtime.json").write_text("{}\n")
    python = runtime / "python/bin/python3.14"
    python.write_text(f"#!/bin/sh\necho {version}\n")
    python.chmod(0o755)
    (runtime / "python/bin/python3.14-config").write_text("")
    archive = tmp_path / _RUNTIME_ASSET
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(app, arcname="CLIO Desktop.app")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def _install_runtime(
    tmp_path: Path, archive: Path, sha: str | None, *, os_name: str = "darwin", **env: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    prefix = tmp_path / "prefix"
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    venv = prefix / "clio-agent/.venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin/old").write_text("the previous environment\n")
    script = (
        "set -euo pipefail\n"
        'have() { command -v "$1" >/dev/null 2>&1; }\n'
        'say() { echo "say: $*"; }\n'
        'warn() { echo "warn: $*" >&2; }\n'
        'die() { echo "die: $*" >&2; exit 1; }\n'
        "sw_vers() { echo 15.4; }\n"
        f'curl() {{ echo "curl $*" >&2; case "$*" in *SHA256SUMS*) return 22 ;; esac; '
        f"cat {shell_path(archive)!r}; }}\n"
        f"OS={os_name} ARCH=arm64 REPO_URL=https://example.invalid\n"
        f"PREFIX={shell_path(prefix)!r} VENV={shell_path(venv)!r}\n"
        f"DESKTOP_SCRATCH={shell_path(scratch)!r}\n"
        f"{_runtime_block()}\n"
        "start_release_runtime v1.2.3\n"
        "if finish_release_runtime 1.2.3; then echo RESULT runtime; else echo RESULT pypi; fi\n"
    )
    if sha is not None:
        (scratch / "checksums").write_text(f"{sha}  {_RUNTIME_ASSET}\n")
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-c", script],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return result, venv


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_a_plain_macos_install_uses_the_releases_prebuilt_runtime(tmp_path: Path) -> None:
    archive, sha = _runtime_archive(tmp_path, "1.2.3")
    result, venv = _install_runtime(tmp_path, archive, sha)
    assert "RESULT runtime" in result.stdout, result.stdout + result.stderr
    fetched = (tmp_path / "scratch/runtime.log").read_text()
    assert f"/releases/download/v1.2.3/{_RUNTIME_ASSET}" in fetched
    assert os.readlink(venv / "bin/python") == "../python/bin/python3.14"
    assert os.readlink(venv / "bin/python3") == "python"
    trampoline = (venv / "bin/clio-agent").read_text()
    assert f"'''exec' '{venv}/bin/python' \"$0\" \"$@\"" in trampoline
    assert "from clio_agent.ui.cli import main" in trampoline
    assert (venv / "runtime.json").is_file()
    assert not (venv / "bin/old").exists()  # the previous environment was replaced
    assert not (tmp_path / "prefix/clio-agent/.venv.previous").exists()
    assert not (tmp_path / "prefix/clio-agent/.venv.incoming").exists()  # only the runtime


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
@pytest.mark.parametrize(
    ("case", "env"),
    [
        ("tampered", {}),
        ("other version", {}),
        ("not published", {}),
        ("no checksums", {}),
        ("pypi requested", {"CLIO_INSTALL_FROM": "pypi"}),
        ("linux", {}),
    ],
)
def test_without_a_verified_matching_runtime_the_install_uses_pypi(
    tmp_path: Path, case: str, env: dict[str, str]
) -> None:
    archive, sha = _runtime_archive(tmp_path, "9.9.9" if case == "other version" else "1.2.3")
    published: str | None = sha
    if case == "tampered":
        published = "0" * 64
    if case == "not published":
        published = ""
    if case == "no checksums":  # the release publishes no macOS checksums at all
        published = None
    result, venv = _install_runtime(
        tmp_path, archive, published, os_name="linux" if case == "linux" else "darwin", **env
    )
    assert "RESULT pypi" in result.stdout, result.stdout + result.stderr
    assert (venv / "bin/old").is_file()  # the previous environment is untouched
    assert not (tmp_path / "prefix/clio-agent/.venv.incoming").exists()
    if case in ("pypi requested", "linux", "no checksums"):
        assert not (tmp_path / "scratch/runtime.log").exists()  # no runtime was fetched
    if case in ("not published", "no checksums"):
        assert "publishes no prebuilt agent" in result.stdout


def test_the_prebuilt_agent_downloads_first_and_pypi_remains_the_fallback() -> None:
    text = (ROOT / "install/install.sh").read_text()
    plan = text.index('  install_plan="$(release_install_plan "$tag")"')
    start = text.index('  start_release_runtime "$tag"', plan)
    # The checksums the plan fetched are reused; LibreOffice and pnpm wait for the runtime.
    assert plan < start
    assert 'if [ -n "$RUNTIME_JOB" ]; then runtime_first=1; else start_install_downloads' in text
    finish = text.index('  ! finish_release_runtime "$package_version" || runtime_installed=1')
    after = text.index('  [ "$runtime_first" = 0 ] || start_install_downloads "$install_plan"')
    assert start < finish < after < text.index('say "Installing $pkg_spec from PyPI', after)
