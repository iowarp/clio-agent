"""install.sh lays down the Claude Code SDK with the managed packages (gact-tui#739).

#2242 gave ``document_install`` a ``--provider-support`` step that installs a provider's
SDK beside the other managed work, so the first Claude Code turn no longer installs it,
and install.ps1 passes ``claude_code``. install.sh passes it to both of its document
calls (the runtime install's background one and the serial one), only when the
installed clio-agent has the flag: an older release's argparse would reject it.
``CLIO_INSTALL_PROVIDER_SUPPORT`` chooses the providers; empty installs none now.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.installer_shims import ROOT, run_installer

pytestmark = pytest.mark.skipif(os.name == "nt", reason="needs bash")

INSTALL = ROOT / "install" / "install.sh"

# The stand-in for the new CLIO's Python: TEST_PROVIDER_SUPPORT is whether its
# document_install has --provider-support.
_PYTHON = r"""#!/usr/bin/env bash
echo "python $*" >> "$TEST_LOG"
case "$*" in
  *"version("*) echo 0.10.0 ;;
  *find_spec*--provider-support*) exit "$TEST_PROVIDER_SUPPORT" ;;
  *find_spec*_run_store_migrations*) exit 3 ;;
  *find_spec*) exit 0 ;;
  *"-m clio_agent.runtime.document_install"*) echo '{"status": "ready"}' ;;
esac
exit 0
"""


def _document_calls(calls: list[str]) -> list[str]:
    return [call for call in calls if "-m clio_agent.runtime.document_install" in call]


def _serial_install(tmp_path: Path, **env: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    checkout = tmp_path / "my-clone"
    (checkout / "install").mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "clio-agent"\n')
    for name in ("install.sh", "clio", "uninstall.sh"):
        shutil.copy(INSTALL.parent / name, checkout / "install" / name)
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(_PYTHON)
    python.chmod(0o755)
    result, calls = run_installer(
        tmp_path,
        "--source",
        script=checkout / "install/install.sh",
        env={"TEST_INSTALL_CONTINUE": "1", "TEST_PROVIDER_SUPPORT": "0", **env},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (document,) = _document_calls(calls)
    return document


def _background_install(tmp_path: Path, **env: str) -> str:
    """Run install.sh's start_document_install (the runtime path) with the stand-in."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    text = INSTALL.read_text()
    functions = text[text.index("has_module() {") : text.index("# ---------- install clio-agent")]
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text(_PYTHON)
    (venv / "bin" / "python").chmod(0o755)
    log = tmp_path / "calls.log"
    script = (
        "set -e\n"
        "say() { :; }; finish_downloads() { :; }\n"
        f'VENV="{venv}"; DESKTOP_SCRATCH="{tmp_path}"; DESKTOP_PLAN_WAITS=1; INSTALL_DOWNLOADS=\n'
        'PROVIDER_SUPPORT="${CLIO_INSTALL_PROVIDER_SUPPORT-claude_code}"\n'
        + functions
        + '\nstart_document_install\nwait "$DOC_JOB"\nwait "$NODE_JOB"\n'
    )
    base = {k: v for k, v in os.environ.items() if not k.startswith("CLIO_")}
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**base, "TEST_LOG": str(log), "TEST_PROVIDER_SUPPORT": "0", **env},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (document,) = _document_calls(log.read_text().splitlines())
    return document


@pytest.mark.parametrize("install", [_serial_install, _background_install])
def test_both_document_calls_lay_down_the_claude_code_sdk(tmp_path: Path, install) -> None:
    """SABOTAGE: drop the flag from either call and the first turn installs the SDK."""
    assert "--provider-support claude_code" in install(tmp_path)


@pytest.mark.parametrize("install", [_serial_install, _background_install])
def test_the_environment_chooses_the_providers_and_empty_installs_none(
    tmp_path: Path, install
) -> None:
    chosen = install(tmp_path / "chosen", CLIO_INSTALL_PROVIDER_SUPPORT="claude_code,copilot")
    assert "--provider-support claude_code,copilot" in chosen
    assert "--provider-support" not in install(tmp_path / "none", CLIO_INSTALL_PROVIDER_SUPPORT="")


@pytest.mark.parametrize("install", [_serial_install, _background_install])
def test_a_release_without_the_step_is_not_given_the_flag(tmp_path: Path, install) -> None:
    """SABOTAGE: pass it unconditionally and an older release's argparse fails the install."""
    assert "--provider-support" not in install(tmp_path, TEST_PROVIDER_SUPPORT="3")
