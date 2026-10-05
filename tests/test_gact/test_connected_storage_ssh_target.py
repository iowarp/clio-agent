"""Exercise the SSH file protocol with real subprocesses and isolated folders."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec
from clio_agent.gact.storage.drive import drive_folder_id
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.ssh_target import SshTargetSource


def execute(spec: CommandSpec) -> CommandResult:
    """Run the actual remote script on a local test host without emulating its implementation."""
    result = subprocess.run(
        [sys.executable, *spec.args], input=spec.stdin, capture_output=True, text=True, timeout=10
    )
    assert len(result.stdout) < 16_000
    return CommandResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)


def test_registered_target_streams_folder_and_rejects_escape_and_writes(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "nested").mkdir(parents=True)
    content = bytes(range(256)) * 193
    (root / "nested" / "bytes.bin").write_bytes(content)
    for index in range(18):
        (root / f"directory-{index}").mkdir()
    adapter = SshTargetSource(str(root), execute)
    entries = adapter.entries()
    assert len(entries) == 20
    file = next(row for row in entries if row.kind == "file")
    with adapter.open_read(file) as stream:
        assert stream.read() == content
    with pytest.raises(PermissionError, match="read only"):
        adapter.apply(file.path, None, file.revision)
    with pytest.raises(ValueError, match="could not read"):
        adapter.request("read", path="../outside")
    assert (root / "nested" / "bytes.bin").read_bytes() == content


@pytest.mark.parametrize(
    "value",
    [
        "folder_ID-1",
        " https://drive.google.com/drive/folders/folder_ID-1?usp=sharing ",
        "https://drive.google.com/drive/u/0/folders/folder_ID-1",
    ],
)
def test_drive_folder_links_normalized_before_auth_binding(value: str, tmp_path: Path) -> None:
    assert drive_folder_id(value) == "folder_ID-1"
    service = StorageService(tmp_path / "data", tmp_path / "private" / "auth.json")
    record = service.create("w", CreateSource(provider="google_drive", root=value, label="Data"))
    assert service.get("w", record.source.id).source.root == "folder_ID-1"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org/folders/id",
        "https://drive.google.com.evil.test/drive/folders/id",
        "http://drive.google.com/drive/folders/id",
        "https://drive.google.com/file/d/id/view",
        "https://user@drive.google.com/drive/folders/id",
        "https://drive.google.com/drive/folders/../id",
    ],
)
def test_drive_folder_link_rejects_wrong_origins_and_file_links(value: str) -> None:
    with pytest.raises(ValueError, match="folder link"):
        drive_folder_id(value)
