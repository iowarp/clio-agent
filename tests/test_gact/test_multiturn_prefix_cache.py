"""Multi-turn prompt stability for prefix-cache (KV-reuse) hits (#714).

The question this file answers: across a MULTI-TURN conversation, does an expert get
a byte-stable cacheable prompt PREFIX, so the LM server's prefix cache keeps hitting?

The decisive prefix-cache boundary is UPSTREAM of ARC. Known-verified facts (see
``tests/test_arc/test_live_plane_byte_equality.py``): within a turn the ARC render is
byte-for-byte the stock dspy ``_format_trajectory``; each ``forward()`` tombstones the
prior working-set so the per-turn ReAct trajectory is fresh (this MATCHES native dspy);
cross-turn history is prepended AS TEXT, identically with/without ARC. So the remaining
prefix-cache risk lives in two places, both tested here:

(a) the SYSTEM PROMPT (the dspy signature instructions = the literal system message,
    plus the assembled blueprint body + orchestrator-identity briefing + child
    descriptions that ride as the ``system_prompt`` input). If ANY per-turn volatile
    content leaks in (a timestamp, a uuid, a reordered dict/set, a session id), the
    cache dies from byte 0.

(b) the cacheable prefix being a true byte-PREFIX of the next turn's prompt.

The expert system prompt is assembled deterministically in
``_build_blueprint_dspy_module`` from:
  * ``agent_def.system_prompt`` (static blueprint body),
  * ``_runtime_active_workspace_context`` (workspace root + ordered allowed-roots tuple),
  * ``_runtime_dynamic_agent_children_context`` (orchestrator briefing; children sorted
    by ``(tier, id)``; static prose).
None of those sources read ``datetime.now`` / ``uuid`` / an unordered dict-or-set, so the
expectation is byte-stability. These tests pin that contract so a future edit that
injects volatile content fails loudly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from clio_agent.gact import context as ctx
from clio_agent.gact.agents.builders import (
    _blueprint_runtime_signature,
    _build_blueprint_dspy_module,
)
from clio_agent.gact.agents.composition import _runtime_dynamic_agent_children_context
from clio_agent.gact.runtime.globals import _gact_app_context
from clio_agent.gact.types import AgentDef


def _clear_app_caches(app: Any) -> None:
    """Clear THIS app's resolve-once expert caches (now per-app on ``app.state``,
    #770 Site 2) to force a genuine recompute rather than a cache echo."""
    for name in ("expert_children", "orchestrator_briefing"):
        store = getattr(app.state, name, None)
        if isinstance(store, dict):
            store.clear()


# ---- deterministic fixture graph -------------------------------------------- #

PARENT = AgentDef(
    id="main",
    source="expert_pack",
    title="Main Orchestrator",
    system_prompt="You coordinate the EarthScope workflow.",
    module={"kind": "react"},
)
CHILDREN = [
    AgentDef(
        id="geospatial",
        source="expert_pack",
        title="Geospatial",
        description="Resolves places to coordinates and finds nearby stations.",
        parent_id="main",
        tier=2,
        tools=["geo_geocode", "ndp_station_search"],
    ),
    AgentDef(
        id="analysis",
        source="expert_pack",
        title="Analysis",
        description="Analyses GNSS time series for selected stations.",
        parent_id="main",
        tier=2,
        tools=["ndp_timeseries"],
    ),
]


