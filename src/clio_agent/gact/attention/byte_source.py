"""The bytes of a SafeTensors file on this machine, read as batched ranges.

CLIO reads attention files only where it can open them: in place (CLIO runs
on the connector's node) or from a local copy (``provenance.attention.files_dir``).
Fetching files from a remote node is not CLIO's job; how remote attention data
is served (e.g. by Flowcept) is an open design question with the connector
team.

Every failure is ``attention_file_unavailable``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from clio_agent.gact.attention.reasons import AttentionUnavailable


class LocalFile:
    """A file on this machine."""

    def __init__(self, path: Path) -> None:
        """Wrap ``path``."""
        self.path = path
        self.label = str(path)

    def read_many(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        """Seek and read each ``(offset, size)`` range, in order."""
        out: list[bytes] = []
        try:
            with self.path.open("rb") as fh:
                for offset, size in ranges:
                    fh.seek(offset)
                    out.append(fh.read(size))
        except OSError as exc:
            raise AttentionUnavailable(
                "attention_file_unavailable",
                f"cannot read {self.path}: {type(exc).__name__}: {exc}",
                {"path": self.label},
            ) from exc
        return out

    def stat(self) -> tuple[int, str]:
        """``(size, sha256 hex)`` of the whole file, streamed."""
        sha = hashlib.sha256()
        try:
            with self.path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                    sha.update(chunk)
            return self.path.stat().st_size, sha.hexdigest()
        except OSError as exc:
            raise AttentionUnavailable(
                "attention_file_unavailable",
                f"cannot read {self.path}: {exc}",
                {"path": self.label},
            ) from exc
