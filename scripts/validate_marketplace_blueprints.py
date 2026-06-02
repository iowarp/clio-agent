#!/usr/bin/env python3
"""Validate Agent Blueprint marketplace sources before real-provider benchmarks."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio_agent.gact.agent_blueprints import validate_agent_blueprint_path


@dataclass(frozen=True)
class MarketplaceValidationOptions:
    """Static marketplace hierarchy requirements."""

    complex_min_experts: int = 3
    complex_min_edges: int = 2
    complex_min_levels: int = 3
    require_complex_count: int = 0
    exclude_complex_ids: tuple[str, ...] = ()


def _candidate_blueprint_roots(source: Path) -> list[Path]:
    """Return Agent Blueprint roots from a marketplace source directory."""

    source = source.expanduser().resolve()
    if (source / "AGENT.md").exists():
        return [source]
    return [
        path
        for path in sorted(source.iterdir())
        if path.is_dir() and (path / "AGENT.md").exists()
    ]


def _hierarchy_metrics(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Return static hierarchy metrics for validated Agent Blueprint agents."""

    real_agents = [
        row
        for row in agents
        if not str(row.get("id") or "").endswith(".manifest")
    ]
    ids = {str(row.get("id") or "") for row in real_agents if row.get("id")}
    children_by_parent: dict[str, list[str]] = {agent_id: [] for agent_id in ids}
    roots: list[str] = []
    edges = 0
    for row in real_agents:
        agent_id = str(row.get("id") or "")
        parent_id = str(row.get("parent_id") or "")
        if not agent_id:
            continue
        if parent_id and parent_id in ids:
            children_by_parent.setdefault(parent_id, []).append(agent_id)
            edges += 1
        else:
            roots.append(agent_id)

    def depth_from(agent_id: str, seen: set[str]) -> int:
        if agent_id in seen:
            return 0
        children = children_by_parent.get(agent_id) or []
        if not children:
            return 1
        next_seen = {*seen, agent_id}
        return 1 + max(depth_from(child, next_seen) for child in children)

    max_levels = max((depth_from(root, set()) for root in roots), default=0)
    leaf_count = sum(1 for agent_id in ids if not children_by_parent.get(agent_id))
    branching_parent_count = sum(
        1 for children in children_by_parent.values() if len(children) >= 2
    )
    tool_names = sorted(
        {
            str(tool)
            for row in real_agents
            for tool in (row.get("tools") or [])
            if str(tool).strip()
        }
    )
    skill_names = sorted(
        {
            str(skill)
            for row in real_agents
            for skill in (row.get("skills") or [])
            if str(skill).strip()
        }
    )
    command_names = sorted(
        {
            str(command)
            for row in real_agents
            for command in (row.get("commands") or [])
            if str(command).strip()
        }
    )
    return {
        "expert_count": len(real_agents),
        "root_count": len(roots),
        "root_agents": sorted(roots),
        "edge_count": edges,
        "max_levels": max_levels,
        "leaf_count": leaf_count,
        "branching_parent_count": branching_parent_count,
        "tool_count": len(tool_names),
        "tools": tool_names,
        "skill_count": len(skill_names),
        "skills": skill_names,
        "command_count": len(command_names),
        "commands": command_names,
    }


def _is_complex_pack(
    row: dict[str, Any],
    *,
    options: MarketplaceValidationOptions,
) -> bool:
    """Return whether one static pack summary meets complex hierarchy thresholds."""

    metrics = row["metrics"]
    return (
        row["enabled"]
        and row["id"] not in set(options.exclude_complex_ids)
        and metrics["expert_count"] >= options.complex_min_experts
        and metrics["edge_count"] >= options.complex_min_edges
        and metrics["max_levels"] >= options.complex_min_levels
    )


