"""A scoped POSIX install keeps clio-kit in its own uv tool folders (gact-tui#739).

install.sh ran a global ``uv tool install clio-kit`` even for an installation with its
own prefix, home, data or user folder, replacing the user's own clio-kit (#2226 fixed
the same in install.ps1). The launcher must then find that clio-kit, and the
uninstaller must remove it without touching the user's global uv tools.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install" / "install.sh"
LAUNCHER = ROOT / "install" / "clio"
UNINSTALL = ROOT / "install" / "uninstall.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer")

_FAKE_UV = """#!/bin/sh
# Records each call with the uv tool folders it saw.
echo "$* | UV_TOOL_DIR=${UV_TOOL_DIR:-} UV_TOOL_BIN_DIR=${UV_TOOL_BIN_DIR:-}" >> "$UV_CALLS"
[ "$1 $2" = "tool list" ] && [ -n "${USER_HAS_KIT:-}" ] && echo "clio-kit v2.12.7"
exit 0
"""


def _kit_block() -> str:
    text = INSTALL.read_text()
    return text[
        text.index("# ---------- clio-kit MCP runtime") : text.index("# start_submodules SRC")
    ]


def _run_kit(tmp_path: Path, *, prior_record: bool = False, **env: str) -> tuple[Path, str]:
    """Run install.sh's clio-kit step with a fake uv; returns (prefix, uv calls)."""
    prefix = tmp_path / "prefix"
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uv").write_text(_FAKE_UV)
    (fake / "uv").chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    calls = tmp_path / "uv-calls"
    if prior_record:
        (prefix / "clio-agent").mkdir(parents=True)
    script = (
        "set -e\n"
        'say() { echo "==> $*"; }; warn() { echo "warn: $*" >&2; }\n'
        'need_uv() { :; }; have() { command -v "$1" >/dev/null; }\n'
        f'PREFIX="{prefix}"; DESKTOP_SCRATCH="{scratch}"; CLIO_KIT_PACKAGE=""\n'
        'INSTALLED_TOOLS="$PREFIX/installed-tools"\n'
        'UNRECORDED_INSTALL=0; [ -e "$INSTALLED_TOOLS" ] || [ ! -d "$PREFIX/clio-agent" ] '
        "|| UNRECORDED_INSTALL=1\n"
        'record_installed() { printf \'%s %s\\n\' "$1" "$2" >> "$INSTALLED_TOOLS"; }\n'
        + _kit_block()
        + "\nstart_clio_kit 3.14\nfinish_clio_kit\n"
    )
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CLIO_", "UV_"))}
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**base, "PATH": f"{fake}:{base['PATH']}", "UV_CALLS": str(calls), **env},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return prefix, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize(
    "scope", ["CLIO_AGENT_HOME", "CLIO_PREFIX", "CLIO_AGENT_DATA_DIR", "CLIO_USER_DIR"]
)
def test_a_scoped_install_puts_clio_kit_in_its_own_tool_folders(tmp_path: Path, scope: str) -> None:
    prefix, calls = _run_kit(tmp_path, **{scope: str(tmp_path / "scope")})

    tools = prefix / "uv-tools"
    install = [line for line in calls.splitlines() if line.startswith("tool install")]
    assert install == [
        f"tool install --python 3.14 clio-kit==2.12.7 | UV_TOOL_DIR={tools} "
        f"UV_TOOL_BIN_DIR={tools}/bin"
    ]
    assert not any(line.startswith("tool list") for line in calls.splitlines()), (
        "a scoped install never asks about the user's global tools"
    )
    # Nothing global to remove later, but the record exists so uninstall.sh never
    # mistakes this for an older installation that put clio-kit in the global tools.
    assert (prefix / "installed-tools").read_text() == ""


def test_a_default_install_keeps_using_the_users_uv_tools(tmp_path: Path) -> None:
    prefix, calls = _run_kit(tmp_path)

    assert "tool install --python 3.14 clio-kit==2.12.7 | UV_TOOL_DIR= UV_TOOL_BIN_DIR=" in calls
    assert (prefix / "installed-tools").read_text() == "uv-tool clio-kit\n"


def test_a_default_install_does_not_claim_the_users_own_clio_kit(tmp_path: Path) -> None:
    prefix, _ = _run_kit(tmp_path, USER_HAS_KIT="1")

    assert (prefix / "installed-tools").read_text() == ""


