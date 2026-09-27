"""Where a SafeTensors file's bytes come from: a local file, or byte ranges over a shell.

The connector writes its files on the GPU node. CLIO reads them in place when it
runs there, from a local mirror (``provenance.attention.files_dir``), or -- when
it runs elsewhere -- through ``provenance.attention.remote_shell``: a command
prefix that opens a shell on the node (e.g. ``ssh spotter-node``). Remote reads
send one small Python 3.6+ script on stdin and get back only the requested byte
ranges, so a selection costs a couple of round trips, never a whole-file copy.

Every failure is ``attention_file_unavailable`` with the command's own message.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from typing import Protocol

from clio_agent import conf
from clio_agent.gact.attention.reasons import AttentionUnavailable

#: Runs on the node (Python 3.6 compatible: Delta's system python3 is 3.6).
#: argv: mode ("read" | "stat") path [offset size ...]; stdout: the raw bytes,
#: or "<size> <sha256>" for stat.
_REMOTE_SCRIPT = r"""
import hashlib, os, sys
mode, path = sys.argv[1], sys.argv[2]
out = sys.stdout.buffer
if mode == "stat":
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    out.write(("%d %s" % (os.path.getsize(path), h.hexdigest())).encode())
else:
    nums = [int(v) for v in sys.argv[3:]]
    with open(path, "rb") as fh:
        for off, size in zip(nums[0::2], nums[1::2]):
            fh.seek(off)
            data = fh.read(size)
            if len(data) != size:
                sys.stderr.write("short read at %d: %d of %d bytes\n" % (off, len(data), size))
                sys.exit(3)
            out.write(data)
out.flush()
"""


class ByteSource(Protocol):
    """Random access to one file's bytes."""

    label: str

    def read_many(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        """The bytes of each ``(offset, size)`` range, in order."""
        ...

    def stat(self) -> tuple[int, str]:
        """``(size, sha256 hex)`` of the whole file."""
        ...


class LocalFile:
    """A file on this machine."""

    def __init__(self, path: Path) -> None:
        """Wrap ``path``."""
        self.path = path
        self.label = str(path)

    def read_many(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        """Seek and read each range."""
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
        """Size and sha256, streamed."""
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


def configured_remote_shell() -> list[str]:
    """The node shell command prefix as argv (``[]`` when not configured)."""
    raw = conf.resolve(
        "provenance.attention.remote_shell",
        env="CLIO_PROVENANCE_ATTENTION_REMOTE_SHELL",
        default="",
        cast=conf.as_str,
    ).strip()
    return shlex.split(raw) if raw else []


class RemoteFile:
    """A file on the node, read through ``<prefix> python3 - <args>``."""

    def __init__(self, prefix: list[str], path: str) -> None:
        """``prefix`` opens a shell on the node; ``path`` is the node path."""
        self.prefix = prefix
        self.path = path
        self.label = f"{' '.join(prefix)}:{path}"

    def _run(self, args: list[str]) -> bytes:
        # One remote command string: ssh joins its arguments with spaces and hands
        # them to the node's shell, so every argument is quoted for that shell.
        remote = " ".join(shlex.quote(a) for a in ["python3", "-", *args])
        try:
            done = subprocess.run(  # noqa: S603 - operator-configured command
                [*self.prefix, remote],
                input=_REMOTE_SCRIPT.encode(),
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise AttentionUnavailable(
                "attention_file_unavailable",
                f"cannot run the remote shell {self.prefix[0]!r}: {exc}",
                {"path": self.label},
            ) from exc
        if done.returncode != 0:
            tail = done.stderr.decode(errors="replace").strip().splitlines()[-3:]
            raise AttentionUnavailable(
                "attention_file_unavailable",
                f"remote read failed (exit {done.returncode}): {' | '.join(tail)}",
                {"path": self.label},
            )
        return done.stdout

    def read_many(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        """All ranges in one remote call, split back by size."""
        args = ["read", self.path]
        for offset, size in ranges:
            args += [str(offset), str(size)]
        raw = self._run(args)
        want = sum(size for _, size in ranges)
        if len(raw) != want:
            raise AttentionUnavailable(
                "attention_file_unavailable",
                f"remote read returned {len(raw)} bytes, wanted {want}",
                {"path": self.label},
            )
        out: list[bytes] = []
        cursor = 0
        for _, size in ranges:
            out.append(raw[cursor : cursor + size])
            cursor += size
        return out

    def stat(self) -> tuple[int, str]:
        """Size and sha256 computed on the node (the file never crosses the wire)."""
        raw = self._run(["stat", self.path]).decode(errors="replace").split()
        try:
            return int(raw[0]), raw[1]
        except (IndexError, ValueError) as exc:
            raise AttentionUnavailable(
                "attention_file_unavailable",
                f"remote stat answered {' '.join(raw)!r}",
                {"path": self.label},
            ) from exc
