"""A torn record in clio-core's metadata log, cut out before the daemon starts (owner module).

clio-core keeps its blob and tag metadata in write-ahead logs beside the data tier
(``<metadata_log_path>.blob.<N>`` and ``.tag.<N>``: the magic ``2LAW``, then records of
a 13-byte header, ``<u8 type><u64 seq><u32 length>``, and ``length`` payload bytes).
A daemon killed while it appends (a crash, or a stop that ran out of time and was
killed) leaves the last record torn. On its next start, iowarp-core 2.4.1 reports the
torn record (``TransactionLog::Load: ... claiming N bytes but only M remain; ignoring it
and everything after``), but then appends new records after the torn bytes instead of
cutting them off. From the start after that, the torn record's length swallows the new
records, the reader lands inside one and stops on a garbage length (FN-14: ``claiming
2147483839 bytes``): every conversation written since the tear is gone, and so is every
one written after any later start.

:func:`repair_metadata_logs` runs under the spawn lock right before this home spawns its
daemon (no daemon uses the files). For each log it walks the records by their lengths;
a log that ends exactly at its last record is left alone, whatever it holds. Otherwise:

* the torn bytes are followed by a run of records whose sequence numbers continue from
  the last good record and that walks exactly to the end of the file: the torn bytes are
  cut out (``clio_core_metadata_log_repaired``);
* nothing follows the torn record: the file is cut at it, which is what clio-core reads
  anyway (``clio_core_metadata_log_repaired``);
* anything else: the files are left unchanged and the attach is refused
  (``clio_core_metadata_log_unreadable``), because a daemon started on that log would
  write every new record where the next start cannot read it.

Cut bytes are kept beside the log (``<log>.torn-<UTC time>``), never deleted. A log
without the ``2LAW`` magic is a format this module does not know: it is reported and
left to clio-core. Each outcome is logged with its reason and kept for the
``cte_metadata_log`` health row and the scratch sweep (:func:`restore_suspect`).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import struct
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from clio_agent.arc.clio_core_alloc_log import UNVERIFIED, withhold_trailing_frees
from clio_agent.errors import ClioError

logger = logging.getLogger(__name__)

REPAIRED = "clio_core_metadata_log_repaired"
UNREADABLE = "clio_core_metadata_log_unreadable"
UNKNOWN_FORMAT = "clio_core_metadata_log_format_unknown"
#: The daemon's own log said a metadata restore stopped early (after this process spawned it).
RESTORE_FAILED = "clio_core_metadata_restore_failed"

MAGIC = b"2LAW"
_HEADER = struct.Struct("<BQI")
#: Sequence numbers are one counter across the logs; consecutive records in one log step
#: by a few. A step this large or a step back marks bytes that are not a record.
_MAX_SEQ_STEP = 1 << 24
_LOG_FILE = re.compile(r"\.(blob|tag)\.\d+$")
#: Metadata record types that can free blocks: 1 (a blob's new block list), 3 (a delete).
_FREEING_TYPES = frozenset({1, 3})
_RESTORE_FAILURE = re.compile(r"TransactionLog::Load:|metadata restore under .* failed")

_lock = threading.Lock()
_outcomes: list[dict[str, object]] = []
_daemon_log: tuple[Path, int] | None = None


class ClioCoreMetadataLogUnreadableError(ClioError):
    """A metadata log has bytes that are not records, and no run of records after them."""

    degradation_reason = UNREADABLE

    def __init__(self, message: str, details: dict[str, object]) -> None:
        super().__init__(message, error_type=UNREADABLE, details=details)


@dataclass(frozen=True)
class LogScan:
    """One metadata log's verdict: ``clean``, ``cut`` (bytes ``[start, end)``),
    ``unreadable`` or ``unknown_format``."""

    path: str
    verdict: str
    size: int
    start: int = 0
    end: int = 0
    records: int = 0
    #: The type byte of the torn record that was cut (-1: none or unknown).
    torn_type: int = -1


def _chain(data: bytes, offset: int, after_seq: int | None) -> list[tuple[int, int, int]] | None:
    """The records from ``offset`` when they walk exactly to the end with rising sequence
    numbers (each a small step past ``after_seq``), else ``None``."""
    records: list[tuple[int, int, int]] = []
    previous = after_seq
    while offset < len(data):
        if offset + _HEADER.size > len(data):
            return None
        _kind, seq, length = _HEADER.unpack_from(data, offset)
        end = offset + _HEADER.size + length
        if end > len(data) or (previous is not None and not 0 < seq - previous <= _MAX_SEQ_STEP):
            return None
        records.append((offset, seq, end))
        previous, offset = seq, end
    return records


def scan_log(path: Path) -> LogScan:
    """Walk one metadata log and decide what, if anything, is torn in it."""
    data = path.read_bytes()
    if len(data) < len(MAGIC) or data[: len(MAGIC)] != MAGIC:
        return LogScan(str(path), "unknown_format" if data else "clean", len(data))
    offset, walked = len(MAGIC), []
    while offset < len(data):
        if offset + _HEADER.size > len(data):
            break
        _kind, seq, length = _HEADER.unpack_from(data, offset)
        end = offset + _HEADER.size + length
        if end > len(data):
            break
        walked.append((offset, seq, end))
        offset = end
    if offset == len(data):
        return LogScan(str(path), "clean", len(data), records=len(walked))
    # The trusted records: rising sequence numbers in small steps. The last of them may be
    # the torn one whose length swallowed records appended after it.
    trusted = walked[:1]
    for record in walked[1:]:
        if not 0 < record[1] - trusted[-1][1] <= _MAX_SEQ_STEP:
            break
        trusted.append(record)
    if not trusted:  # the first record is torn
        last_off, last_seq, last_end = len(MAGIC), None, len(MAGIC)
    else:
        last_off, last_seq, last_end = trusted[-1]
    resume = _resume_offset(data, last_off + _HEADER.size, last_seq)
    if resume is not None:
        start = last_off if resume < last_end else last_end
        return LogScan(str(path), "cut", len(data), start, resume, len(trusted), data[start])
    if last_end == offset:  # only a torn tail follows the good records
        return LogScan(str(path), "cut", len(data), offset, len(data), len(trusted), data[offset])
    return LogScan(str(path), "unreadable", len(data), records=len(trusted))


def _resume_offset(data: bytes, first: int, after_seq: int | None) -> int | None:
    """Where records resume after torn bytes: the first offset from which records walk
    exactly to the end, numbered on from ``after_seq``; failing that (a daemon started
    after an earlier tear may number from elsewhere), at least two records of their own."""
    for constraint in (after_seq,) if after_seq is None else (after_seq, None):
        for resume in range(first, len(data) - _HEADER.size + 1):
            run = _chain(data, resume, constraint)
            if run is not None and (constraint is not None or len(run) >= 2):
                return resume
    return None


def _cut(path: Path, scan: LogScan, stamp: str) -> None:
    data = path.read_bytes()
    aside = path.with_name(f"{path.name}.torn-{stamp}")
    aside.write_bytes(data[scan.start : scan.end])
    staged = path.with_name(path.name + ".repair")
    with staged.open("wb") as handle:
        handle.write(data[: scan.start] + data[scan.end :])
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staged, path)


def metadata_log_files(config_path: str) -> list[Path]:
    """The metadata logs the config's ``metadata_log_path`` names (none when unset)."""
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError:
        return []
    match = re.search(r"(?m)^\s*metadata_log_path:\s*[\"']?([^\"'\r\n]+)", text)
    if match is None:
        return []
    base = Path(match.group(1).strip())
    if not base.parent.is_dir():
        return []
    return sorted(
        entry
        for entry in base.parent.iterdir()
        if entry.name.startswith(base.name)
        and _LOG_FILE.fullmatch(entry.name[len(base.name) :])
        and entry.is_file()
    )


