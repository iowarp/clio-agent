"""A small time-varying VTK dataset, written whole or one step at a time.

The dataset is a 12x12x12 image (VTK XML ``.vti``) with two point fields:
``pressure``, a spherical wave that travels outward, and ``temperature``, a slow
gradient that rotates. A ``.pvd`` collection names the steps and their times, so
any VTK reader sees one dataset with time steps.

    python wave_series.py write DIR [--steps 6]
        writes every step and the collection at once (the committed fixture
        under ``wave/`` is this, with the defaults).

    python wave_series.py grow DIR [--steps 30] [--interval 5]
        writes one step every ``interval`` seconds and rewrites the collection
        after each, as a simulation that is still running would (plan PV9, AQ46).
        Each file is written to a temporary name and renamed into place, so a
        reader never sees half a step.

Standard library only, so it runs under any Python, including ParaView's.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path

SIZE = 12
SPACING = 1.0 / (SIZE - 1)
DT = 0.1


def _fields(step: int) -> tuple[list[float], list[float]]:
    t = step * DT
    pressure: list[float] = []
    temperature: list[float] = []
    angle = 0.6 * t
    for k in range(SIZE):
        z = k * SPACING - 0.5
        for j in range(SIZE):
            y = j * SPACING - 0.5
            for i in range(SIZE):
                x = i * SPACING - 0.5
                r = math.sqrt(x * x + y * y + z * z)
                pressure.append(math.cos(12.0 * r - 6.0 * t) * math.exp(-2.0 * r))
                temperature.append(300.0 + 50.0 * (x * math.cos(angle) + y * math.sin(angle)))
    return pressure, temperature


def _array(name: str, values: list[float]) -> str:
    rows = []
    for start in range(0, len(values), 8):
        rows.append(" ".join(f"{v:.4g}" for v in values[start : start + 8]))
    body = "\n".join(rows)
    return f'<DataArray type="Float32" Name="{name}" format="ascii">\n{body}\n</DataArray>'


def step_xml(step: int) -> str:
    """One step as VTK XML ImageData."""

    pressure, temperature = _fields(step)
    extent = f"0 {SIZE - 1} 0 {SIZE - 1} 0 {SIZE - 1}"
    return (
        '<?xml version="1.0"?>\n'
        '<VTKFile type="ImageData" version="1.0" byte_order="LittleEndian">\n'
        f'<ImageData WholeExtent="{extent}" Origin="0 0 0" '
        f'Spacing="{SPACING:.6g} {SPACING:.6g} {SPACING:.6g}">\n'
        f'<Piece Extent="{extent}">\n<PointData Scalars="pressure">\n'
        f"{_array('pressure', pressure)}\n{_array('temperature', temperature)}\n"
        "</PointData>\n</Piece>\n</ImageData>\n</VTKFile>\n"
    )


def collection_xml(steps: int) -> str:
    """The ``.pvd`` collection naming steps ``0 .. steps-1``."""

    rows = "\n".join(
        f'<DataSet timestep="{s * DT:.6g}" part="0" file="wave_{s:04d}.vti"/>' for s in range(steps)
    )
    return (
        '<?xml version="1.0"?>\n<VTKFile type="Collection" version="0.1">\n'
        f"<Collection>\n{rows}\n</Collection>\n</VTKFile>\n"
    )


def _write(path: Path, text: str) -> None:
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def write(out: Path, steps: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for step in range(steps):
        _write(out / f"wave_{step:04d}.vti", step_xml(step))
    _write(out / "wave.pvd", collection_xml(steps))


def grow(out: Path, steps: int, interval: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for step in range(steps):
        _write(out / f"wave_{step:04d}.vti", step_xml(step))
        _write(out / "wave.pvd", collection_xml(step + 1))
        print(f"step {step} written (t={step * DT:.6g})", flush=True)
        if step + 1 < steps:
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    w = sub.add_parser("write", help="write every step at once")
    w.add_argument("out", type=Path)
    w.add_argument("--steps", type=int, default=6)
    g = sub.add_parser("grow", help="write one step at a time")
    g.add_argument("out", type=Path)
    g.add_argument("--steps", type=int, default=30)
    g.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.mode == "write":
        write(args.out, args.steps)
    else:
        grow(args.out, args.steps, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
