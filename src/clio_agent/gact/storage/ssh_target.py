"""Storage adapter reusing registered host identities and infrastructure SSH transport."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, BinaryIO, Callable, cast

from clio_schemas.connected_resources import SourceCapabilities

from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec
from clio_agent.gact.storage.models import FileEntry


class _TargetReader(io.RawIOBase):
    def __init__(self, source: SshTargetSource, entry: FileEntry) -> None:
        self.source = source
        self.entry = entry
        self.offset = 0
        self.pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if not self.pending:
            payload = self.source.request("read", path=self.entry.path, offset=self.offset)
            self.pending = base64.b64decode(payload["data"], validate=True)
            self.offset += len(self.pending)
        count = min(len(buffer), len(self.pending))
        buffer[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count


class SshTargetSource:
    """Read selected files through the same connected target used by service management."""

    capabilities = SourceCapabilities(
        search=True,
        revision_check=True,
        supported_modes=["read_only", "working_copy"],
        unavailable_reasons={
            "write_enabled": "This Desktop SSH connection supports downloads. Use a browser SFTP connection for a writable link.",
        },
    )

    def __init__(
        self, root: str, execute: Callable[[CommandSpec], CommandResult], *, windows: bool = False
    ) -> None:
        self.root = root
        self.execute = execute
        self.program = "python" if windows else "python3"
        self.script = Path(__file__).with_name("ssh_target_probe.py").read_text(encoding="utf-8")

    def request(self, action: str, **parameters: Any) -> dict[str, Any]:
        """Send bounded file operations through the selected target without shell interpolation."""
        result = self.execute(
            CommandSpec(
                program=self.program,
                args=["-c", self.script],
                stdin=json.dumps({"action": action, "root": self.root, **parameters}),
                timeout_seconds=120,
            )
        )
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise ValueError("The selected SSH host returned an invalid file response") from exc
        if result.exit_code or not isinstance(payload, dict) or payload.get("error"):
            raise ValueError(
                "The selected SSH host could not read this folder; check its connection and permissions"
            )
        return payload

    def entries(self) -> list[FileEntry]:
        """Read a complete bounded inventory with file hashes for transfer verification."""
        rows: list[FileEntry] = []
        pending = [""]
        while pending:
            folder = pending.pop()
            offset = 0
            while True:
                page = self.request("list", path=folder, offset=offset)
                for item in page["entries"]:
                    path = "/".join(filter(None, [folder, item["name"]]))
                    if len(rows) >= 100_000:
                        raise ValueError("Select a smaller source folder (limit 100,000 entries)")
                    if item["directory"]:
                        rows.append(FileEntry(path=path, kind="directory"))
                        pending.append(path)
                    else:
                        metadata = self.request("stat", path=path)
                        rows.append(
                            FileEntry(
                                path=path,
                                kind="file",
                                size=metadata["size"],
                                revision=metadata["revision"],
                                sha256=metadata["revision"],
                            )
                        )
                if page["next"] is None:
                    break
                offset = page["next"]
        return sorted(rows, key=lambda row: row.path)

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Stream bounded chunks; materialization verifies the resulting content hash."""
        return cast(BinaryIO, io.BufferedReader(_TargetReader(self, entry)))

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Reject writeback until this transport supports reviewed source updates."""
        raise PermissionError("This SSH source is read only")
