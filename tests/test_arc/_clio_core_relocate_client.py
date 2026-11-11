"""Subprocess client for the moved-store end-to-end test (``test_clio_core_relocate_e2e``).

Runs the real ``ClioCoreStore`` path against a private clio-core daemon, in its own
process (clio-core allows one runtime per process). ``argv[1]`` is the phase:

* ``write``: put the blob, read it back, stop the daemon cleanly;
* ``read``: read the blob, stop the daemon cleanly.

The store comes from ``CLIO_ARC_CTE_DIR`` through the seeded default (so the start-time
relocation runs as in the product), or from ``CLIO_ARC_STORE_CONFIG`` when set. One JSON
line is printed: the store type, the bytes read and the relocation this start made.
"""

from __future__ import annotations

import socket as _socket

# Winsock before the native binding loads (#914; see _clio_core_offload_client).
_probe = _socket.socket()
_probe.close()

import json  # noqa: E402
import sys  # noqa: E402

BLOB = b"relocated-needle=42;" * 500


def main(phase: str) -> None:
    from clio_agent.arc import storage  # noqa: PLC0415
    from clio_agent.arc.clio_core_relocate import last_relocation  # noqa: PLC0415
    from clio_agent.arc.runtime_stop import stop_runtime_daemon  # noqa: PLC0415

    store = storage.make_arc_store(backend="cte", namespace="relocate-e2e")
    result: dict[str, object] = {"store_type": type(store).__name__}
    if phase == "write":
        store.put("segments", "blob", BLOB)
    got = store.get("segments", "blob")
    result["read"] = None if got is None else len(got)
    result["identical"] = got == BLOB
    result["relocation"] = last_relocation()
    outcome = stop_runtime_daemon(store._config_path, "error")  # type: ignore[attr-defined]
    result["stopped"] = outcome.stopped
    print("RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