def test_a_callers_own_uv_tool_folders_are_kept(tmp_path: Path) -> None:
    # An SSH deployment exports its own UV_TOOL_DIR/UV_TOOL_BIN_DIR before install.sh.
    deploy = tmp_path / "deploy"
    prefix, calls = _run_kit(
        tmp_path,
        CLIO_AGENT_HOME=str(tmp_path / "scope"),
        UV_TOOL_DIR=str(deploy / "uv-tools"),
        UV_TOOL_BIN_DIR=str(deploy / "bin"),
    )

    assert f"UV_TOOL_DIR={deploy}/uv-tools UV_TOOL_BIN_DIR={deploy}/bin" in calls
    # Its clio-kit is not in the user's global tools, so uninstall must not remove that one.
    assert (prefix / "installed-tools").read_text() == ""


def test_an_older_scoped_install_keeps_the_record_of_its_shared_clio_kit(tmp_path: Path) -> None:
    # Installed before scoping (no record): its installer put clio-kit in the global
    # tools, so the record keeps uninstall.sh removing that one.
    prefix, _ = _run_kit(tmp_path, prior_record=True, CLIO_AGENT_HOME=str(tmp_path / "scope"))

    assert (prefix / "installed-tools").read_text() == "uv-tool clio-kit\n"


def _launcher_env(tmp_path: Path, with_kit: bool) -> dict[str, str]:
    prefix = tmp_path / "prefix"
    user_bin = tmp_path / "bin"
    user_bin.mkdir()
    (user_bin / "clio-kit").write_text("#!/bin/sh\n")
    (user_bin / "clio-kit").chmod(0o755)
    if with_kit:
        kit = prefix / "uv-tools" / "bin" / "clio-kit"
        kit.parent.mkdir(parents=True)
        kit.write_text("#!/bin/sh\n")
        kit.chmod(0o755)
    text = LAUNCHER.read_text()
    block = text[
        text.index('export PATH="$CLIO_BIN_DIR:$PATH"') : text.index("# The installer provisions")
    ]
    script = (
        f'CLIO_PREFIX="{prefix}"; CLIO_BIN_DIR="{user_bin}"\n'
        + block
        + "\nprintf 'kit=%s\\ntools=%s\\nbin=%s\\n' \"$(command -v clio-kit)\" "
        '"${UV_TOOL_DIR:-}" "${UV_TOOL_BIN_DIR:-}"\n'
    )
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CLIO_", "UV_"))}
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=base, check=True, timeout=30
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


def test_the_launcher_puts_a_scoped_clio_kit_first(tmp_path: Path) -> None:
    seen = _launcher_env(tmp_path, with_kit=True)

    tools = tmp_path / "prefix" / "uv-tools"
    assert seen == {"kit": f"{tools}/bin/clio-kit", "tools": str(tools), "bin": f"{tools}/bin"}
    # The detached server gets the same PATH order.
    assert 'export PATH="$CLIO_KIT_PATH$CLIO_BIN_DIR:$PATH"' in LAUNCHER.read_text()


def test_the_launcher_leaves_a_default_install_on_the_users_clio_kit(tmp_path: Path) -> None:
    seen = _launcher_env(tmp_path, with_kit=False)

    assert seen == {"kit": f"{tmp_path}/bin/clio-kit", "tools": "", "bin": ""}


def test_uninstall_removes_the_scoped_clio_kit_and_not_the_users(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    (prefix / "clio-agent").mkdir(parents=True)
    (prefix / "installed-tools").write_text("")
    kit = prefix / "uv-tools" / "clio-kit" / "bin"
    kit.mkdir(parents=True)
    (kit / "python").write_text("x")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uv").write_text(_FAKE_UV)
    (fake / "uv").chmod(0o755)
    calls = tmp_path / "uv-calls"
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CLIO_", "XDG_", "UV_"))}
    env = {
        **base,
        "HOME": str(tmp_path / "home"),
        "CLIO_PREFIX": str(prefix),
        "CLIO_BIN_DIR": str(tmp_path / "bin"),
        "CLIO_PORT": "1",
        "PATH": f"{fake}:{base['PATH']}",
        "UV_CALLS": str(calls),
        "USER_HAS_KIT": "1",
    }
    result = subprocess.run(
        ["bash", str(UNINSTALL), "--yes"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (prefix / "uv-tools").exists()
    assert "tool uninstall" not in (calls.read_text() if calls.exists() else "")
