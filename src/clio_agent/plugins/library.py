"""The clio-coder library dialect of the ``ai.iowarp.clio`` extension.

clio-coder's library (federated into clio-kit) publishes Agent Plugins 1.0.0
packages whose ``ai.iowarp.clio`` object is a component manifest::

    {"manifestVersion": 1, "kind": "skill",
     "resources": {"skills": "."},
     "components": [{"kind": "skill", "id": "archify", "path": "SKILL.md"}]}

The loader maps it into the same plugin record as a native package: each
``skill`` component becomes a skill (its id is the skill name, its path the
``SKILL.md``). clio-coder's other component kinds (agents, prompts, fleets,
resources, scripts) have no CLIO counterpart; each is reported as a typed
``library_component_unmapped`` warning, never dropped silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.plugins.frontmatter import FrontmatterError, split_frontmatter
from clio_agent.plugins.manifest import within
from clio_agent.plugins.records import Diagnostic, SkillRecord

#: The keys that mark the dialect (CLIO's own extension has none of them).
LIBRARY_FIELDS = frozenset({"manifestVersion", "resources", "components", "compatibility", "evals"})


def is_library_manifest(ext: dict[str, Any]) -> bool:
    return "manifestVersion" in ext


def library_skills(
    root: Path, plugin: str, ext: dict[str, Any]
) -> tuple[list[SkillRecord], list[Diagnostic]]:
    """Map a library manifest's components onto skills, with typed reasons for the rest."""

    skills: list[SkillRecord] = []
    diagnostics: list[Diagnostic] = []
    unmapped: dict[str, list[str]] = {}
    components = ext.get("components")
    if not isinstance(components, list):
        return [], [
            Diagnostic("error", "library_manifest_invalid", "components must be a list", plugin)
        ]
    for component in components:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("kind") or "")
        component_id = str(component.get("id") or "")
        if kind != "skill":
            unmapped.setdefault(kind or "unknown", []).append(component_id)
            continue
        skill_file = (root / str(component.get("path") or "")).resolve()
        if not within(root, skill_file) or not skill_file.is_file():
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "skill_invalid",
                    f"skill {component_id}: {component.get('path')!r} is not a file in the plugin",
                    plugin,
                )
            )
            continue
        try:
            meta, _ = split_frontmatter(skill_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, FrontmatterError) as exc:
            diagnostics.append(
                Diagnostic("warning", "skill_invalid", f"skill {component_id}: {exc}", plugin)
            )
            continue
        meta = meta or {}
        name = str(meta.get("name") or "")
        description = str(meta.get("description") or "").strip()
        if name != component_id or not description:
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "skill_invalid",
                    f"skill {component_id}: SKILL.md must name it and describe it",
                    plugin,
                )
            )
            continue
        skills.append(SkillRecord(id=name, description=description, path=str(skill_file)))
    for kind, ids in sorted(unmapped.items()):
        diagnostics.append(
            Diagnostic(
                "warning",
                "library_component_unmapped",
                f"clio-coder {kind} components have no CLIO counterpart: {', '.join(ids)}",
                plugin,
            )
        )
    return skills, diagnostics