@pytest.fixture(autouse=True)
def _clear_prompt_caches() -> Iterator[None]:
    """The orchestrator-briefing / expert-children resolve-once caches are now
    PER-APP on ``app.state`` (#770 Site 2), so a fresh app per build is naturally
    isolated -- no cross-test process-global to scrub. Same-app builds within one
    test clear via ``_clear_app_caches`` where a genuine recompute is asserted."""
    yield


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the expert build deterministic and offline.

    Children resolve to the fixed ``CHILDREN`` rows (no disk/registry read), the LM /
    adapter / tools are stubbed (we never actually call a model -- we inspect the
    ASSEMBLED prompt), and the lm-config is a trivial namespace.
    """

    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    # Step 6: ``_dynamic_agent_lm_config`` returns a ``ResolvedLMSpec`` whose
    # ``materialize`` yields the runnable config; stub that contract here.
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(
            materialize=lambda cred_resolver=None: SimpleNamespace(
                provider="argonne", model="gpt-oss-120b", temperature=0.0
            )
        ),
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_tools",
        lambda base_agent, agent_def: [],
    )

    # Child rows resolve deterministically for BOTH the children-context render and the
    # next_expert Literal build. ``builders`` binds ``_runtime_child_agent_rows`` /
    # ``_runtime_declared_child_ids`` directly at module load, while ``composition``
    # reaches into the ``resolution`` namespace at call time -- so patch BOTH bind sites.
    def _children(app: Any, parent_id: str, session_id: str = "") -> list[AgentDef]:
        return list(CHILDREN) if parent_id == "main" else []

    monkeypatch.setattr("clio_agent.gact.agents.resolution._runtime_child_agent_rows", _children)
    monkeypatch.setattr("clio_agent.gact.agents.builders._runtime_child_agent_rows", _children)
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._runtime_declared_child_ids",
        lambda app, parent_id, session_id="": {row.id for row in _children(app, parent_id)},
    )
    # No active workspace -> the workspace block is empty (it is also stable when a
    # workspace IS bound, since the allowed-roots tuple is ordered, but keep this test
    # focused on the orchestrator-identity injection). composition reaches this through
    # the resolution namespace at call time, so the namespace patch suffices.
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_workspace_catalog_cwd",
        lambda app, session_id="", workspace_id="": None,
    )


def _build_expert_system_prompt(monkeypatch: pytest.MonkeyPatch, *, arc: Any) -> str:
    """Assemble the expert system prompt exactly as a turn would, optionally with an
    ARC bound on the app. Returns the ``system_prompt`` the module hands the model."""

    _patch_runtime(monkeypatch)
    # ``sessions`` mirrors real app wiring: the workflow-schema resolver looks the
    # session up (unknown id -> no blueprint to attribute -> GENERIC schema).
    app = SimpleNamespace(state=SimpleNamespace(arc=arc, sessions={}))
    sid = "sess-multiturn"
    sid_token = ctx.set_session_id(sid)
    try:
        with _gact_app_context(app):
            module = _build_blueprint_dspy_module(SimpleNamespace(), PARENT)
            return module.system_prompt
    finally:
        ctx.reset(sid_token)


# ---- (iii) SYSTEM-PROMPT byte-stability ------------------------------------- #


def test_expert_system_prompt_byte_stable_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME expert with the SAME children renders a byte-identical system prompt on
    turn 1 and turn 2. A datetime/uuid/reordered-dict leak would break this."""
    # Each build uses a FRESH app, so its per-app caches start empty -> turn2 is a
    # genuine recompute, not a cache echo (no shared process-global to scrub).
    turn1 = _build_expert_system_prompt(monkeypatch, arc=None)
    turn2 = _build_expert_system_prompt(monkeypatch, arc=None)
    assert turn1 == turn2
    # And it actually carries the orchestrator identity + child descriptions (so the
    # equality above is over the real, load-bearing prompt, not an empty string).
    assert "ORCHESTRATOR" in turn1
    assert "`geospatial`" in turn1 and "`analysis`" in turn1


def test_signature_instructions_byte_stable_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dspy signature *instructions* ARE the literal system message ChatAdapter
    emits. They must be byte-stable for the SAME expert+children across turns -- the
    ``next_expert`` Literal is built from ``sorted(child_ids)`` so its option order is
    deterministic, and the rest of the signature is static."""
    _patch_runtime(monkeypatch)
    app = SimpleNamespace(state=SimpleNamespace(arc=None, sessions={}))
    sid = "sess-multiturn"
    sid_token = ctx.set_session_id(sid)
    try:
        with _gact_app_context(app):
            sig1 = _blueprint_runtime_signature(PARENT)
            _clear_app_caches(app)
            sig2 = _blueprint_runtime_signature(PARENT)
    finally:
        ctx.reset(sid_token)
    assert sig1.instructions == sig2.instructions
    # next_expert Literal options are deterministically ordered (sorted children + finish)
    assert "next_expert" in sig1.output_fields
    next_expert_ann = str(sig1.output_fields["next_expert"].annotation)
    assert next_expert_ann == str(sig2.output_fields["next_expert"].annotation)


def test_orchestrator_briefing_byte_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The orchestrator-identity briefing (child-description injection) is byte-stable:
    children are sorted by (tier, id) and the prose is static -- no volatile content."""
    _patch_runtime(monkeypatch)
    app = SimpleNamespace(state=SimpleNamespace(arc=None))
    first = _runtime_dynamic_agent_children_context(app, PARENT, session_id="sess-multiturn")
    _clear_app_caches(app)
    second = _runtime_dynamic_agent_children_context(app, PARENT, session_id="sess-multiturn")
    assert first == second
    assert first  # non-empty: the parent HAS children, so a briefing was rendered


