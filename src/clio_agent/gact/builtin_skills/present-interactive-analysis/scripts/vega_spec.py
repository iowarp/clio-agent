#!/usr/bin/env python3
"""Export an Altair chart to a Vega-Lite spec and pre-check it for clio.chart.v1.

The server-side chart guard is authoritative; these checks only catch the
refusals Altair users hit most often (inlined data, URLs, oversize specs) before
a round trip.
"""

from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path
from typing import Any

MAX_SPEC_BYTES = 64 * 1024
SOURCE_DATA = {"name": "source"}
FORBIDDEN_DATA_KEYS = ("url", "values", "sequence")


def _walk(node: Any, path: str) -> list[tuple[str, Any, Any]]:
    """Return (path, key, value) for every mapping entry in ``node``."""

    found: list[tuple[str, Any, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}/{key}"
            found.append((child, key, value))
            found.extend(_walk(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk(value, f"{path}/{index}"))
    return found


def check_spec(spec: Any) -> list[str]:
    """Return human-readable problems that would get ``spec`` refused."""

    if not isinstance(spec, dict):
        return ["the spec must be a JSON object"]
    problems: list[str] = []
    if "datasets" in spec:
        problems.append(
            "top-level 'datasets' present: build the chart on alt.NamedData('source') "
            "instead of passing a DataFrame"
        )
    for path, key, value in _walk(spec, ""):
        if key == "data" and value != SOURCE_DATA:
            if isinstance(value, dict) and any(k in value for k in FORBIDDEN_DATA_KEYS):
                problems.append(
                    f"{path}: data must be {{'name': 'source'}}, not inline or URL data"
                )
            else:
                problems.append(f"{path}: data must be exactly {{'name': 'source'}}")
        elif key == "url":
            problems.append(f"{path}: URLs are not allowed anywhere in a chart spec")
        elif key == "usermeta":
            problems.append(f"{path}: 'usermeta' is not allowed")
    size = len(json.dumps(spec, separators=(",", ":")).encode("utf-8"))
    if size > MAX_SPEC_BYTES:
        problems.append(f"spec is {size} bytes; the limit is {MAX_SPEC_BYTES}")
    return problems


def build(chart_file: Path, out_file: Path) -> int:
    """Run ``chart_file``, export its ``chart`` variable, write and check the spec."""

    namespace = runpy.run_path(str(chart_file), run_name="__chart__")
    chart = namespace.get("chart")
    if chart is None or not hasattr(chart, "to_dict"):
        print(f"error: {chart_file} must assign an Altair chart to a variable named 'chart'")
        return 2
    spec = chart.to_dict()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return _report(spec, out_file)


def check(spec_file: Path) -> int:
    """Check an existing spec file."""

    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    return _report(spec, spec_file)


def _report(spec: Any, where: Path) -> int:
    problems = check_spec(spec)
    if problems:
        print(f"{where}: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"{where}: OK (pre-check passed; the server guard is authoritative)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build", help="export an Altair chart file to a spec")
    build_cmd.add_argument("chart_file", type=Path)
    build_cmd.add_argument("out_file", type=Path)
    check_cmd = sub.add_parser("check", help="pre-check an existing spec JSON file")
    check_cmd.add_argument("spec_file", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        return build(args.chart_file, args.out_file)
    return check(args.spec_file)


if __name__ == "__main__":
    sys.exit(main())
