#!/usr/bin/env python3
"""Boot a gact server with an extra IN-PROCESS transform tool for the #1031 P3 gate.

The native fs/shell tools have no DESIGNATED output arg, so they never produce a
provenance ``generated`` edge (only domain/marketplace tools do — and those fail to
spawn under the Windows codex sandbox, a separate #974 finding). To demonstrate the
full cross-job ``b = transform(a)`` lineage on Windows WITHOUT that blocker, we mount
one synthetic transform tool the same way ``fs``/``shell`` are mounted — in-process on
the gateway (``tools/gateway.py::_mount_builtins``), so it never touches the codex
spawn path. It takes ``input_path`` (→ a ``used`` edge, resolved cross-workspace) and
``output_path`` (a designation arg → a ``generated`` edge), so ONE call yields both
edges and the lineage chains a → b across jobs.

Usage: ``python scripts/live_gate_boot.py <port>``
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from fastmcp import FastMCP

import clio_agent.tools.gateway as _gw

xform_server = FastMCP("xform")


@xform_server.tool
def summarize_csv(input_path: str, output_path: str) -> dict:
    """Read a CSV at ``input_path``, sum its ``revenue`` column, and write a summary
    CSV to ``output_path``. Returns the written path under ``local_path`` (a
    designated-output result key) plus the computed total.

    Args:
        input_path: Absolute path to the source CSV (header + rows).
        output_path: Absolute path to write the summary CSV to.
    """
    src = Path(input_path)
    rows = list(csv.DictReader(src.open(encoding="utf-8")))
    total = 0.0
    for r in rows:
        val = r.get("revenue") or list(r.values())[-1]
        try:
            total += float(val)
        except (TypeError, ValueError):
            continue
    total_out = int(total) if total.is_integer() else total
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"metric,value\ntotal_revenue,{total_out}\n", encoding="utf-8")
    return {"local_path": str(out), "total_revenue": total_out, "rows_read": len(rows)}


def _install_xform() -> None:
    """Patch ``_mount_builtins`` so every gateway also mounts the xform tool
    in-process, register it in the static ``TOOL_CATALOG`` so blueprint validation
    accepts the ``xform_summarize_csv`` reference, then rebuild the module gateway
    singleton so it is present. Runs before the app is built (and thus before any
    blueprint validation), so the mutated catalog is the one validation reads."""
    _orig = _gw._mount_builtins

    def _patched(gw) -> None:
        _orig(gw)
        _gw._mount_with_namespace(gw, xform_server, "xform")

    _gw._mount_builtins = _patched
    _gw.gateway = _gw._new_base_gateway()

    # Register the in-process tool in the static catalog so
    # agent_blueprints._validate_agent_tool_references accepts it (it checks
    # set(TOOL_CATALOG), not the runtime-derived catalog).
    from clio_agent.tools.catalog import TOOL_CATALOG, ToolCatalogEntry

    TOOL_CATALOG["xform_summarize_csv"] = ToolCatalogEntry(
        name="xform_summarize_csv",
        owner="workspace",
        tags=frozenset({"workspace", "transform", "write"}),
        visible_to=frozenset({"workspace", "planner"}),
        planner_visible=True,
    )


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 17931
    _install_xform()
    from clio_agent.gact.app import run_server

    run_server(host="127.0.0.1", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