def validate_marketplace_source(
    source: Path,
    *,
    options: MarketplaceValidationOptions | None = None,
) -> dict[str, Any]:
    """Validate all Agent Blueprints under a local marketplace source."""

    options = options or MarketplaceValidationOptions()
    if not source.exists() or not source.is_dir():
        return {
            "source": str(source),
            "ok": False,
            "validation_errors": [f"marketplace source is not a directory: {source}"],
            "blueprints": [],
            "complex_blueprints": [],
            "requirements": options.__dict__,
        }
    blueprints: list[dict[str, Any]] = []
    errors: list[str] = []
    for root in _candidate_blueprint_roots(source):
        validation = validate_agent_blueprint_path(root, scope="marketplace")
        blueprint = validation.get("agent_blueprint") or {}
        blueprint_id = str(blueprint.get("id") or root.name)
        validation_errors = [
            str(error) for error in validation.get("validation_errors", []) or []
        ]
        validation_warnings = [
            str(warning) for warning in validation.get("validation_warnings", []) or []
        ]
        if validation_errors:
            errors.extend(f"{blueprint_id}: {error}" for error in validation_errors)
        row = {
            "id": blueprint_id,
            "title": str(blueprint.get("title") or blueprint_id),
            "root": str(root),
            "enabled": bool(validation.get("enabled")) and not validation_errors,
            "validation_errors": validation_errors,
            "validation_warnings": validation_warnings,
            "mcp_descriptor_count": len(validation.get("mcp_descriptors", []) or []),
            "hook_descriptor_count": len(validation.get("hook_descriptors", []) or []),
            "metrics": _hierarchy_metrics(list(validation.get("agents", []) or [])),
        }
        row["complex"] = _is_complex_pack(row, options=options)
        blueprints.append(row)

    complex_blueprints = [row["id"] for row in blueprints if row["complex"]]
    if options.require_complex_count and len(complex_blueprints) < options.require_complex_count:
        errors.append(
            "complex blueprint count below requirement: "
            f"{len(complex_blueprints)}/{options.require_complex_count}"
        )

    return {
        "source": str(source.expanduser().resolve()),
        "ok": not errors,
        "validation_errors": errors,
        "blueprint_count": len(blueprints),
        "complex_blueprint_count": len(complex_blueprints),
        "complex_blueprints": complex_blueprints,
        "requirements": {
            "complex_min_experts": options.complex_min_experts,
            "complex_min_edges": options.complex_min_edges,
            "complex_min_levels": options.complex_min_levels,
            "require_complex_count": options.require_complex_count,
            "exclude_complex_ids": list(options.exclude_complex_ids),
        },
        "blueprints": blueprints,
    }


def _render_text_report(result: dict[str, Any]) -> str:
    """Render a compact human-readable marketplace validation report."""

    requirements = result["requirements"]
    lines = [
        "# CLIO Agent Blueprint Marketplace Preflight",
        "",
        f"Source: `{result['source']}`",
        f"Blueprints: {result['blueprint_count']}",
        (
            "Complex blueprints: "
            f"{result['complex_blueprint_count']}"
            + (
                f" / required {requirements['require_complex_count']}"
                if requirements["require_complex_count"]
                else ""
            )
        ),
        f"Status: {'pass' if result['ok'] else 'fail'}",
        "",
        "| Blueprint | Enabled | Complex | Experts | Edges | Levels | Tools | MCP | Errors |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in result["blueprints"]:
        metrics = row["metrics"]
        lines.append(
            "| "
            + " | ".join(
                [
                    row["id"],
                    "yes" if row["enabled"] else "no",
                    "yes" if row["complex"] else "no",
                    str(metrics["expert_count"]),
                    str(metrics["edge_count"]),
                    str(metrics["max_levels"]),
                    str(metrics["tool_count"]),
                    str(row["mcp_descriptor_count"]),
                    "; ".join(row["validation_errors"]) or "-",
                ]
            )
            + " |"
        )
    if result["validation_errors"]:
        lines.extend(["", "## Blocking Gaps", ""])
        lines.extend(f"- {error}" for error in result["validation_errors"])
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Local marketplace repo or one blueprint root")
    parser.add_argument("--json", action="store_true", help="Write JSON instead of Markdown")
    parser.add_argument("--output", type=Path, help="Optional report output path")
    parser.add_argument("--complex-min-experts", type=int, default=3)
    parser.add_argument("--complex-min-edges", type=int, default=2)
    parser.add_argument("--complex-min-levels", type=int, default=3)
    parser.add_argument("--require-complex-count", type=int, default=0)
    parser.add_argument(
        "--exclude-complex-id",
        action="append",
        default=[],
        help="Blueprint id to exclude from the complex-count requirement",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    options = MarketplaceValidationOptions(
        complex_min_experts=args.complex_min_experts,
        complex_min_edges=args.complex_min_edges,
        complex_min_levels=args.complex_min_levels,
        require_complex_count=args.require_complex_count,
        exclude_complex_ids=tuple(args.exclude_complex_id or ()),
    )
    result = validate_marketplace_source(args.source, options=options)
    rendered = (
        json.dumps(result, indent=2, sort_keys=True)
        if args.json
        else _render_text_report(result)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report = rendered + ("" if rendered.endswith("\n") else "\n")
        args.output.write_text(report, encoding="utf-8")
    else:
        print(rendered)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
