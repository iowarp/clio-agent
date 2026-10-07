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

import argparse
import secrets
import socket
import sys
import traceback
from contextlib import ExitStack
from pathlib import Path

_CORE_PORT_FLOOR = 20_000
_CORE_PORT_CEILING = 32_000
_CORE_PORT_SPAN = 5


def _free_core_port_block() -> int:
    """Find five free endpoints below the Linux and Windows ephemeral ranges.

    The daemon binds adjacent RPC/transport endpoints. Checking only its base
    port can leave a half-started daemon when a neighboring endpoint is busy.
    Probes close before startup; avoiding ephemeral ports prevents outgoing
    connections from taking an adjacent endpoint in that interval.
    """
    choices = _CORE_PORT_CEILING - _CORE_PORT_FLOOR - _CORE_PORT_SPAN + 1
    for _ in range(400):
        base = _CORE_PORT_FLOOR + secrets.randbelow(choices)
        with ExitStack() as probes:
            try:
                for offset in range(_CORE_PORT_SPAN):
                    probe = probes.enter_context(socket.socket())
                    if sys.platform == "win32":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind(("0.0.0.0", base + offset))  # noqa: S104 - temporary bind probe
            except OSError:
                continue
            return base
    raise RuntimeError("no free five-port block below the ephemeral range for the ARC smoke")


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reserve-core-port", action="store_true")
    if parser.parse_args().reserve_core_port:
        try:
            print(_free_core_port_block())
        except RuntimeError as exc:
            print(f"arc-smoke: {exc}", file=sys.stderr)
            return 1
        return 0

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
