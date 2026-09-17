"""Catalog-aware A2UI producer tools (S4).

Replaces ``gact/a2ui_tools.py`` (deleted whole — its 78-line docstring wall
restated every component's props as prose handcuffs, the pattern
``.claude/CLAUDE.md`` ⚑3 forbids, and hardcoded the builtin workspace catalog
id). Guidance now lives in the catalog itself, disclosed as a skill
(:mod:`clio_agent.gact.a2ui_catalogs.skills`); these four tools are thin,
catalog-agnostic verbs over the transcript-owned surface store
(:mod:`clio_agent.gact.a2ui_store`), and every producer mistake comes back as
a typed refusal dict (:mod:`clio_agent.gact.a2ui_producer._refusal`), never
an exception.
"""

from __future__ import annotations

from clio_agent.gact.a2ui_producer.create import build_create_a2ui_surface_tool
from clio_agent.gact.a2ui_producer.delete import build_delete_a2ui_surface_tool
from clio_agent.gact.a2ui_producer.update_components import build_update_a2ui_components_tool
from clio_agent.gact.a2ui_producer.update_data_model import build_update_a2ui_data_model_tool

__all__ = [
    "build_create_a2ui_surface_tool",
    "build_delete_a2ui_surface_tool",
    "build_update_a2ui_components_tool",
    "build_update_a2ui_data_model_tool",
]
