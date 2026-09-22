#!/usr/bin/env python3
"""Prove a relocated bundled runtime selects the tiered clio-core ARC backend.

``build-gact-runtime.ps1`` prunes the installed image hard. A prune casualty or
a loader problem leaves imports healthy and only shows up later, as ARC quietly
running on local files instead of clio-core, on the user's machine. This script
is the build-time proof that the RELOCATED image still reaches clio-core.

It exists as a file rather than a ``python -c`` one-liner because the useful
part is the failure path: :func:`clio_agent.arc.storage.make_arc_store` degrades
LOUDLY by design, which means it reports a typed reason but swallows the
traceback. On a degrade this re-runs the same initialization WITHOUT that
wrapper so the actual exception and its stack reach the build log.

Exit status is 0 when clio-core backs ARC, 1 otherwise.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _resolve_cte_config() -> str:
    """Resolve the CTE config exactly as ``make_arc_store`` does for ``cte``."""
    from clio_agent import conf, paths
    from clio_agent.arc.clio_core_config import default_cte_config_path

    configured = conf.resolve(
        "arc.store_config", env="CLIO_ARC_STORE_CONFIG", default="", cast=conf.as_str
    )
    if configured:
        return configured
    workspace_config = paths.workspace_core_dir() / "cte.yaml"
    return str(workspace_config) if workspace_config.is_file() else default_cte_config_path()


def _report_degrade_cause() -> None:
    """Re-run the clio-core init without the degrade wrapper, printing the stack."""
    from clio_agent.arc import clio_core_file_capacity
    from clio_agent.arc.storage import ClioCoreStore

    config_path = _resolve_cte_config()
    print(f"arc-smoke: re-running clio-core init with config {config_path}", file=sys.stderr)
    try:
        clio_core_file_capacity.preflight_clio_core_config(config_path, env=os.environ)
        ClioCoreStore(config_path=config_path)
    except Exception:  # noqa: BLE001 - diagnostic: the stack IS the output
        traceback.print_exc()
    else:
        print(
            "arc-smoke: the re-run SUCCEEDED; the degrade is not reproducible in-process",
            file=sys.stderr,
        )


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
    """Return 0 when ARC is backed by clio-core, 1 on any degrade."""
    from clio_agent.arc.storage import ClioCoreStore, make_arc_store

    store = make_arc_store(backend="cte")
    if isinstance(store, ClioCoreStore):
        print("arc-smoke: ClioCoreStore")
        return 0

    print(f"arc-smoke: ARC degraded to {type(store).__name__}", file=sys.stderr)
    _report_degrade_cause()
    _dump_runtime_log()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
