"""P1, P2 and P4: answer keys over a frozen source tree, and the 5,000-line file.

P1 picks a real function with several call sites, found with ``ast`` at
generation time. P2 writes a generated module with one fact deep inside and
decoys around it. P4 records a checklist of verifiable facts (module names and
entry points) for three subsystems.
"""

from __future__ import annotations

import ast
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.fixtures.common import write_text


@dataclass
class _Index:
    defs: dict[str, list[tuple[str, int, bool]]] = field(default_factory=lambda: defaultdict(list))
    calls: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return None


def index_tree(root: Path) -> _Index:
    """Every function definition and call site (by callee name) under ``root``."""
    index = _Index()
    for path in sorted(root.rglob("*.py")):
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(root).as_posix()
        top_level = {id(node) for node in tree.body}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.defs[node.name].append((rel, node.lineno, id(node) in top_level))
            elif isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else ""
                )
                if name:
                    index.calls[name].append((rel, node.lineno))
    return index


def pick_p1_function(
    root: Path,
    rng: random.Random,
    *,
    min_calls: int = 3,
    max_calls: int = 8,
    min_files: int = 2,
    min_name: int = 10,
) -> dict[str, Any]:
    """Choose a uniquely named module-level function with several call sites.

    Unique: no other function or method anywhere in the tree has the name, so
    every call site by that name is a call of this function.
    """
    index = index_tree(root)
    candidates: list[str] = []
    for name, defs in sorted(index.defs.items()):
        if len(defs) != 1 or not defs[0][2] or name.startswith("__") or len(name) < min_name:
            continue
        calls = sorted(set(index.calls.get(name, [])))
        files = {file for file, _ in calls}
        if min_calls <= len(calls) <= max_calls and len(files) >= min_files:
            candidates.append(name)
    if not candidates:
        raise ValueError(f"no P1 candidate function under {root}")
    name = rng.choice(candidates)
    (def_file, def_line, _), calls = index.defs[name][0], sorted(set(index.calls[name]))
    return {
        "function": name,
        "definition": {"file": def_file, "line": def_line},
        "callers": [{"file": file, "line": line} for file, line in calls],
        "candidates_considered": len(candidates),
    }


# --------------------------------------------------------------------------- P2

P2_FILE = "station_registry.py"
P2_FACT = "ACTIVE_STATION_DRIFT_TOLERANCE_PPM"
P2_LINES = 5000
_REGIONS = ("north", "south", "east", "west", "coast", "ridge", "valley", "delta", "plateau")
_SENSORS = ("barometer", "anemometer", "hygrometer", "pyranometer", "rain_gauge", "thermistor")


def _station_block(rng: random.Random, number: int) -> list[str]:
    region = rng.choice(_REGIONS)
    sensor = rng.choice(_SENSORS)
    code = f"{region[:3].upper()}{number:04d}"
    return [
        f"STATION_{code} = Station(",
        f'    code="{code}",',
        f'    region="{region}",',
        f"    latitude={rng.uniform(-60, 70):.4f},",
        f"    longitude={rng.uniform(-170, 170):.4f},",
        f"    elevation_m={rng.randint(0, 3200)},",
        f'    primary_sensor="{sensor}",',
        f"    sample_interval_s={rng.choice((10, 30, 60, 300))},",
        ")",
        "",
    ]


def _helper_block(number: int) -> list[str]:
    return [
        f"def station_group_{number:03d}(stations: list[Station]) -> list[str]:",
        f'    """Codes of group {number} stations that report at least hourly."""',
        "    return [s.code for s in stations if s.sample_interval_s <= 3600]",
        "",
        "",
    ]


def write_p2(dest: Path, rng: random.Random) -> dict[str, Any]:
    """Write the P2 folder; return its answer key."""
    value = rng.randint(21, 49)
    retired_value = value + rng.randint(9, 17)
    proposed_value = value - rng.randint(5, 9)
    fact_line = 4300 + rng.randint(-40, 40)
    header = [
        '"""Station registry for the regional observation network.',
        "",
        "Generated from the network inventory; edit the inventory, not this file.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
        "",
        "@dataclass(frozen=True)",
        "class Station:",
        "    code: str",
        "    region: str",
        "    latitude: float",
        "    longitude: float",
        "    elevation_m: int",
        "    primary_sensor: str",
        "    sample_interval_s: int",
        "",
        "",
    ]
    decoys = {
        1200 + rng.randint(-30, 30): [
            "# Stations retired before the 2024 refit keep their old tolerance.",
            f"RETIRED_STATION_DRIFT_TOLERANCE_PPM = {retired_value}",
            "",
        ],
        2600 + rng.randint(-30, 30): [
            "# Proposed in review, not adopted:",
            f"# {P2_FACT} = {proposed_value}",
            "",
        ],
    }
    fact = [
        "# Clock drift allowed before an active station is flagged for resync.",
        f"{P2_FACT} = {value}",
        "",
    ]
    lines = list(header)
    number = 0
    placed = False
    while True:
        for at, block in list(decoys.items()):
            if len(lines) >= at:
                lines.extend(block)
                del decoys[at]
        if not placed and len(lines) + 1 >= fact_line:
            lines.extend([""] * max(0, fact_line - 1 - len(lines)))
            lines.extend(fact)
            placed = True
            continue
        number += 1
        block = _helper_block(number) if number % 7 == 0 else _station_block(rng, number)
        if len(lines) + len(block) > P2_LINES:
            break
        lines.extend(block)
    lines.extend(["# end of registry"] * (P2_LINES - len(lines)))
    actual = next(i for i, line in enumerate(lines, 1) if line.startswith(f"{P2_FACT} ="))
    write_text(dest / P2_FILE, "\n".join(lines) + "\n")
    write_text(
        dest / "README.md",
        "# Observation network\n\n`station_registry.py` lists every station and its limits.\n",
    )
    return {
        "file": P2_FILE,
        "name": P2_FACT,
        "line": actual,
        "value": value,
        "decoy_values": [retired_value, proposed_value],
        "total_lines": len(lines),
    }


# --------------------------------------------------------------------------- P4

P4_SUBSYSTEMS = ("arc", "plugins", "providers")


def _module_lines(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("\n")


def _exports(package: Path) -> list[str]:
    tree = _parse(package / "__init__.py") if (package / "__init__.py").is_file() else None
    if tree is None:
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return []
            return sorted(str(v) for v in value)
    return []


def _public_defs(path: Path) -> list[str]:
    tree = _parse(path)
    if tree is None:
        return []
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    )


def p4_checklist(
    root: Path, package_root: str, subsystems: tuple[str, ...] = P4_SUBSYSTEMS
) -> dict[str, Any]:
    """Four largest modules plus up to two entry points per subsystem."""
    out: dict[str, Any] = {"package_root": package_root, "subsystems": {}}
    for name in subsystems:
        package = root / package_root / name
        modules = sorted(
            (p for p in package.glob("*.py") if p.name != "__init__.py"),
            key=lambda p: (-_module_lines(p), p.name),
        )
        facts = [
            {"kind": "module", "text": f"names the {p.stem} module", "match_any": [p.stem]}
            for p in modules[:4]
        ]
        entry = _exports(package)[:2] or (_public_defs(modules[0])[:2] if modules else [])
        facts += [
            {"kind": "entry_point", "text": f"names {item}", "match_any": [item]} for item in entry
        ]
        out["subsystems"][name] = {"path": f"{package_root}/{name}", "facts": facts}
    return out
