"""Frees a torn metadata record never committed, withheld from clio-core's allocation log.

clio-core logs block allocations in ``<file tier>_node<N>.alloc_log`` (40-byte entries:
``<u64 op><u64 class><u64 offset><u64 size><u64 0>``, op 1 allocates and op 2 frees),
apart from its metadata log. A delete or rewrite logs its frees there, then its metadata
record. When that record is torn (:mod:`clio_agent.arc.clio_core_wal_repair`), clio-core
restores the blob as it was, still pointing at blocks the allocation log has freed; the
next new blob is given one of those blocks, and the next write of the restored blob
overwrites it (reproduced with iowarp-core 2.4.1: a new record read back as the restored
blob's bytes).

So when the metadata repair cuts a torn put or delete, the frees at the end of the
allocation log are withheld: moved, unchanged, to ``<alloc_log>.withheld-<UTC time>``.
The blocks stay allocated to the restored blob, which keeps its own bytes. A withheld free
that belonged to a delete whose record is whole only leaks its blocks (space, never data).
Frees followed by allocations are not at the end and cannot be told apart from the next
operation's: they are left (``clio_core_alloc_log_unchanged``; a torn put, which logs
allocations, only leaks them). A log that is not whole 40-byte entries with ops 1 and 2
is left alone and reported (``clio_core_alloc_log_unverified``).
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path

WITHHELD = "clio_core_alloc_log_frees_withheld"
#: Nothing to withhold: the log ends in allocations (a torn put only leaks them).
UNCHANGED = "clio_core_alloc_log_unchanged"
UNVERIFIED = "clio_core_alloc_log_unverified"

_ENTRY = struct.Struct("<QQQQQ")
_ALLOC, _FREE = 1, 2


def alloc_logs(config_path: str) -> list[Path]:
    """The allocation logs of the config's file tiers."""
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError:
        return []
    found: list[Path] = []
    for match in re.finditer(r"(?m)^\s*-?\s*path:\s*[\"']?([^\"'\r\n]+)", text):
        tier = match.group(1).strip()
        if "::" in tier:
            continue  # a ram tier
        base = Path(tier)
        if base.parent.is_dir():
            found.extend(sorted(base.parent.glob(f"{base.name}_node*.alloc_log")))
    return found


def withhold_trailing_frees(config_path: str, stamp: str) -> list[dict[str, object]]:
    """Move the trailing frees of each allocation log aside; one outcome per log."""
    outcomes: list[dict[str, object]] = []
    for path in alloc_logs(config_path):
        data = path.read_bytes()
        whole = len(data) - len(data) % _ENTRY.size
        entries = [_ENTRY.unpack_from(data, at) for at in range(0, whole, _ENTRY.size)]
        if whole != len(data) or any(
            op not in (_ALLOC, _FREE) or tail for op, *_rest, tail in entries
        ):
            outcomes.append({"reason": UNVERIFIED, "path": str(path), "why": "unknown format"})
            continue
        trailing = 0
        while trailing < len(entries) and entries[-1 - trailing][0] == _FREE:
            trailing += 1
        if not trailing:
            outcomes.append({"reason": UNCHANGED, "path": str(path)})
            continue
        cut = len(data) - trailing * _ENTRY.size
        aside = path.with_name(f"{path.name}.withheld-{stamp}")
        aside.write_bytes(data[cut:])
        staged = path.with_name(path.name + ".repair")
        with staged.open("wb") as handle:
            handle.write(data[:cut])
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        outcomes.append(
            {"reason": WITHHELD, "path": str(path), "frees": trailing, "kept_as": str(aside)}
        )
    return outcomes


__all__ = ["UNCHANGED", "UNVERIFIED", "WITHHELD", "alloc_logs", "withhold_trailing_frees"]
