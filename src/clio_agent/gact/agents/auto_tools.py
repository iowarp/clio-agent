"""Auto-attached react-expert tool set (#969 / #1066 / #1067).

Every dynamic react expert built in :mod:`clio_agent.gact.agents.builders` gets the same small
set of infrastructure tools — attached automatically, NOT counted against the 5-7 curated
domain-tool budget (RULE 5), the same way ``load_skill`` and the child-delegation tools are:

* ``create_artifact`` (#969) — designate a session output as a provenance-tracked artifact;
* ``plan_exit`` (#1066) — the plan-mode turn-ending yield that hands a plan back for approval;
* ``write_todos`` (#1067) — the execution-phase checklist (whole-list replacement).

Collecting them behind one seam keeps ``builders.py`` from re-listing the set (and re-importing
each builder) at its two attach sites, so a fourth auto-tool lands here, not by growing the
god-file (no-accretion ground rule).
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.artifacts.proposals import build_create_artifact_tool
from clio_agent.gact.plan_mode import build_plan_exit_tool
from clio_agent.gact.todos import build_write_todos_tool


def build_auto_react_tools(agent_def: Any) -> list[Any]:
    """Return the auto-attached tool list for one react expert (order-stable).

    Order is fixed so the react prompt's tool prefix stays byte-stable across builds — a
    provider prompt-cache and the transcript-tap dedup keys both key off a stable tool order.
    """

    return [
        build_create_artifact_tool(agent_def),
        build_plan_exit_tool(agent_def),
        build_write_todos_tool(agent_def),
    ]
