"""Capture the declared-server MCP baseline for the #1274 unification campaign.

Records, for the canonical v1 fleet (the provenance-qualification list) plus the
v2 ``web`` server, exactly the two surfaces C1-S1 must keep byte-identical for
v1 servers:

1. the ``/v1/mcp/handshake`` rows (same seams the route calls:
   ``handshake_mcp_servers`` + ``handshake_server_row``), with volatile fields
   (latency) dropped and list order normalized; and
2. each declared server's LIVE tool listing via ``gateway._list_declared_tools``
   -- deliberately the raw cache-free lister, so the baseline can never be
   minted from (or diffed against) a stale on-disk listing-cache entry -- as
   ``<server>_<tool> -> sha256(canonical schema)`` over the schema-bearing
   fields only (name/description/input_schema/output_schema/annotations), so a
   digest change is attributable to an actual contract move.

Run it on the pre-S1 base to mint the baseline fixture, and again at C1-S6 to
diff (#1286 leg i). The fleet is declared through a throwaway workspace
``.clio/mcp.yaml`` so the REAL declared path (workspace scope -> transport_for
-> gateway mount) is exercised, never a synthetic spec object.

Usage:
    uv run python scripts/mcp_v1_baseline.py --out tests/fixtures/mcp_v1_baseline/baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

# The canonical fleet: scripts/provenance_qualification/Dockerfile:13-16.
FLEET = ("geo", "ndp", "pandas", "plot", "hdf5", "parquet", "web")


def _write_workspace(root: Path) -> Path:
    """Write a throwaway workspace declaring the fleet; return its path."""
    clio_dir = root / ".clio"
    clio_dir.mkdir(parents=True, exist_ok=True)
    lines = ["mcp_servers:"]
    lines.extend(f"  {name}: clio-kit mcp-server {name}" for name in FLEET)
    (clio_dir / "mcp.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _git_commit() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=False
    )
    return out.stdout.strip() or "unknown"


def _clio_kit_version() -> str:
    out = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True, check=False)
    for line in out.stdout.splitlines():
        if line.startswith("clio-kit "):
            return line.strip()
    return "unknown"


def _normalized_handshake_row(row: dict[str, Any]) -> dict[str, Any]:
    """Drop volatile fields; sort the tool list."""
    keep = dict(row)
    keep.pop("latency_ms", None)
    # Era rows reflect process-local execution history, not this probe; a fresh
    # capture process has none. Keep them anyway (expected None) so a future
    # diff surfaces any change in what the probe itself stamps.
    keep["tools"] = sorted(keep.get("tools") or [])
    return keep


def _schema_digest(tool: Any) -> str:
    """Digest ONLY the schema-bearing fields, so a change is attributable."""

    annotations = getattr(tool, "annotations", None)
    payload = {
        "name": tool.name,
        "description": getattr(tool, "description", None),
        "input_schema": getattr(tool, "input_schema", None),
        "output_schema": getattr(tool, "output_schema", None),
        "annotations": annotations.model_dump(mode="json", exclude_none=True)
        if annotations is not None
        else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _capture(workspace: Path) -> dict[str, Any]:
    from clio_agent.gact.routes.mcp_rows import handshake_server_row
    from clio_agent.providers.handshake import handshake_mcp_servers
    from clio_agent.tools.gateway import _list_declared_tools
    from clio_agent.tools.mcp_config import load_mcp_servers

    specs = load_mcp_servers(cwd=workspace)
    declared = {name: spec for name, spec in specs.items() if name in FLEET}
    missing = sorted(set(FLEET) - set(declared))
    if missing:
        raise RuntimeError(f"fleet servers missing from declared specs: {missing}")

    reports = await handshake_mcp_servers(list(declared.values()))
    handshake_rows = sorted(
        (_normalized_handshake_row(handshake_server_row(r)) for r in reports),
        key=lambda row: str(row.get("name")),
    )

    # The raw per-spec lister: its own transport, no listing-cache read/write
    # (list_tool_definitions consults the 24h on-disk cache - a baseline minted
    # from a cached listing would diff against reality at S6).
    declared_tools: dict[str, str] = {}
    for name in sorted(declared):
        for tool in _list_declared_tools(declared[name]):
            declared_tools[f"{name}_{tool.name}"] = _schema_digest(tool)

    return {"handshake": handshake_rows, "declared_tools": declared_tools}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output JSON path (repo-relative or absolute)")
    args = parser.parse_args()

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = REPO / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mcp-baseline-ws-") as tmp:
        workspace = _write_workspace(Path(tmp))
        captured = asyncio.run(_capture(workspace))

    unreachable = [r["name"] for r in captured["handshake"] if not r.get("reachable")]
    if unreachable:
        # A partial capture must never become a committable baseline.
        print(f"NOT written: unreachable servers {unreachable}")
        return 1

    payload = {
        "meta": {
            "commit": _git_commit(),
            "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "clio_kit": _clio_kit_version(),
            "fleet": list(FLEET),
            "listing_source": "gateway._list_declared_tools (live, cache-free)",
        },
        **captured,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"baseline written: {out_path}")
    print(f"handshake rows: {len(captured['handshake'])}")
    print(f"declared tools: {len(captured['declared_tools'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