def repair_metadata_logs(config_path: str, *, daemon_log: Path | None = None) -> list[LogScan]:
    """Cut torn records out of the config's metadata logs before its daemon starts.

    Raises:
        ClioCoreMetadataLogUnreadableError: A log holds bytes that are neither records
            nor a torn record with records after it; nothing was changed.
    """
    global _daemon_log
    if daemon_log is not None:
        with _lock:
            _daemon_log = (daemon_log, daemon_log.stat().st_size if daemon_log.exists() else 0)
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    scans = [scan_log(path) for path in metadata_log_files(config_path)]
    unreadable = [scan for scan in scans if scan.verdict == "unreadable"]
    if unreadable:
        details = {"reason": UNREADABLE, "logs": [asdict(scan) for scan in unreadable]}
        _record(UNREADABLE, details)
        logger.error("clio-core metadata log unreadable reason=%s logs=%s", UNREADABLE, details)
        raise ClioCoreMetadataLogUnreadableError(
            f"clio-core's metadata log {unreadable[0].path} holds bytes that are not "
            "records and no records after them, so a daemon started on it would write "
            "every new record where the next start cannot read it. Nothing was changed. "
            "Report it with the file; to start the metadata fresh, quit CLIO and move "
            "the metadata.log.* files aside (the stored records are then not listed).",
            details,
        )
    for scan in scans:
        if scan.verdict == "unknown_format":
            _record(UNKNOWN_FORMAT, asdict(scan))
            logger.warning("clio-core metadata log reason=%s %s", UNKNOWN_FORMAT, asdict(scan))
        elif scan.verdict == "cut":
            _cut(Path(scan.path), scan, stamp)
            _record(REPAIRED, {**asdict(scan), "kept_as": f"{scan.path}.torn-{stamp}"})
            logger.warning(
                "clio-core metadata log had a torn record reason=%s log=%s cut=[%d,%d) of "
                "%d bytes, kept as %s.torn-%s; the records after it are readable again",
                REPAIRED,
                scan.path,
                scan.start,
                scan.end,
                scan.size,
                scan.path,
                stamp,
            )
    torn = {scan.torn_type for scan in scans if scan.verdict == "cut"}
    if torn & _FREEING_TYPES:  # a torn put or delete may have logged frees it never committed
        for outcome in withhold_trailing_frees(config_path, stamp):
            reason = str(outcome.pop("reason"))
            _record(reason, outcome)
            log = logger.error if reason == UNVERIFIED else logger.warning
            log("clio-core allocation log after a torn record reason=%s %s", reason, outcome)
    return scans


