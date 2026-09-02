"""Auto-attached react-expert tool set (#969 / #1066 / #1067).

Every dynamic react expert built in :mod:`clio_agent.gact.agents.builders` gets the same small
set of infrastructure tools — attached automatically, NOT counted against the 5-7 curated
domain-tool budget (RULE 5), the same way ``load_skill`` and the child-delegation tools are:

* ``create_artifact`` (#969) — designate a session output as a provenance-tracked artifact;
* ``plan_exit`` (#1066) — the plan-mode turn-ending yield that hands a plan back for approval;
* ``write_todos`` (#1067) — the execution-phase checklist (whole-list replacement);
* ``cron_create`` / ``cron_list`` / ``cron_delete`` (#1081) — the model-callable scheduling
  triad (arm a recurring/one-shot future turn for this session; read back; cancel-both);
* ``loop_wakeup`` (#1079) — the autonomous-loop self-pace control (reschedule the loop
  after a delay, or stop; typed bounds + a bounded fallback enforce no-runaway);
* ``goal_status`` (#1080) — the READ-ONLY goal readback (condition / iters / budget /
  deterministic-gate met?). There is deliberately NO ``set_goal`` / ``goal_clear`` tool: a
  goal is armed only by the user (/goal) or a declared skill-effect, never by the model
  (a self-armed halt is the self-grading anti-pattern, ⚑ RULE 1).
* ``raise_alert_card`` (spotter-ai follow-on) — a GENERIC way for any spawned child agent
  to raise a notification/action card into its PARENT session's transcript. Auto-attached
  (not spotter-specific) so a spawned child never has to remember to declare it just to
  notify its parent; spotter-ai's watcher is simply the first caller.

``refresh_provider_models`` (#1211 review R6/S2) is DELIBERATELY NOT in the universal
list above: probing claude_code is a REAL, BILLED API call per alias (up to 5 per
refresh), so handing it to every spawned Tier-2/3 child by default would let an
unrelated child rack up billed calls it never asked for. It is attached ONLY on
tier-1 MAIN sessions (``agent_def.parent_id`` empty — see :func:`build_auto_react_tools`)
— the same root check :func:`clio_agent.gact.agents.skill_runtime.effective_declared_skills`
uses to auto-declare the ``/update-models`` skill itself, so the tool is present exactly
where the skill's own metadata block is. The skill-effects system
(:mod:`clio_agent.gact.agents.skill_effects`) has no "attach a tool on skill load"
primitive today; if one is added later, migrating this attachment to it (skill-scoped
rather than tier-scoped) is the natural next step.

Collecting them behind one seam keeps ``builders.py`` from re-listing the set (and re-importing
each builder) at its two attach sites, so a fourth auto-tool lands here, not by growing the
god-file (no-accretion ground rule).
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_tools import build_create_a2ui_surface_tool
from clio_agent.gact.action_cards import build_raise_alert_card_tool
from clio_agent.gact.artifacts.proposals import build_create_artifact_tool
from clio_agent.gact.autonomous_loop import build_loop_wakeup_tool
from clio_agent.gact.cron_tools import build_cron_tools
from clio_agent.gact.goal import build_goal_status_tool
from clio_agent.gact.plan_mode import build_plan_exit_tool
from clio_agent.gact.resource_tools import build_resource_tools
from clio_agent.gact.todos import build_write_todos_tool
from clio_agent.providers.model_discovery import build_refresh_provider_models_tool


def build_auto_react_tools(agent_def: Any) -> list[Any]:
    """Return the auto-attached tool list for one react expert (order-stable).

    Order is fixed so the react prompt's tool prefix stays byte-stable across builds — a
    provider prompt-cache and the transcript-tap dedup keys both key off a stable tool order.
    ``refresh_provider_models`` is appended ONLY for a tier-1 MAIN session (no
    ``parent_id`` — never a spawned Tier-2/3 child): see the module docstring for why a
    billed action is scoped this way instead of joining the universal list above.
    """

    tools = [
        build_create_artifact_tool(agent_def),
        build_plan_exit_tool(agent_def),
        build_write_todos_tool(agent_def),
        *build_cron_tools(),
        build_loop_wakeup_tool(),
        build_goal_status_tool(),
        build_raise_alert_card_tool(agent_def),
        # The bounded read surface for uploaded workspace resources. Universal,
        # not tier-1 only: the enrichment block that announces an attachment
        # names these tools, and a spawned child working the attachment needs
        # the same doors. They read only, and read only within the session's own
        # workspace, so there is no billed or destructive action to scope.
        *build_resource_tools(agent_def),
    ]
    if not (getattr(agent_def, "parent_id", "") or ""):
        tools.append(build_create_a2ui_surface_tool())
        tools.append(build_refresh_provider_models_tool())
    return tools
