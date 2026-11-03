"""Generate every fixture and the manifest.

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.fixtures.generate``

Writes into ``--out`` (default ``.cloud-run/eval-fixtures/``), replacing what is
there::

    tree/         git archive <base> src/clio_agent      P1, P4
    p2/           the generated 5,000-line module        P2
    p3/tools/     a copy of src/clio_agent/tools with five seeded bugs   P3
    home/         an almost empty folder                 P5 to P10
    h1/           the ledgerlite repository              H1, H3
    h2/           the recstream repository               H2
    h4-export/    a growth-chamber data export, 140 files, about 300 MB   H4, H5
    marketplace/  the clio-eval plugins                  P5 to P10
    manifest.json sha256 per file (git internals excepted) and the answer keys

The same seed, base commit and options always give the same manifest.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.fixtures import export_h4, repos, seed_bugs, source_tree
from scripts.eval.tool_calling.fixtures.common import (
    archive_tree,
    derive_rng,
    hash_tree,
    reset_dir,
    write_text,
)
from scripts.eval.tool_calling.fixtures.plugins import write_marketplace
from scripts.eval.tool_calling.paths import (
    DEFAULT_FIXTURES,
    MANIFEST_NAME,
    MARKETPLACE_DIR,
    REPO_ROOT,
)
from scripts.eval.tool_calling.servers.records import negative_answer

SCHEMA = "clio.eval.tool_calling.fixtures.v1"
GENERATOR_VERSION = 2
DEFAULT_BASE = "32d7cbc6"
DEFAULT_SEED = 7

HOME_README = """# Home notes

This folder belongs to the household assistant. The house has lights, heating,
locks, media, sensors, a garden with irrigation zones and a home battery.
"""


def generate(
    out: Path,
    *,
    seed: int = DEFAULT_SEED,
    base: str = DEFAULT_BASE,
    repo: Path = REPO_ROOT,
    tree_path: str = "src/clio_agent",
    p3_package: str = "tools",
    p4_subsystems: tuple[str, ...] = source_tree.P4_SUBSYSTEMS,
    python: str = "python",
    h4_scale: int = 1,
) -> dict[str, Any]:
    """Write all fixtures under ``out``; return the manifest (also written).

    ``h4_scale`` thins the H4 export's sampling (1 is the full 300 MB; tests use 360).
    """
    reset_dir(out)
    tasks: dict[str, Any] = {}

    tree = out / "tree"
    full_base = archive_tree(repo, base, tree_path, tree)
    tasks["P1"] = {
        "fixture": "tree",
        "answer": source_tree.pick_p1_function(tree, derive_rng(seed, "P1")),
    }
    tasks["P4"] = {
        "fixture": "tree",
        "answer": source_tree.p4_checklist(tree, tree_path, p4_subsystems),
    }

    tasks["P2"] = {
        "fixture": "p2",
        "answer": source_tree.write_p2(out / "p2", derive_rng(seed, "P2")),
    }

    p3 = out / "p3"
    shutil.copytree(tree / tree_path / p3_package, p3 / p3_package)
    tasks["P3"] = {
        "fixture": "p3",
        "answer": {
            "package": p3_package,
            "issues": seed_bugs.seed_bugs(p3, p3_package, derive_rng(seed, "P3")),
        },
    }

    write_text(out / "home" / "README.md", HOME_README)
    for task, size in (("P5", 20), ("P6", 200), ("P7", 2000)):
        tasks[task] = {"fixture": "home", "answer": negative_answer(size, seed)}
    tasks["P8"] = {
        "fixture": "home",
        "answer": {
            "tool": "climate_set_temperature",
            "server": "synthetic-home",
            "args": {"entity_id": "climate.guest_bedroom", "temperature": 19.5},
        },
    }
    tasks["P9"] = {
        "fixture": "home",
        "answer": {
            "tool": "irrigation_run_zone",
            "server": "synthetic-home",
            "args": {"zone_id": "irrigation.back_garden_beds", "duration_minutes": 10},
        },
    }
    tasks["P10"] = {"fixture": "home", "answer": {"deferred_servers": ["synthetic-home"]}}

    scratch = out / ".verify-scratch"
    tasks["H1"] = {"fixture": "h1", "answer": repos.build_h1(out / "h1", scratch)}
    tasks["H2"] = {"fixture": "h2", "answer": repos.build_h2(out / "h2", scratch)}
    tasks["H3"] = {"fixture": "h1", "answer": tasks["H1"]["answer"]}
    h4 = export_h4.write_export(out / export_h4.FIXTURE, seed, scale=h4_scale)
    tasks["H4"] = {"fixture": export_h4.FIXTURE, "answer": h4}
    tasks["H5"] = {"fixture": export_h4.FIXTURE, "answer": h4}
    tasks["H6"] = {"fixture": export_h4.FIXTURE, "answer": h4}

    market = write_marketplace(out / MARKETPLACE_DIR, seed=seed, python=python)
    manifest = {
        "schema": SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "base_commit": full_base,
        "tree_path": tree_path,
        "marketplace": {"dir": MARKETPLACE_DIR, **market},
        "tasks": tasks,
        "files": hash_tree(out),
    }
    write_text(out / MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fixtures.generate", description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--base", default=DEFAULT_BASE, help="commit to archive the tree from")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository holding --base")
    parser.add_argument("--tree-path", default="src/clio_agent")
    parser.add_argument("--p3-package", default="tools", help="package (under --tree-path) for P3")
    parser.add_argument("--p4-subsystems", default=",".join(source_tree.P4_SUBSYSTEMS))
    parser.add_argument(
        "--python", default="python", help="bare command the plugins start servers with"
    )
    parser.add_argument(
        "--h4-scale",
        type=int,
        default=1,
        help="thin the H4 export's sampling by this factor (a divisor of 360; 1 is full size)",
    )
    args = parser.parse_args(argv)
    manifest = generate(
        args.out.resolve(),
        seed=args.seed,
        base=args.base,
        repo=args.repo.resolve(),
        tree_path=args.tree_path,
        p3_package=args.p3_package,
        p4_subsystems=tuple(s for s in args.p4_subsystems.split(",") if s),
        python=args.python,
        h4_scale=args.h4_scale,
    )
    print(
        f"wrote {len(manifest['files'])} files and {len(manifest['tasks'])} answer keys to "
        f"{args.out} (base {manifest['base_commit'][:12]}, seed {manifest['seed']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
