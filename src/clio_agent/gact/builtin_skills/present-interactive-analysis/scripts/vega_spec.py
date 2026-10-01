#!/usr/bin/env python3
"""Export an Altair chart to a Vega-Lite spec and pre-check it for clio.chart.v1.

The pre-check in :func:`check_spec` calls the real server-side guard
(:func:`clio_schemas.a2ui.chart_spec.check_chart_spec`) instead of a hand-copied
rule list, so this script can never drift from what the server actually
enforces — a spec this script accepts is a spec the guard accepts, including
the data-free layout keys (``columns``, ``spacing``, ``padding``, ``align``,
``bounds``, ``center``) and ``projection`` it allows as of clio-schemas 0.5.2.
The server-side guard is still authoritative; this is a pre-check to save a
round trip, not a second source of truth.
"""

from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path
from typing import Any

from clio_schemas.a2ui.chart_spec import check_chart_spec


def check_spec(spec: Any) -> list[str]:
    """Return human-readable problems that would get ``spec`` refused.

    Delegates to the real guard for every structural rule (allowed top-level
    keys, the ``data: {"name": "source"}`` requirement, forbidden keys, size
    and depth limits). The one addition on top is Altair-specific: a
    top-level ``datasets`` key is what Altair emits when a chart is built on
    a DataFrame or URL instead of ``alt.NamedData("source")``, so it gets a
    tool-specific explanation alongside the guard's own verdict.
    """

    if not isinstance(spec, dict):
        return ["the spec must be a JSON object"]
    problems: list[str] = []
    if "datasets" in spec:
        problems.append(
            "top-level 'datasets' present: build the chart on alt.NamedData('source') "
            "instead of passing a DataFrame"
        )
    problems.extend(str(violation) for violation in check_chart_spec(spec))
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
