"""The bootstrap must protect data even with a destructive older release installer."""

from pathlib import Path

import pytest

from tests.test_gact.test_clio_agent_deploy import _install, linux_only


@linux_only
@pytest.mark.parametrize("fail", [False, True])
def test_older_installer_cannot_erase_sessions_or_unknown_content(
    tmp_path: Path, fail: bool
) -> None:
    program = tmp_path / "clio" / "clio-agent"
    sessions = program / ".clio" / "agent" / "sessions.json"
    sessions.parent.mkdir(parents=True)
    sessions.write_bytes(b"valuable sessions")
    (program / "my-notebook.txt").write_bytes(b"keep me")
    (program / ".venv").mkdir()
    (program / ".venv" / "old-version").write_bytes(b"old")
    installer = """rm -rf "$CLIO_PREFIX/clio-agent"
mkdir -p "$CLIO_PREFIX/clio-agent/.venv"
echo new > "$CLIO_PREFIX/clio-agent/.venv/new-version"
"""
    result = _install(tmp_path, pypi="200", installer=installer + ("exit 9\n" if fail else ""))
    assert result.returncode == (9 if fail else 0), result.stdout + result.stderr
    assert sessions.read_bytes() == b"valuable sessions"
    assert (program / "my-notebook.txt").read_bytes() == b"keep me"
    assert (program / ".venv" / ("old-version" if fail else "new-version")).is_file()


@linux_only
def test_unpublished_upgrade_does_not_move_existing_data(tmp_path: Path) -> None:
    program = tmp_path / "clio" / "clio-agent"
    program.mkdir(parents=True)
    (program / "keep").write_bytes(b"untouched")
    result = _install(tmp_path, pypi="404", installer="exit 0\n")
    assert result.returncode == 78
    assert (program / "keep").read_bytes() == b"untouched"
    assert not (program.parent / "user/state/install-backups").exists()
