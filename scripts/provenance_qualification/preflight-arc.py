#!/usr/bin/env python3
"""Fail-fast write/read/delete qualification for the configured ARC store."""

from __future__ import annotations

import json
from uuid import uuid4

from clio_agent.arc.storage import ClioCoreStore, make_arc_store


def main() -> int:
    """Verify that the configured clio-core store is writable, not merely listening."""
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
