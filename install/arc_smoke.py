#!/usr/bin/env python3
"""Prove a relocated bundled runtime selects the tiered clio-core ARC backend.

``build-gact-runtime.ps1`` prunes the installed image hard. A prune casualty or
a loader problem leaves imports healthy and only shows up later, as ARC quietly
running on local files instead of clio-core, on the user's machine. This script
is the build-time proof that the RELOCATED image still reaches clio-core.

It exists as a file rather than a ``python -c`` one-liner because the useful
part is the failure path: :func:`clio_agent.arc.storage.make_arc_store` raises a
typed ``ArcStoreUnavailableError`` (clio-core is the only store) chained to the real
cause; this prints the reason, the full traceback and the daemon log tail.

Exit status is 0 when clio-core backs ARC, 1 otherwise.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _dump_runtime_log() -> None:
    """Print the tail of the clio-core daemon log, wherever this run put it."""
    from clio_agent.arc.clio_core_config import runtime_state_dir

    log_path = Path(runtime_state_dir()) / "clio-runtime.log"
    if not log_path.is_file():
        print(f"arc-smoke: no daemon log at {log_path}", file=sys.stderr)
        return
    print(f"arc-smoke: --- {log_path} (tail) ---", file=sys.stderr)
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-60:]:
        print(line, file=sys.stderr)


def main() -> int:
    """Return 0 when clio-core backs ARC, 1 when it cannot be brought up."""
    from clio_agent.arc.init_degradation import ArcStoreUnavailableError
    from clio_agent.arc.storage import make_arc_store

    try:
        make_arc_store(backend="cte")
    except ArcStoreUnavailableError as exc:
        print(f"arc-smoke: clio-core unavailable (reason={exc.reason})", file=sys.stderr)
        traceback.print_exc()
        _dump_runtime_log()
        return 1
    print("arc-smoke: ClioCoreStore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
