"""Subprocess client entrypoint for the clio-core auto-offload / reload acceptance test.

This runs the **real** ``arc/storage.py`` ``ClioCoreStore`` path (not raw binding
calls) against a *private*, env-configured clio-core daemon. It is deliberately
a subprocess for two reasons:

* clio-core's process-global one-init-per-process guard (``ClioCoreStore._initialized``)
  means a test process that also drives clio-core elsewhere cannot re-init a second
  runtime; a fresh subprocess gets a clean guard.
* clio-core#722: client ops against a *dead* runtime access-violate (0xC0000005)
  and take the whole process down. Isolating the ops in a subprocess turns that
  crash into a non-zero exit that FAILS the test, instead of killing the pytest
  host.

Behaviour: write a marker-laden working set that exceeds the ram tier's
``capacity_limit`` (forcing the backend to transparently offload cold blobs to
its disk tier), then read every blob back through the same store and confirm
byte-identity. Results are written as JSON to ``$CLIO_OFFLOAD_OUT`` so the parent
can assert on them even if a late native fault perturbs stdout.

Configuration is entirely via environment (set by the parent test):

* ``CLIO_ARC_STORE=cte`` / ``CLIO_ARC_STORE_CONFIG`` / ``CLIO_CORE_PORT`` /
  ``CLIO_SERVER_CONF`` — point the real store at the private daemon + config.
* ``CLIO_OFFLOAD_RUN_ID`` — the per-run marker token (the parent derives the
  on-disk needle from it).
* ``CLIO_OFFLOAD_OUT`` — path to write the JSON result to.
* ``CLIO_OFFLOAD_TOTAL_MB`` — target working-set size in MB (default 30).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import traceback
from typing import Any

_KIND = "segments"
_BLOB_BYTES = 1_000_000  # ~1 MB per blob; count is derived from the MB target


def _marker(run_id: str) -> bytes:
    """Return the per-run marker, padded to a multiple of 3 bytes.

    A length that is a multiple of 3 makes ``base64(marker * k) == base64(marker) * k``
    (base64 encodes in 3-byte groups), so a contiguous run of the marker survives
    base64 wrapping as a contiguous, greppable run of ``base64(marker)`` on disk.

    Args:
        run_id: The per-run token shared with the parent test.

    Returns:
        The marker bytes, ``len(...) % 3 == 0``.
    """
    marker = f"CLIO_CLIO_CORE_OFFLOAD_{run_id}".encode()
    while len(marker) % 3:
        marker += b"_"
    return marker


def _build_blobs(marker: bytes, total_mb: int) -> list[tuple[str, bytes]]:
    """Build the working-set blobs.

    Each blob leads with an aligned run of ``marker`` (so its base64 is findable on
    the disk tier) followed by a unique random tail (so every blob is distinct and
    a byte-identical read-back is a real reload proof, not a trivial constant).

    Args:
        marker: The aligned marker bytes from :func:`_marker`.
        total_mb: Target total working-set size in MB.

    Returns:
        A list of ``(blob_name, data)`` pairs.
    """
    head = marker * 256  # aligned (multiple of 3) -> clean base64 split
    tail_len = _BLOB_BYTES - len(head)
    count = max(1, (total_mb * 1_000_000) // _BLOB_BYTES)
    return [(f"offload_{i:04d}", head + os.urandom(tail_len)) for i in range(count)]


def _emit(out_path: str, result: dict[str, Any]) -> None:
    """Write ``result`` as JSON to ``out_path`` (best-effort, flushed)."""
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    """Run the offload/reload ops and report. Returns a process exit code."""
    run_id = os.environ["CLIO_OFFLOAD_RUN_ID"]
    out_path = os.environ["CLIO_OFFLOAD_OUT"]
    total_mb = int(os.environ.get("CLIO_OFFLOAD_TOTAL_MB", "30"))
    result: dict[str, Any] = {"run_id": run_id, "stage": "start"}
    try:
        from clio_agent.arc.storage import ClioCoreStore, make_arc_store, release_runtime_client

        store = make_arc_store(backend="cte")
        result["store_type"] = type(store).__name__
        if not isinstance(store, ClioCoreStore):
            # A silent LocalFS fallback would make the proof vacuous.
            result["error"] = f"expected ClioCoreStore, got {type(store).__name__}"
            _emit(out_path, result)
            return 3

        marker = _marker(run_id)
        blobs = _build_blobs(marker, total_mb)
        put_shas = {name: hashlib.sha256(data).hexdigest() for name, data in blobs}

        result["stage"] = "put"
        for name, data in blobs:
            store.put(_KIND, name, data)

        result["stage"] = "get"
        mismatches: list[str] = []
        for name, _data in blobs:
            got = store.get(_KIND, name)
            if got is None or hashlib.sha256(got).hexdigest() != put_shas[name]:
                mismatches.append(name)

        result.update(
            stage="done",
            put_count=len(blobs),
            total_bytes=sum(len(d) for _, d in blobs),
            marker_b64=base64.b64encode(marker).decode("ascii"),
            readback_identical=(not mismatches),
            mismatches=mismatches,
        )
        _emit(out_path, result)
        return 0 if not mismatches else 4
    except Exception as exc:  # noqa: BLE001 - report ANY failure to the parent
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        try:
            _emit(out_path, result)
        except Exception:  # noqa: BLE001,S110 - out-path unwritable: fall through
            pass
        return 5
    finally:
        # Deterministically release this subprocess's clio-core client (last-one-out
        # stops the PRIVATE daemon) instead of leaning on atexit, so a client is never
        # left ghost-registered even if the interpreter is torn down abruptly. The parent
        # test still force-reaps as belt-and-suspenders. Best-effort: a release failure
        # must not mask the op result the parent asserts on.
        try:
            release_runtime_client()
        except Exception:  # noqa: BLE001,S110 - import/release failure: parent reap covers it
            pass


if __name__ == "__main__":
    sys.exit(main())
