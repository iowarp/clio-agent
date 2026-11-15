"""Subprocess client for the restart-durability test: one daemon lifetime per run.

``write <prefix> <count>`` stores ``count`` records (framed records of several sizes and
records grown by raw appends, whose metadata spans many blocks), ``check <prefix>
<count>`` reads them back. Either way the run ends by releasing the daemon (last client
out stops it), so each run is one start and one stop of clio-core. The result is written
as JSON to ``$CLIO_DURABILITY_OUT``.
"""

from __future__ import annotations

import socket as _socket

# Winsock must be initialised before the native binding loads (#914).
_probe = _socket.socket()
_probe.close()

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

_SIZES = (40, 1500, 4100, 9000, 70000)


def _body(name: str, size: int) -> bytes:
    return (hashlib.sha256(name.encode()).digest() * (size // 32 + 1))[:size]


def main() -> int:
    """Write or check one prefix's records; returns a process exit code."""
    mode, prefix, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    from clio_agent.arc import storage

    store = storage.make_arc_store()
    missing: list[str] = []
    for index in range(count):
        name = f"{prefix}.{index:04d}"
        body = _body(name, _SIZES[index % len(_SIZES)])
        appended = index % 3 == 0
        if mode == "write":
            if appended:  # a chunk-record style blob: many raw appends
                step = max(1, len(body) // 8)
                for offset in range(0, len(body), step):
                    store.put_at("segments", name, body[offset : offset + step], offset)
            else:
                store.put("segments", name, body)
            continue
        got = store.get_raw("segments", name) if appended else store.get("segments", name)
        if got is None or bytes(got)[: len(body)] != body:
            missing.append(name)
    out = {"mode": mode, "prefix": prefix, "count": count, "missing": missing}
    with open(os.environ["CLIO_DURABILITY_OUT"], "w", encoding="utf-8") as handle:
        json.dump(out, handle)
    storage.release_runtime_client()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
