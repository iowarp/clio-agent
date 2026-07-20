#!/usr/bin/env python3
"""Measure clio-owned cache/state disk footprint and assert it against the budget (#1001).

Bounded disk is release-gating (iowarp/clio-agent#1001): a desktop agent must not demand
tens of GB of cache from a user's machine. This script is the release-gating teeth — it
sums the disk footprint of the roots clio-agent OWNS and can evict (the MCP uv spawn cache
and the regenerable user cache), prints a per-root + total breakdown, and exits NONZERO
when the total exceeds the recorded budget in ``scripts/disk_budget.json``.

It is intentionally CI-cheap: a directory walk (``du``) over near-empty roots on a fresh
box. The steady-state bound is enforced at runtime by the boot prune in
:mod:`clio_agent.tools.mcp_cache`; this script proves the bound holds.

Scope note: the ambient uv cache and clio-kit's private cache are separately owned (dev
environment / upstream clio-kit#334) and are NOT counted here — only what this repo owns.

Usage::

    uv run python scripts/disk_footprint.py            # measure + assert against budget
    uv run python scripts/disk_footprint.py --json      # machine-readable
    uv run python scripts/disk_footprint.py --budget-gb 3.0   # override the budget
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_GIB = 1024**3
_REPO = Path(__file__).resolve().parents[1]
_BUDGET_FILE = _REPO / "scripts" / "disk_budget.json"

# A 5% tolerance so honest measurement noise (a transient wheel mid-download) does not flap
# CI, matching the mcp_mem_budget gate convention.
BUDGET_TOLERANCE = 1.05


def _dir_size(path: Path, *, exclude: set[Path] | None = None) -> int:
    """Sum the on-disk size of regular files under ``path`` (symlinks not followed).

    Directories in ``exclude`` (and their subtrees) are skipped, so overlapping roots can
    be counted without double-billing.
    """
    exclude = exclude or set()
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        # prune excluded subtrees in place
        dirs[:] = [d for d in dirs if (root_path / d) not in exclude]
        if root_path in exclude:
            dirs[:] = []
            continue
        for name in files:
            fp = os.path.join(root, name)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                continue
    return total


def clio_owned_roots() -> list[tuple[str, Path]]:
    """Return the ``(label, path)`` clio-owned cache roots this budget bounds."""
    from clio_agent import paths  # noqa: PLC0415 - import after argparse so --help is fast

    cache = paths.user_cache_dir()
    mcp_uv = cache / "mcp-uv-cache"
    return [
        ("mcp-uv-cache", mcp_uv),
        ("user-cache (excl. mcp-uv-cache)", cache),
    ]


def measure() -> tuple[list[tuple[str, int]], int]:
    """Measure each clio-owned root; return ``(per_root, total_bytes)`` with no overlap."""
    from clio_agent import paths  # noqa: PLC0415

    mcp_uv = paths.user_cache_dir() / "mcp-uv-cache"
    per_root: list[tuple[str, int]] = []
    for label, path in clio_owned_roots():
        if not path.exists():
            per_root.append((label, 0))
            continue
        exclude = {mcp_uv} if label.startswith("user-cache") else None
        per_root.append((label, _dir_size(path, exclude=exclude)))
    total = sum(size for _label, size in per_root)
    return per_root, total


def load_budget_gb() -> float:
    """Load the recorded steady-state budget (GiB) from ``disk_budget.json``."""
    data = json.loads(_BUDGET_FILE.read_text(encoding="utf-8"))
    return float(data["steady_state_gb"])


def check_footprint(total_gb: float, budget_gb: float) -> tuple[bool, str]:
    """Pure budget verdict: total (GiB) must not exceed ``budget_gb`` * tolerance.

    A non-positive or malformed budget FAILS (never passes silently), mirroring
    ``scripts/mcp_mem_attribution.check_budget``.
    """
    if not isinstance(budget_gb, (int, float)) or budget_gb <= 0:
        return False, f"malformed budget: budget_gb={budget_gb!r} (must be > 0)"
    ceiling = budget_gb * BUDGET_TOLERANCE
    ok = total_gb <= ceiling
    return ok, (
        f"total={total_gb:.3f}GiB budget={budget_gb:.3f}GiB ceiling={ceiling:.3f}GiB ok={ok}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure clio-owned disk footprint (#1001).")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--budget-gb",
        type=float,
        default=None,
        help="Override the budget (GiB); default reads scripts/disk_budget.json.",
    )
    args = parser.parse_args(argv)

    per_root, total = measure()
    total_gb = total / _GIB
    budget_gb = args.budget_gb if args.budget_gb is not None else load_budget_gb()
    ok, detail = check_footprint(total_gb, budget_gb)

    if args.json:
        print(
            json.dumps(
                {
                    "roots": [
                        {"label": label, "bytes": size, "gib": round(size / _GIB, 4)}
                        for label, size in per_root
                    ],
                    "total_bytes": total,
                    "total_gib": round(total_gb, 4),
                    "budget_gib": budget_gb,
                    "ok": ok,
                    "detail": detail,
                },
                indent=2,
            )
        )
    else:
        for label, size in per_root:
            print(f"  {label:<34} {size / _GIB:8.3f} GiB")
        print(f"  {'TOTAL':<34} {total_gb:8.3f} GiB  (budget {budget_gb:.2f} GiB)")
        print(("OK: " if ok else "OVER BUDGET: ") + detail)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
