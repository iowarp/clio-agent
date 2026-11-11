"""An upgrade over data CLIO 0.9.5.1 or older wrote: said plainly, moved aside on a yes.

install.sh runs the new CLIO's ``store-check`` (which reads the store itself) before the
store migrations, then, by its exit status:

* 5 (written by 0.9.5.1 or older): asks on a terminal (default yes); without a
  terminal, or with ``--reset-old-data``, moves it and says so; a "no" leaves it;
* 6 or 4 (cannot tell): says so and asks (default no); without a terminal it never
  moves it;
* 3 (CLIO is running): says so and moves nothing;
* 0 (this version's data, or none): nothing.

The move itself is ``reset-store --yes`` (tests/test_gact/test_store_reset.py covers it).
Here the new CLIO's Python is a stand-in that answers ``store-check`` with the status a
test sets and records every call.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.test_scripts.installer_shims import ROOT, run_installer

pytestmark = pytest.mark.skipif(os.name == "nt", reason="needs bash and a POSIX terminal")

_PYTHON = r"""#!/usr/bin/env bash
echo "python $*" >> "$TEST_LOG"
case "$*" in
  *"version("*) echo 0.10.0 ;;
  *find_spec*) exit 0 ;;
  *"store_reset_command check"*)
    echo "STORE-CHECK says: written by CLIO 0.9.5.1 or older"
    exit "$TEST_CHECK_STATUS" ;;
  *"store_reset_command reset --yes"*) echo "Moved your CLIO data aside, unchanged" ;;
esac
exit 0
"""


def _install(
    tmp_path: Path, check_status: int, *args: str, typed: str | None = None
) -> tuple[str, list[str]]:
    """Install a checkout in place whose CLIO answers ``store-check`` with ``check_status``."""
    checkout = tmp_path / "my-clone"
    (checkout / "install").mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "clio-agent"\n')
    for name in ("install.sh", "clio", "uninstall.sh"):
        shutil.copy(ROOT / "install" / name, checkout / "install" / name)
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(_PYTHON)
    python.chmod(0o755)
    env = {"TEST_INSTALL_CONTINUE": "1", "TEST_CHECK_STATUS": str(check_status)}
    result, calls = run_installer(
        tmp_path,
        "--source",
        *args,
        script=checkout / "install/install.sh",
        env=env,
        terminal_input=typed,
    )
    shown = result.stdout + result.stderr
    assert result.returncode == 0, shown
    return shown, calls


def _moved(calls: list[str]) -> bool:
    return any("store_reset_command reset --yes" in call for call in calls)


def _checked_before_migrating(calls: list[str]) -> None:
    order = [call for call in calls if "store_reset_command check" in call or "migrate" in call]
    assert "store_reset_command check" in order[0]


def test_old_data_is_moved_after_a_yes(tmp_path: Path) -> None:
    """SABOTAGE: skip the question (or the move) in install.sh's check_old_data and the
    calls below change; an old store would be left for the boot to refuse."""
    shown, calls = _install(tmp_path, 5, typed="y\n")
    assert "written by CLIO 0.9.5.1 or older" in shown
    assert "Move it aside now? [Y/n]" in shown
    assert _moved(calls)
    _checked_before_migrating(calls)


def test_old_data_is_moved_on_enter_and_left_on_a_no(tmp_path: Path) -> None:
    _shown, calls = _install(tmp_path / "enter", 5, typed="\n")
    assert _moved(calls)
    shown, calls = _install(tmp_path / "no", 5, typed="n\n")
    assert not _moved(calls)
    assert "Your data was left in place" in shown and "clio reset-store" in shown


@pytest.mark.parametrize("args", [(), ("--reset-old-data",)])
def test_without_a_terminal_old_data_is_moved_and_said(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    shown, calls = _install(tmp_path, 5, *args)
    assert _moved(calls)
    assert "Move it aside now?" not in shown
    assert "Moved your CLIO data aside" in shown
    assert ("--reset-old-data" if args else "No terminal to ask on") in shown


def test_data_this_version_wrote_is_left_alone_without_a_question(tmp_path: Path) -> None:
    shown, calls = _install(tmp_path, 0, typed="y\n")
    assert not _moved(calls)
    assert "Move it aside" not in shown
    _checked_before_migrating(calls)


@pytest.mark.parametrize("status", [6, 4])
def test_data_whose_writer_cannot_be_told_is_asked_about_never_moved_silently(
    tmp_path: Path, status: int
) -> None:
    """SABOTAGE: treat "cannot tell" like "old" and the unattended install moves it."""
    shown, calls = _install(tmp_path / "unattended", status, "--reset-old-data")
    assert not _moved(calls)
    assert "cannot tell whether your data must be moved aside" in shown
    shown, calls = _install(tmp_path / "enter", status, typed="\n")
    assert "Move it aside anyway? [y/N]" in shown
    assert not _moved(calls)
    _shown, calls = _install(tmp_path / "yes", status, typed="yes\n")
    assert _moved(calls)


def test_a_running_clio_is_named_and_nothing_moves(tmp_path: Path) -> None:
    shown, calls = _install(tmp_path, 3, "--reset-old-data")
    assert not _moved(calls)
    assert "CLIO is running, so your data was not checked" in shown
    assert "clio stop" in shown


def test_the_option_is_documented() -> None:
    script = (ROOT / "install/install.sh").read_text()
    assert "--reset-old-data" in script.split("set -euo pipefail")[0]
    windows = (ROOT / "install/install.ps1").read_text(encoding="utf-8")
    # The Windows installer takes the same decisions (no PowerShell runs here).
    for text in (
        "-ResetOldData",
        "store_reset_command check",
        "store_reset_command reset --yes",
        "'Move it aside now? [Y/n]' 'y'",
        "'Move it aside anyway? [y/N]' 'n'",
        "$checkStatus -eq 3",
        "$checkStatus -eq 5",
    ):
        assert text in windows, text
    assert windows.index("Invoke-OldDataCheck\n") < windows.index("store_migrate_command\n")
