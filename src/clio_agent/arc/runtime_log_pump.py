"""Bounded writer for the clio-core daemon's stdout/stderr (stdlib only, run as a script).

The shared daemon (``clio_run start``) used to write straight into an append-mode
``clio-runtime.log`` with no cap. On 2026-11-09 a clio-core error path that loops
forever and logs every iteration (``GlobalDeserialize::read_binary: Attempted to
read beyond end of data``) grew that file to 128 GB in 53 minutes and filled the
disk. This pump sits between the daemon and the file instead:

* the daemon's stdout and stderr are the write end of a pipe; this process reads
  the other end until EOF (the daemon's exit) and then exits itself;
* consecutive identical lines are collapsed into one line plus a periodic
  ``previous line repeated N times`` summary;
* the file never exceeds ``--max-bytes``: on overflow it is rotated once to
  ``<log>.1`` (so at most two capped files exist) and a fresh file is started;
* a sustained line rate above ``--flood-lines-per-s`` writes a flood record JSON
  (``--flood-record``) that clio-agent reads to surface and replace the daemon,
  and removes it again when the rate falls;
* write failures (``ENOSPC``) drop output but never stop the draining, so the
  daemon is never blocked or killed by a full pipe.

It must stay importable without clio-agent (it runs under ``python -I``), and it
must not die with the server: it is spawned detached like the daemon and ignores
SIGHUP/SIGINT. Spawned by :mod:`clio_agent.arc.runtime_log`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Optional

#: Upper bound on one line; a longer run of bytes is written as several lines.
MAX_LINE_BYTES = 64 * 1024
#: Repeat summaries during an unbroken run are written at most this often.
REPEAT_SUMMARY_INTERVAL_S = 10.0
#: Window over which the line rate is measured for flood detection.
FLOOD_WINDOW_S = 10.0
#: A flood record is rewritten at most this often while the flood lasts.
FLOOD_RECORD_REFRESH_S = 2.0
_MARK = b"[clio-agent log pump] "


class BoundedLog:
    """Append-only file capped at ``max_bytes`` with one rotation to ``<path>.1``."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max(4096, max_bytes)
        self._fh: Optional[BinaryIO] = None
        self._size = 0
        self.dropped_bytes = 0
        self._failing_since: Optional[float] = None

    def _open(self) -> BinaryIO:
        if self._fh is None:
            self._fh = open(self.path, "ab")  # noqa: SIM115 - held for the pump's life
            self._size = self._fh.tell()
        return self._fh

    def _rotate(self) -> None:
        self.close()
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except FileNotFoundError:
            pass

    def write(self, data: bytes) -> None:
        """Write ``data``, rotating first if it would cross the cap; drop it on I/O error."""
        if self._failing_since is not None and time.monotonic() - self._failing_since < 1.0:
            self.dropped_bytes += len(data)
            return
        try:
            fh = self._open()
            if self._size + len(data) > self.max_bytes and self._size > 0:
                self._rotate()
                fh = self._open()
            if self.dropped_bytes:
                note = _MARK + b"dropped %d bytes after write errors\n" % self.dropped_bytes
                fh.write(note)
                self._size += len(note)
                self.dropped_bytes = 0
            fh.write(data[: self.max_bytes])
            fh.flush()
            self._size += min(len(data), self.max_bytes)
            self._failing_since = None
        except OSError:
            # ENOSPC, a removed directory, a revoked permission: keep draining the pipe and
            # retry the file about once a second. Never stop reading -- a full pipe would
            # block (or SIGPIPE) the daemon.
            self.dropped_bytes += len(data)
            self._failing_since = time.monotonic()
            self.close()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


class FloodTracker:
    """Measure the line rate per window and keep the flood record in step with it."""

    def __init__(
        self, record_path: Optional[Path], pidfile: Optional[Path], lines_per_s: int
    ) -> None:
        self.record_path = record_path
        self.pidfile = pidfile
        self.threshold = max(1, lines_per_s) * FLOOD_WINDOW_S
        self._window_start: Optional[float] = None  # the first observed line opens it
        self._window_lines = 0
        self._last_rate = 0.0
        self.active = False
        self._first_seen = 0.0
        self._last_written = 0.0
        self.total_lines = 0

    def observe(self, line: bytes, now: float) -> None:
        if self._window_start is None:
            self._window_start = now
        self._window_lines += 1
        self.total_lines += 1
        elapsed = now - self._window_start
        if elapsed >= FLOOD_WINDOW_S:
            self._last_rate = self._window_lines / elapsed
            flooding = self._window_lines >= self.threshold
            self._window_start = now
            self._window_lines = 0
            if flooding and not self.active:
                self.active = True
                self._first_seen = time.time()
            elif not flooding and self.active:
                self.active = False
                self.clear()
        if self.active and now - self._last_written >= FLOOD_RECORD_REFRESH_S:
            self._last_written = now
            self._write(line)

    def _daemon_pid(self) -> Optional[int]:
        if self.pidfile is None:
            return None
        try:
            return int(self.pidfile.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def _write(self, line: bytes) -> None:
        if self.record_path is None:
            return
        record = {
            "reason": "clio_core_daemon_log_flood",
            "daemon_pid": self._daemon_pid(),
            "pump_pid": os.getpid(),
            "line": line[:2048].decode("utf-8", errors="replace").rstrip("\n"),
            "lines_per_s": round(self._last_rate, 1),
            "threshold_lines_per_s": round(self.threshold / FLOOD_WINDOW_S, 1),
            "total_lines": self.total_lines,
            "first_seen": self._first_seen,
            "last_seen": time.time(),
        }
        tmp = self.record_path.with_name(self.record_path.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
            os.replace(tmp, self.record_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def clear(self) -> None:
        if self.record_path is None:
            return
        try:
            self.record_path.unlink()
        except OSError:
            pass


def _repeat_note(count: int) -> bytes:
    return _MARK + b"previous line repeated %d more times\n" % count


def pump(
    source: BinaryIO,
    log: BoundedLog,
    flood: FloodTracker,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Copy ``source`` into ``log`` until EOF; returns the number of lines read."""
    previous: Optional[bytes] = None
    pending = 0
    last_summary = clock()
    lines = 0
    while True:
        line = source.readline(MAX_LINE_BYTES)
        if not line:
            break
        lines += 1
        now = clock()
        flood.observe(line, now)
        if line == previous:
            pending += 1
            if now - last_summary >= REPEAT_SUMMARY_INTERVAL_S:
                log.write(_repeat_note(pending))
                pending = 0
                last_summary = now
            continue
        if pending:
            log.write(_repeat_note(pending))
            pending = 0
        log.write(line if line.endswith(b"\n") else line + b"\n")
        previous = line
        last_summary = now
    if pending:
        log.write(_repeat_note(pending))
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--flood-record", type=Path, default=None)
    parser.add_argument("--pidfile", type=Path, default=None)
    parser.add_argument("--flood-lines-per-s", type=int, default=1000)
    args = parser.parse_args(argv)
    for name in ("SIGHUP", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, signal.SIG_IGN)
    log = BoundedLog(args.log, args.max_bytes)
    flood = FloodTracker(args.flood_record, args.pidfile, args.flood_lines_per_s)
    try:
        pump(sys.stdin.buffer, log, flood)
    finally:
        flood.clear()
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
