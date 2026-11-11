"""install.sh upgrades the store inside the document step when that step can (gact-tui#739).

A clio-agent whose ``document_install`` runs ``clio-agent migrate`` itself does so while
its packages install, so install.sh no longer runs a serial ``migrate`` first (13 s of a
cold macOS install). Its summary line reports the migration, and install.sh warns about a
refusal or failure exactly as it does for ``migrate``'s exit status. An older release
still gets the separate ``migrate`` and ``--skip-store-migrations``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.test_scripts.installer_shims import ROOT, run_installer, shell_path

pytestmark = pytest.mark.skipif(os.name == "nt", reason="needs bash")

# The stand-in for the new CLIO's Python records its calls; the summary parser is the
# only call it hands to a real Python, so install.sh's own parsing is what is tested.
_PYTHON = r"""#!/usr/bin/env bash
echo "python $*" >> "$TEST_LOG"
case "$*" in
  *json.loads*) exec "$TEST_REAL_PYTHON" "$@" ;;
  *"version("*) echo 0.10.0 ;;
  *find_spec*_run_store_migrations*) exit "$TEST_DOCS_MIGRATE" ;;
  *find_spec*) exit 0 ;;
  *"-m clio_agent.runtime.document_install"*)
    echo "Python document packages are ready."
    echo "$TEST_SUMMARY" ;;
esac
exit 0
"""


def _install(
    tmp_path: Path, *, docs_migrate: bool, migrations: dict[str, str]
) -> tuple[str, list[str]]:
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
    summary = {"status": "ready", "store_migrations": migrations}
    env = {
        "TEST_INSTALL_CONTINUE": "1",
        "TEST_DOCS_MIGRATE": "0" if docs_migrate else "3",
        "TEST_SUMMARY": json.dumps(summary),
        "TEST_REAL_PYTHON": shell_path(Path(sys.executable)),
    }
    result, calls = run_installer(
        tmp_path, "--source", script=checkout / "install/install.sh", env=env
    )
    shown = result.stdout + result.stderr
    assert result.returncode == 0, shown
    return shown, calls


def _document_calls(calls: list[str]) -> list[str]:
    return [call for call in calls if "-m clio_agent.runtime.document_install" in call]


def test_the_document_step_migrates_and_no_serial_migrate_runs(tmp_path: Path) -> None:
    """SABOTAGE: run ``migrate`` before the document step again and the 13 s returns."""
    shown, calls = _install(tmp_path, docs_migrate=True, migrations={"status": "done"})
    assert not [call for call in calls if "-m clio_agent.gact.store_migrate_command" in call]
    (document,) = _document_calls(calls)
    assert "--skip-store-migrations" not in document
    assert "Python document packages are ready." in shown
    for warning in ("store in use", "clio-agent migrate failed", "migrate did not run"):
        assert warning not in shown


@pytest.mark.parametrize(
    ("migrations", "warning"),
    [
        ({"status": "refused", "reason": "store_in_use"}, "store in use by a running CLIO"),
        ({"status": "refused", "reason": "store_format_old"}, "written by CLIO 0.9.5.1 or older"),
        ({"status": "failed", "reason": "store_unavailable"}, "clio-agent migrate failed"),
    ],
)
def test_a_refused_or_failed_migration_is_warned_as_before(
    tmp_path: Path, migrations: dict[str, str], warning: str
) -> None:
    shown, _calls = _install(tmp_path, docs_migrate=True, migrations=migrations)
    assert warning in shown


def test_an_older_document_step_gets_the_separate_migrate(tmp_path: Path) -> None:
    _shown, calls = _install(tmp_path, docs_migrate=False, migrations={"status": "skipped"})
    migrate = [i for i, c in enumerate(calls) if "-m clio_agent.gact.store_migrate_command" in c]
    document = [i for i, c in enumerate(calls) if "-m clio_agent.runtime.document_install" in c]
    assert migrate and document and migrate[0] < document[0]
    assert "--skip-store-migrations" in calls[document[0]]
