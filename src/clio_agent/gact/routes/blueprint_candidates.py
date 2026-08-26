"""Marketplace blueprint candidate discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root


def agent_blueprint_candidates(root: Path) -> list[dict[str, Any]]:
    """Enumerate installable blueprint roots discovered under ``root``."""

    candidates: list[Path] = []
    if (root / "AGENT.md").exists():
        candidates.append(root)
    if root.is_dir():
        candidates.extend(sorted(path for path in root.iterdir() if (path / "AGENT.md").exists()))
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        parsed = parse_agent_blueprint_root(candidate, scope="marketplace")
        parsed_wire = parsed.to_wire()
        rows.append(
            {
                "id": parsed.id,
                "title": parsed.title,
                "version": parsed.version,
                "enabled": parsed.enabled,
                "validation_errors": list(parsed.validation_errors),
                "definition_path": str(parsed.root_path),
                "kind": parsed_wire["kind"],
            }
        )
    return rows