def test_orchestrator_briefing_child_order_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child rows rendered in (tier, id) order regardless of input order -- so a
    registry that returns children in a different order does NOT perturb the prompt."""
    _patch_runtime(monkeypatch)
    app = SimpleNamespace(state=SimpleNamespace(arc=None))
    sorted_order = _runtime_dynamic_agent_children_context(app, PARENT, session_id="sess-multiturn")
    _clear_app_caches(app)
    # Re-patch with the children REVERSED; the render must be identical.
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_child_agent_rows",
        lambda app, parent_id, session_id="": list(reversed(CHILDREN))
        if parent_id == "main"
        else [],
    )
    reversed_input = _runtime_dynamic_agent_children_context(
        app, PARENT, session_id="sess-multiturn"
    )
    assert sorted_order == reversed_input
    # `analysis` (id-sorted) precedes `geospatial` in the rendered briefing.
    assert sorted_order.index("`analysis`") < sorted_order.index("`geospatial`")


# ---- (i) ARC-vs-native multi-turn equivalence ------------------------------- #


def test_system_prompt_identical_arc_vs_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assembled expert system prompt is byte-identical whether or not an ARC is
    bound on the app -- the prompt assembly does not branch on ARC. (ARC governs the
    per-turn trajectory render, not the system prompt.)"""
    # Fresh app per build -> per-app caches start empty; no shared global to scrub.
    native = _build_expert_system_prompt(monkeypatch, arc=None)
    arc_backed = _build_expert_system_prompt(monkeypatch, arc=SimpleNamespace(name="fake-arc"))
    assert native == arc_backed


def test_system_prompt_identical_arc_vs_native_two_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-turn: run the expert build across two turns on BOTH the ARC path and the
    native (arc=None) path; every one of the four system prompts is identical. This is
    the cross-path AND cross-turn equivalence in one assertion."""
    prompts: list[str] = []
    for arc in (None, SimpleNamespace(name="fake-arc")):
        for _turn in range(2):
            # Fresh app per build (inside the helper) -> a genuine recompute each turn.
            prompts.append(_build_expert_system_prompt(monkeypatch, arc=arc))
    assert len(set(prompts)) == 1, "system prompt differs across turn/ARC-path"


# ---- (ii) cross-turn prefix stability (ties to KV reuse) -------------------- #


def test_system_message_is_a_byte_prefix_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cacheable prefix -- the literal system message (dspy signature instructions)
    PLUS the static assembled system_prompt the expert always emits first -- is a byte
    PREFIX of the next turn's. With a no-compaction, same-expert/same-children setup the
    two are in fact EQUAL, so the trivial prefix relation (equality => prefix) holds and
    the KV cache hits from byte 0 through the entire system region. The per-turn user
    content (the question + prepended transcript) appends AFTER this prefix."""
    _patch_runtime(monkeypatch)
    app = SimpleNamespace(state=SimpleNamespace(arc=None, sessions={}))
    sid_token = ctx.set_session_id("sess-multiturn")
    try:
        with _gact_app_context(app):
            module1 = _build_blueprint_dspy_module(SimpleNamespace(), PARENT)
            sys_msg_1 = module1.program.signature.instructions
            prefix_1 = sys_msg_1 + "\n\n" + module1.system_prompt
            _clear_app_caches(app)
            module2 = _build_blueprint_dspy_module(SimpleNamespace(), PARENT)
            sys_msg_2 = module2.program.signature.instructions
            prefix_2 = sys_msg_2 + "\n\n" + module2.system_prompt
    finally:
        ctx.reset(sid_token)
    assert prefix_2.startswith(prefix_1)
    assert prefix_1 == prefix_2  # no-compaction same-expert: exact, not just prefix
