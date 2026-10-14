"""Official-client process ownership and private failure reporting."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from clio_agent.gact.storage import obsidian_sync
from clio_agent.gact.storage.models import CreateSource, SourceConfiguration, SourceRecord
from clio_agent.gact.storage.service import StorageService


@pytest.fixture
def record(tmp_path: Path) -> SourceRecord:
    """Create a real configured vault; subprocess doubles are confined to these tests."""
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    storage = StorageService(tmp_path / "storage", tmp_path / "auth" / "credentials.json")
    return storage.create(
        "w",
        CreateSource(
            provider="obsidian",
            label="Sync copy",
            root=str(vault),
            configuration=SourceConfiguration(obsidian_connection="headless_sync"),
        ),
    )


def test_sync_owns_only_its_process_and_keeps_client_output_private(
    record: SourceRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated starts reuse a client, and shutdown terminates and reaps it."""
    monkeypatch.setattr(obsidian_sync, "client_command", lambda: ["ob"])
    runner = Mock(return_value=Mock(returncode=0))
    process = Mock()
    process.poll.return_value = None
    launcher = Mock(return_value=process)
    monkeypatch.setattr(obsidian_sync.subprocess, "run", runner)
    monkeypatch.setattr(obsidian_sync.subprocess, "Popen", launcher)
    sync = obsidian_sync.ObsidianSync()
    assert sync.start(record)["state"] == "running"
    assert sync.start(record)["state"] == "running"
    assert launcher.call_count == 1 and runner.call_count == 2
    assert runner.call_args.args[0][-2:] == ["--mode", "pull-only"]
    for call in [*runner.call_args_list, launcher.call_args]:
        assert call.kwargs["stdout"] == subprocess.DEVNULL
        assert call.kwargs["stderr"] == subprocess.DEVNULL
        assert call.kwargs["stdin"] == subprocess.DEVNULL
    process.poll.return_value = 7
    assert sync.status(record)["state"] == "failed"
    assert "7" not in str(sync.status(record)["error"])
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("ob", 5), 0]
    sync.shutdown()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert sync.status(record)["state"] == "stopped"


def test_sync_setup_failure_does_not_start_client(
    record: SourceRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing login/setup gives a useful error without returning authentication output."""
    monkeypatch.setattr(obsidian_sync, "client_command", lambda: ["ob"])
    monkeypatch.setattr(obsidian_sync.subprocess, "run", Mock(return_value=Mock(returncode=1)))
    launcher = Mock()
    monkeypatch.setattr(obsidian_sync.subprocess, "Popen", launcher)
    with pytest.raises(ValueError, match="CLIO's Obsidian setup"):
        obsidian_sync.ObsidianSync().start(record)
    launcher.assert_not_called()
    record.source = record.source.model_copy(update={"connection": "local_vault"})
    with pytest.raises(ValueError, match="Headless Sync vault"):
        obsidian_sync.ObsidianSync().start(record)


def test_sync_subfolder_uses_enclosing_vault_without_changing_source_scope(
    record: SourceRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Official sync targets the configured copy while the knowledge source keeps its folder."""
    root = Path(record.source.root)
    project = root / "Research"
    project.mkdir()
    record.source = record.source.model_copy(update={"root": str(project)})
    monkeypatch.setattr(obsidian_sync, "client_command", lambda: ["ob"])
    runner = Mock(return_value=Mock(returncode=0))
    process = Mock()
    process.poll.return_value = None
    launcher = Mock(return_value=process)
    monkeypatch.setattr(obsidian_sync.subprocess, "run", runner)
    monkeypatch.setattr(obsidian_sync.subprocess, "Popen", launcher)
    sync = obsidian_sync.ObsidianSync()
    assert sync.start(record)["state"] == "running"
    for call in [*runner.call_args_list, launcher.call_args]:
        args = call.args[0]
        assert args[args.index("--path") + 1] == str(root)
    assert record.source.root == str(project)
    sync.shutdown()


def test_sync_selects_managed_client_instead_of_personal_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sync owner uses CLIO's verified client even when a personal ob is configured."""
    monkeypatch.setenv("CLIO_OBSIDIAN_HEADLESS_CLI", str(tmp_path / "personal-ob.cmd"))
    managed = Mock()
    managed.command.return_value = ["managed-node.exe", "managed-cli.js"]
    monkeypatch.setattr(obsidian_sync, "ManagedObsidianClient", lambda: managed)
    assert obsidian_sync.client_command() == ["managed-node.exe", "managed-cli.js"]
