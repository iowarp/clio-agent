#!/usr/bin/env python3
"""Measure a workspace's CAS store footprint and assert it against the budget (#972).

CAS is the only NEW artifact storage (owner decision #966.8), so it is release-gated
the #930/#1001 way: small artifacts are copied sha256-addressed under
``<workspace>/.clio/agent/artifacts/cas`` and the reachability GC
(:mod:`clio_agent.gact.artifacts.cas_gc`) keeps the store under
``artifacts.cas_budget_bytes`` by evicting unreachable blobs oldest-first. This
script is the release-gating teeth: it sums the on-disk size of a workspace's CAS
blobs, prints the total, and (with ``--assert-budget``) exits NONZERO when the
total exceeds the recorded budget in ``scripts/artifact_store_budget.json``.

It is intentionally CI-cheap: a directory walk over a store bounded at 512 MiB, and
near-empty on a fresh box (the steady-state bound is enforced at runtime by the GC;
this script proves the bound holds).

Usage::

    uv run python scripts/artifact_store_footprint.py --root <workspace>   # measure
    uv run python scripts/artifact_store_footprint.py --root <ws> --assert-budget
    uv run python scripts/artifact_store_footprint.py --json               # machine-readable
    uv run python scripts/artifact_store_footprint.py --budget-mb 256 --assert-budget
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_MIB = 1024**2
_REPO = Path(__file__).resolve().parents[1]
_BUDGET_FILE = _REPO / "scripts" / "artifact_store_budget.json"

# A 5% tolerance so honest measurement noise (a blob mid-ingest) does not flap CI,
# matching the disk_budget / mcp_mem_budget gate convention.
BUDGET_TOLERANCE = 1.05


def _cas_dir(workspace_root: Path) -> Path:
    """The CAS store dir for a workspace root (mirrors ``cas.cas_root_for``)."""
    return workspace_root / ".clio" / "agent" / "artifacts" / "cas"


def measure(workspace_root: Path) -> int:
    """Sum the on-disk size of every CAS blob under ``workspace_root`` (``.tmp`` aside)."""
    cas = _cas_dir(workspace_root)
    if not cas.is_dir():
        return 0
    total = 0
    for root, dirs, files in os.walk(cas, followlinks=False):
        # The scratch ``.tmp`` staging dir holds unpublished temp files — not store bytes.
        dirs[:] = [d for d in dirs if not (Path(root) == cas and d == ".tmp")]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def load_budget_mb() -> float:
    """Load the recorded per-workspace CAS budget (MiB) from the budget file."""
    data = json.loads(_BUDGET_FILE.read_text(encoding="utf-8"))
    return float(data["steady_state_mb"])


def check_footprint(total_mb: float, budget_mb: float) -> tuple[bool, str]:
    """Pure budget verdict: total (MiB) must not exceed ``budget_mb`` * tolerance.

    A non-positive or malformed budget FAILS (never passes silently), mirroring
    ``scripts/disk_footprint.check_footprint``.
    """
    if not isinstance(budget_mb, (int, float)) or budget_mb <= 0:
        return False, f"malformed budget: budget_mb={budget_mb!r} (must be > 0)"
    ceiling = budget_mb * BUDGET_TOLERANCE
    ok = total_mb <= ceiling
    return ok, (
        f"total={total_mb:.3f}MiB budget={budget_mb:.3f}MiB ceiling={ceiling:.3f}MiB ok={ok}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure a workspace CAS store footprint (#972).")
    parser.add_argument(
        "--root",
        type=str,
        default=".",
        help="Workspace root whose CAS store to measure (default: cwd).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--assert-budget",
        action="store_true",
        help="Exit nonzero when the store exceeds the budget.",
    )
    parser.add_argument(
        "--budget-mb",
        type=float,
        default=None,
        help="Override the budget (MiB); default reads scripts/artifact_store_budget.json.",
    )
    args = parser.parse_args(argv)

    total = measure(Path(args.root).expanduser().resolve(strict=False))
    total_mb = total / _MIB
    budget_mb = args.budget_mb if args.budget_mb is not None else load_budget_mb()
    ok, detail = check_footprint(total_mb, budget_mb)

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(Path(args.root).resolve(strict=False)),
                    "total_bytes": total,
                    "total_mib": round(total_mb, 4),
                    "budget_mib": budget_mb,
                    "ok": ok,
                    "detail": detail,
                },
                indent=2,
            )
        )
    else:
        print(f"  CAS store {total_mb:8.3f} MiB  (budget {budget_mb:.2f} MiB)")
        print(("OK: " if ok else "OVER BUDGET: ") + detail)

    if args.assert_budget:
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
