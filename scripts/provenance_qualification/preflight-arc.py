#!/usr/bin/env python3
"""Fail-fast write/read/delete qualification for the configured ARC store."""

from __future__ import annotations

import ctypes
import json
import os
import sys
from uuid import uuid4

from clio_agent.arc.storage import ClioCoreStore, make_arc_store


_IO_URING_SETUP_SYSCALL = 425


def _require_io_uring() -> None:
    """Fail with an actionable error when container security blocks io_uring."""
    if sys.platform != "linux":
        return

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    params = ctypes.create_string_buffer(256)
    fd = libc.syscall(
        ctypes.c_long(_IO_URING_SETUP_SYSCALL),
        ctypes.c_uint(2),
        ctypes.byref(params),
    )
    if fd >= 0:
        os.close(fd)
        return

    error_number = ctypes.get_errno()
    if error_number in {1, 13}:
        raise RuntimeError(
            "ARC preflight cannot create an io_uring instance. Docker's default "
            "seccomp profile blocks the pinned iowarp-core runtime; launch this "
            "isolated qualification container with "
            "--security-opt seccomp=unconfined."
        )
    raise RuntimeError(
        "ARC preflight cannot create an io_uring instance: "
        f"errno={error_number} ({os.strerror(error_number)})"
    )


def main() -> int:
    """Verify that the configured clio-core store is writable, not merely listening."""
    _require_io_uring()
    store = make_arc_store(backend="cte")
    if not isinstance(store, ClioCoreStore):
        raise RuntimeError("ARC preflight degraded away from the required clio-core backend")

    name = f"__qualification_preflight_{uuid4().hex}"
    payload = b"clio-arc-write-read-delete-v1"
    store.put("segments", name, payload, search_text="qualification sentinel")
    observed = store.get("segments", name)
    if observed != payload:
        raise RuntimeError(
            f"ARC preflight read mismatch: expected={payload!r}, observed={observed!r}"
        )
    store.delete("segments", name)
    if store.get("segments", name) is not None:
        raise RuntimeError("ARC preflight delete did not remove the sentinel")

    print(json.dumps({"arc": "cte", "write_read_delete": "ready"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
