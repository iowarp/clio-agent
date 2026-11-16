"""Subprocess client: one clio-core daemon lifetime running ``put:NAME:TEXT``, ``del:NAME``
and ``get:NAME`` in order; the reads go to ``$CLIO_STORE_OPS_OUT`` as JSON. The run ends
by releasing the daemon (last client out stops it)."""

from __future__ import annotations

import socket as _socket

# Winsock must be initialised before the native binding loads (#914).
_probe = _socket.socket()
_probe.close()

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402


def main() -> int:
    """Run the ops; returns a process exit code."""
    from clio_agent.arc import storage

    store = storage.make_arc_store()
    reads: dict[str, str | None] = {}
    for op in sys.argv[1:]:
        verb, name, *rest = op.split(":", 2)
        if verb == "put":
            store.put("segments", name, rest[0].encode())
        elif verb == "del":
            store.delete("segments", name)
        else:
            got = store.get("segments", name)
            reads[name] = None if got is None else bytes(got).decode("latin-1")
    with open(os.environ["CLIO_STORE_OPS_OUT"], "w", encoding="utf-8") as handle:
        json.dump(reads, handle)
    storage.release_runtime_client()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
