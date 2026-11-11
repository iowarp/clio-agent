"""Desktop bounds for the clio-core daemon's shared-memory segments (#906).

Without ``runtime.main_segment_size`` and ``runtime.metadata_segment_size`` clio-core
sizes both segments from system RAM and caps each at 1 GiB off Linux
(``context-runtime/src/config_manager.cc``, ``ipc_manager.cc``). Both are mapped into
the daemon and every client, and on Windows the main segment is committed up front.

Measured on Windows (QueryWorkingSetEx per mapped view) in normal use, the
daemon keeps 0 MB of the main segment resident and about 300 MB of the metadata
segment. That 300 MB is the shared-memory metadata cache, whose tables are sized once
at 64Ki tags and 256Ki blobs (``core_runtime.cc``). When Defender rescans a client
after a cloud signature update, it faults in every mapped page, and the agent server
went from about 350 MB to about 4.1 GB. The caps below keep that worst case bounded:
256 MB of task data (tasks are small; blob payloads use the client data segment) and
512 MB of metadata (the cache plus room for its per-entry allocations).

``queue_depth`` is left at clio-core's default. Its per-lane budget is
``2 * max(queue_depth, 128Ki)``, so a smaller depth does not shrink the queue segment.

A seed written before these keys is upgraded in place, once, like
:mod:`clio_agent.arc.clio_core_threads`: only a runtime block in CLIO's own seeded
shape that has no segment key yet. Any explicit segment value is kept. The daemon
reads the sizes when it next starts and recreates its segments on every start.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

__all__ = [
    "MAIN_SEGMENT_SIZE",
    "METADATA_SEGMENT_SIZE",
    "SEGMENT_LINES",
    "ensure_seeded_segments",
]

logger = logging.getLogger(__name__)

MAIN_SEGMENT_SIZE = "256MB"
METADATA_SEGMENT_SIZE = "512MB"

#: The lines CLIO's seed carries between ``num_threads`` and ``conf_dir``.
SEGMENT_LINES = (
    f'  main_segment_size: "{MAIN_SEGMENT_SIZE}"\n'
    f'  metadata_segment_size: "{METADATA_SEGMENT_SIZE}"\n'
)

# CLIO's seeded runtime block before the segment keys.
_SEEDED_RUNTIME = re.compile(r"^(runtime:\n  num_threads: \d+\n)(  conf_dir: )", re.MULTILINE)
_SEGMENT_KEY = re.compile(r"^\s*(main_segment_size|metadata_segment_size):", re.MULTILINE)


def ensure_seeded_segments(path: Path) -> None:
    """Add the desktop segment caps to a CLIO seed that has none."""
    text = path.read_text(encoding="utf-8")
    if _SEGMENT_KEY.search(text) or len(_SEEDED_RUNTIME.findall(text)) != 1:
        return
    upgraded = _SEEDED_RUNTIME.sub(lambda m: m.group(1) + SEGMENT_LINES + m.group(2), text)
    path.write_text(upgraded, encoding="utf-8")
    from clio_agent.arc.clio_core_growth import rekey_managed_capacity  # noqa: PLC0415 - cycle

    # CLIO's own edit: it keeps owning the config's growth.
    rekey_managed_capacity(path, text, upgraded)
    logger.warning(
        "clio-core config %s upgraded in place: main_segment_size %s, "
        "metadata_segment_size %s; the daemon uses them from its next start",
        path,
        MAIN_SEGMENT_SIZE,
        METADATA_SEGMENT_SIZE,
    )
