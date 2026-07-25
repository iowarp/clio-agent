#!/usr/bin/env python3
"""Standalone stdio FastMCP server exposing one designated-output transform tool for
the #1031 P3 provenance gate.

Declared in ``react-leaf-xform/AGENT.md`` as an ``mcp_servers`` entry (namespace
``xform``), so the tool surfaces as ``xform_summarize_csv`` and is recognized by the
blueprint validator AND the runtime tool executor (unlike an ad-hoc in-process mount,
which the executor's custom-tool guard rejects). It takes ``input_path`` (→ a ``used``
lineage edge, resolved cross-workspace) and ``output_path`` (a designation arg → a
``generated`` edge), so ONE call produces both edges and ``b = transform(a)`` chains
across jobs. Depends only on ``fastmcp`` + stdlib, so it spawns cleanly as a plain
``python`` subprocess (run with the OS fence off to avoid the Windows #974 fleet-spawn
bug).
"""

from __future__ import annotations

import csv
from pathlib import Path

from fastmcp import FastMCP

server = FastMCP("xform")


@server.tool
def summarize_csv(input_path: str, output_path: str) -> dict:
    """Read the CSV at ``input_path``, sum its ``revenue`` column, and write a summary
    CSV to ``output_path``. Returns the written path under ``local_path`` plus the total.

    Args:
        input_path: Absolute path to the source CSV (header + rows).
        output_path: Absolute path to write the summary CSV to.
    """
    src = Path(input_path)
    rows = list(csv.DictReader(src.open(encoding="utf-8")))
    total = 0.0
    for r in rows:
        val = r.get("revenue") or (list(r.values())[-1] if r else "0")
        try:
            total += float(val)
        except (TypeError, ValueError):
            continue
    total_out = int(total) if float(total).is_integer() else total
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"metric,value\ntotal_revenue,{total_out}\n", encoding="utf-8")
    return {"local_path": str(out), "total_revenue": total_out, "rows_read": len(rows)}


if __name__ == "__main__":
    server.run()
