"""No clio-core write once the metadata log's disk is nearly full (owner module, FN-14).

clio-core appends a record to its metadata write-ahead log for every put and delete.
When that file system fills, iowarp-core 2.4.1 writes part of the record, answers the
put as a success and logs nothing: the log is torn. Reproduced with the log on a 3 MB
file system and four writers: both busy logs ended inside a record, every put returned
0, and the daemon's log had no error. The torn record and every record written after it
are lost at the next start (:mod:`clio_agent.arc.clio_core_wal_repair` cuts the tear out,
but the allocation log may still disagree with the metadata about that blob's blocks).

So every write and delete CLIO sends to clio-core first checks the free space of the file
system that holds the metadata log, and refuses while it is below
``arc.cte.metadata_disk_reserve`` (``CLIO_ARC_CTE_METADATA_DISK_RESERVE``, default
256MB): :class:`ClioCoreDiskLowRefused`, reason ``clio_core_metadata_disk_low``. A put
refused this way is a lost write like any other (:mod:`clio_agent.arc.clio_core_retry`),
so a message the person sends answers ``503 core_writes_refused`` with the cause, and
writing resumes by itself once space is freed. One ``statvfs`` per write.
"""

from __future__ import annotations

import functools
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from clio_agent.arc.clio_core_async_ops import ClioCorePutRefused

logger = logging.getLogger(__name__)

DISK_LOW = "clio_core_metadata_disk_low"
_DEFAULT_RESERVE = "256MB"
_LOG_EVERY_S = 30.0

_last_logged = 0.0
_log_lock = threading.Lock()


class ClioCoreDiskLowRefused(ClioCorePutRefused):
    """CLIO refused a clio-core write: the metadata log's file system is nearly full."""

    degradation_reason = DISK_LOW

    def __init__(self, name: str, directory: str, free: int, reserve: int) -> None:
        RuntimeError.__init__(
            self,
            f"clio-core write of {name!r} refused (reason={DISK_LOW}): {free} bytes free on "
            f"the file system of {directory}, below the {reserve}-byte reserve. clio-core "
            "would tear its metadata log there; free disk space and the writes resume.",
        )
        self.native_code = None
        self.directory, self.free, self.reserve = directory, free, reserve


def reserve_bytes() -> int:
    """``arc.cte.metadata_disk_reserve`` in bytes (0 turns the guard off)."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle
    from clio_agent.arc.clio_core_config import parse_capacity_bytes  # noqa: PLC0415

    raw = conf.resolve(
        "arc.cte.metadata_disk_reserve",
        env="CLIO_ARC_CTE_METADATA_DISK_RESERVE",
        default=_DEFAULT_RESERVE,
        cast=conf.as_str,
    ).strip()
    return 0 if raw in {"0", ""} else parse_capacity_bytes(raw)


@functools.lru_cache(maxsize=16)
def metadata_log_dir(config_path: str) -> str:
    """The directory of the config's ``metadata_log_path`` (``""``: none configured)."""
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"(?m)^\s*metadata_log_path:\s*[\"']?([^\"'\r\n]+)", text)
    return str(Path(match.group(1).strip()).parent) if match else ""


def ensure_room(store: Any, name: str) -> None:
    """Refuse a write to ``store`` while its metadata log's disk is below the reserve.

    Raises:
        ClioCoreDiskLowRefused: Less than :func:`reserve_bytes` is free there.
    """
    global _last_logged
    directory = metadata_log_dir(str(getattr(store, "_config_path", "") or ""))
    reserve = reserve_bytes() if directory else 0
    if not reserve:
        return
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return  # the directory is clio-core's to report; nothing to measure here
    if free >= reserve:
        return
    refusal = ClioCoreDiskLowRefused(name, directory, free, reserve)
    with _log_lock:
        now = time.monotonic()
        if now - _last_logged >= _LOG_EVERY_S:
            _last_logged = now
            logger.error("%s", refusal)
    raise refusal


__all__ = ["DISK_LOW", "ClioCoreDiskLowRefused", "ensure_room", "metadata_log_dir", "reserve_bytes"]