def _record(reason: str, details: dict[str, object]) -> None:
    with _lock:
        _outcomes.append({"reason": reason, **details})


def outcomes() -> list[dict[str, object]]:
    """What this process found in the metadata logs (repairs, unreadable, unknown format)."""
    with _lock:
        return list(_outcomes)


def restore_failures() -> list[str]:
    """Restore-failure lines the daemon this process spawned wrote to its log."""
    with _lock:
        source = _daemon_log
    if source is None:
        return []
    path, offset = source
    try:
        with path.open("rb") as handle:
            handle.seek(offset if offset <= path.stat().st_size else 0)  # rotated: all of it
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line.strip()[:400] for line in text.splitlines() if _RESTORE_FAILURE.search(line)]


def restore_suspect() -> str:
    """The reason the store's restore cannot be trusted this boot, or ``""``."""
    if restore_failures():
        return RESTORE_FAILED
    if any(item["reason"] == UNREADABLE for item in outcomes()):
        return UNREADABLE
    return ""


def reset_for_tests() -> None:
    """Forget this process's outcomes (tests)."""
    global _daemon_log
    with _lock:
        _outcomes.clear()
        _daemon_log = None


__all__ = [
    "MAGIC",
    "REPAIRED",
    "RESTORE_FAILED",
    "UNKNOWN_FORMAT",
    "UNREADABLE",
    "ClioCoreMetadataLogUnreadableError",
    "LogScan",
    "metadata_log_files",
    "outcomes",
    "repair_metadata_logs",
    "restore_failures",
    "restore_suspect",
    "scan_log",
]
