"""How much of an isolated stack's clio-core store and transcripts a check used (VF).

    python scripts/cloud/checks/apps/store_usage.py [SESSION_ID ...]

Reads the data tier's allocation log (40-byte records: op, kind, offset, size, pad;
op 1 allocates, any other op releases) and prints one JSON line: the bytes allocated now,
the high-water mark, the tier's capacity, the metadata log's size, and each named
session's transcript and semantic trace file sizes. Only for ``CLIO_CLOUD_ISOLATE=1`` stacks.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4] / ".cloud-run" / "xdg"


def main(sessions: list[str]) -> None:
    (host,) = (ROOT / "data" / "clio-agent" / "cte" / "hosts").iterdir()
    log = (host / "storage.bin_node0.alloc_log").read_bytes()
    live: dict[int, int] = {}
    high = 0
    for start in range(0, len(log) - len(log) % 40, 40):
        op, _kind, offset, size, _pad = struct.unpack_from("<5Q", log, start)
        if op == 1:
            live[offset] = size
            high = max(high, offset + size)
        else:
            live.pop(offset, None)
    server = ROOT / "state" / "clio-agent"
    files = {
        "transcript": server / "server" / "messages" / "{sid}.json",
        "trace": server / "traces" / "semantic_traces" / "{sid}.semantic.jsonl",
    }
    print(
        json.dumps(
            {
                "allocated_bytes": sum(live.values()),
                "high_water_bytes": high,
                "capacity_bytes": (host / "storage.bin_node0").stat().st_size,
                "metadata_log_bytes": sum(p.stat().st_size for p in host.glob("metadata.log*")),
                "sessions": {
                    sid: {
                        kind: Path(str(path).format(sid=sid)).stat().st_size
                        if Path(str(path).format(sid=sid)).exists()
                        else None
                        for kind, path in files.items()
                    }
                    for sid in sessions
                },
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
