"""Step 5 (#770 unified-concurrency §4 Site 2): the expert caches are per-app.

Two failing-first regressions proving the resolve-once expert caches no longer
live in process-global dicts (``_EXPERT_CHILDREN_CACHE`` /
``_ORCHESTRATOR_BRIEFING_CACHE``) that let one app's value leak into another's
build (first/last-writer-wins), but on the live turn's ``app.state`` via
``per_app_dict``.

1. **Children cache is per-app.** Build the SAME expert id under app A (live
   children ``{x, y}``) then app B (live children ``{z}``); each app's
   ``next_expert`` Literal is its OWN app's, never the sibling's. The
   discriminating case: an app-less consume (no live turn) must collapse to a
   deterministic ``Literal["finish"]`` — a structured empty — NOT leak the
   last-writer app B's ``{z}`` through a process-global cache.

2. **Orchestrator-briefing cache is per-app.** App A (session-bearing) renders a
   briefing; app B (a session-less rebuild) must render ``""``, not inherit app
   A's cached briefing.
"""

from __future__ import annotations

import typing
from types import SimpleNamespace

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import builders, composition, resolution


class _Orch:
    """Minimal AgentDef stand-in whose declared children drive next_expert."""

    id = "orch"
    module = {"kind": "predict"}
    signature = {
        "inputs": {"question": {"type": "string"}},
        "outputs": {"answer": {"type": "string"}},
    }
    structured_outputs = {"workflow_state": True}


def _route_values(sig: object) -> set[str]:
    annotation = sig.output_fields["next_expert"].annotation  # type: ignore[attr-defined]
    return set(typing.get_args(annotation))


def test_expert_children_cache_is_per_app(monkeypatch) -> None:
    # ``sessions`` mirrors real app wiring: signature build resolves the session's
    # workflow-state schema (unknown id -> no blueprint to attribute -> GENERIC).
    app_a = SimpleNamespace(state=SimpleNamespace(sessions={}))
    app_b = SimpleNamespace(state=SimpleNamespace(sessions={}))

    children = {id(app_a): {"x", "y"}, id(app_b): {"z"}}
    monkeypatch.setattr(
        builders,
        "_runtime_declared_child_ids",
        lambda app, parent_id, *, session_id="": set(children.get(id(app), set())),
    )

    def _build_under(app: object, sid: str) -> object:
        tok = _ctx.set_app(app)
        _ctx.set_session_id(sid)
        try:
            return builders._blueprint_runtime_signature(_Orch())
        finally:
            _ctx.reset(tok)

    sig_a = _build_under(app_a, "sessA")
    assert _route_values(sig_a) == {"x", "y", "finish"}

    sig_b = _build_under(app_b, "sessB")
    assert _route_values(sig_b) == {"z", "finish"}

    # App-less consume (no active app): must collapse to a deterministic finish,
    # never leak the last-writer app B's {z} through a process-global cache.
    sig_none = builders._blueprint_runtime_signature(_Orch())
    assert _route_values(sig_none) == {"finish"}


def test_orchestrator_briefing_cache_is_per_app(monkeypatch) -> None:
    app_a = SimpleNamespace(state=SimpleNamespace())
    app_b = SimpleNamespace(state=SimpleNamespace())

    child = SimpleNamespace(
        tier=1, id="worker", description="does the grounded work", title="Worker", tools=["fs"]
    )
    # Only app A (session-bearing) resolves rows; app B is a session-less rebuild.
    monkeypatch.setattr(
        resolution,
        "_runtime_child_agent_rows",
        lambda app, parent_id, *, session_id="": [child] if app is app_a else [],
    )

    agent_def = SimpleNamespace(
        id="orch", source="expert_pack", description="Orchestrator", title="Orch", tools=[]
    )

    briefing_a = composition._runtime_dynamic_agent_children_context(
        app_a, agent_def, session_id="sessA"
    )
    assert "ORCHESTRATOR" in briefing_a
    assert "worker" in briefing_a

    # App B: session-less rebuild (no rows). Must NOT inherit app A's cached
    # briefing via a process-global cache -> a structured empty ("").
    briefing_b = composition._runtime_dynamic_agent_children_context(
        app_b, agent_def, session_id=""
    )
    assert briefing_b == ""
