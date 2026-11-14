"""No install builds the agents' tool servers unless asked (LZ-D1).

CLIO asks on agent load whether to enable what an agent needs and downloads each
clio-kit server then, so the installers no longer prepare them by default. The step
stays an explicit opt-in for offline or air-gapped installs: ``install.ps1
-PrepareToolServers`` (passed to ``document_install --prepare-tool-servers``) and
``install.sh --prepare-tool-servers`` (``python -m clio_agent.runtime.install_tool_servers``
once clio-kit is installed).
"""

from __future__ import annotations

import os
import re
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from clio_agent.runtime import install_tool_servers as tool_servers
from tests.test_scripts.installer_shims import ROOT, run_installer

INSTALL_SH = ROOT / "install" / "install.sh"
INSTALL_PS1 = ROOT / "install" / "install.ps1"

_PYTHON = r"""#!/usr/bin/env bash
echo "python $*" >> "$TEST_LOG"
case "$*" in
  *"version("*) echo 0.10.0 ;;
  *find_spec*_run_store_migrations*) exit 3 ;;
  *find_spec*) exit 0 ;;
  *"-m clio_agent.runtime.document_install"*) echo '{"status": "ready"}' ;;
esac
exit 0
"""


def _source_install(tmp_path: Path, *args: str) -> list[str]:
    checkout = tmp_path / "my-clone"
    (checkout / "install").mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "clio-agent"\n')
    for name in ("install.sh", "clio", "uninstall.sh"):
        shutil.copy(INSTALL_SH.parent / name, checkout / "install" / name)
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(_PYTHON)
    python.chmod(0o755)
    result, calls = run_installer(
        tmp_path,
        "--source",
        *args,
        script=checkout / "install/install.sh",
        env={"TEST_INSTALL_CONTINUE": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return calls


@pytest.mark.skipif(os.name == "nt", reason="needs bash")
def test_install_sh_prepares_no_tool_server_by_default(tmp_path: Path) -> None:
    """SABOTAGE: run the step unconditionally and every install downloads every server."""

    calls = _source_install(tmp_path)
    assert not [call for call in calls if "install_tool_servers" in call]
    assert not [call for call in calls if "--prepare-tool-servers" in call]


@pytest.mark.skipif(os.name == "nt", reason="needs bash")
def test_install_sh_prepares_them_only_when_asked(tmp_path: Path) -> None:
    calls = _source_install(tmp_path, "--prepare-tool-servers")
    assert [call for call in calls if "-m clio_agent.runtime.install_tool_servers" in call]


def test_install_ps1_passes_the_flag_only_with_its_switch() -> None:
    """Every place that adds ``--prepare-tool-servers`` is guarded by the opt-in switch."""

    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "'^(--prepare-tool-servers|-PrepareToolServers)$'" in text
    assert re.search(r"^\$OptPrepareToolServers = \$false$", text, re.M)
    lines = text.splitlines()
    adds = [
        index
        for index, line in enumerate(lines)
        if "--prepare-tool-servers" in line and ("+=" in line or "providerFlag" in line)
    ]
    assert len(adds) == 2
    for index in adds:
        # The block that adds it opens within a few lines under the switch.
        guards = [line for line in lines[max(0, index - 6) : index] if "if (" in line]
        assert any("$OptPrepareToolServers -and" in line for line in guards), guards


def test_install_readme_documents_the_offline_opt_in() -> None:
    readme = (ROOT / "install" / "README.md").read_text(encoding="utf-8")
    assert "--prepare-tool-servers" in readme and "-PrepareToolServers" in readme
    assert "prepare servers now for offline use" in readme.lower()


def test_the_module_prepares_on_its_own_and_types_its_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Namespace] = []

    def prepare(args: Namespace, marketplace, emit) -> dict:
        seen.append(args)
        assert marketplace.result() == {"status": "ready"}
        emit("Preparing the agents' data tools: hdf5...")
        return {"status": "failed", "servers": {"hdf5": {"status": "failed"}}}

    monkeypatch.setattr(tool_servers, "_prepare_tool_servers", prepare)
    assert tool_servers.main([]) == 1
    assert seen[0].prepare_tool_servers is True and seen[0].reuse_installed is False
    out = capsys.readouterr().out
    assert "Preparing the agents' data tools: hdf5" in out
    assert '"tool_servers": {"servers"' in out
    monkeypatch.setattr(tool_servers, "_prepare_tool_servers", lambda *a: {"status": "ready"})
    assert tool_servers.main([]) == 0
