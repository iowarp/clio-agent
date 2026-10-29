"""Multi-turn prompt stability for prefix-cache (KV-reuse) hits (#714).

The question this file answers: across a MULTI-TURN conversation, does an agent get
a byte-stable cacheable prompt PREFIX, so the LM server's prefix cache keeps hitting?

The messages are append-only across turns: the agent's context is its ARC plane, which
spans turns, and each request is a prefix of the next unless a recorded op landed
(pinned by ``tests/test_gact/test_context_projection.py``). So the remaining
prefix-cache risk lives in two places, both tested here:

(a) the SYSTEM PROMPT (the dspy signature instructions = the literal system message,
    plus the composed plugin prompt that rides as the ``system_prompt`` input) and the
    tool catalog the model sees (the spawn tools list the agent's defined subagents).
    If ANY per-turn volatile content leaks in (a timestamp, a uuid, a reordered
    dict/set, a session id), the cache dies from byte 0.

(b) the cacheable prefix being a true byte-PREFIX of the next turn's prompt.

The agent system prompt is assembled deterministically in ``build_agent_module`` from:
  * ``resolution.prompt_text`` (the composed prompt: identity, ``core`` guidelines,
    rendering, plugin capability and domain fragments, in a fixed section order),
  * ``_runtime_active_workspace_context`` (workspace root + ordered allowed-roots tuple),
  * the skill metadata block of the resolved plugins.
None of those sources read ``datetime.now`` / ``uuid`` / an unordered dict-or-set, so the
expectation is byte-stability. These tests pin that contract so a future edit that
injects volatile content fails loudly.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.task_types import SubmissionContext
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import builders
from clio_agent.gact.agents.resolution import prepare_session_agent, resolution_agent_def
from clio_agent.gact.builtin_boundary import BuiltinBoundary
from clio_agent.gact.runtime.globals import _gact_app_context
from clio_agent.gact.sessions import SessionStore
from clio_agent.plugins.resolve import Resolution, delegable_subagents
from clio_agent.plugins.service import PluginService
from tests._plugin_fixtures import custom_marketplace, fragment

AGENT = """
from clio_agent.sdk import Agent


class EarthscopeMain(Agent):
    id = "earthscope-main"
    version = "1.0.0"
    name = "EarthScope"
    role = "a GNSS workflow coordinator"
    description = "Coordinates the EarthScope workflow."
    plugins = Agent.plugins + ["delegation", "earthscope"]
"""

SUBAGENTS = {
    "geospatial": "Resolves places to coordinates and finds nearby stations.",
    "analysis": "Analyses GNSS time series for selected stations.",
}


def _subagent(subagent_id: str, description: str) -> str:
    return (
        "from clio_agent.sdk import Subagent\n\n"
        f"{subagent_id} = Subagent({subagent_id!r}, prompt='Do the {subagent_id} work.', "
        f"description={description!r})\n"
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A marketplace whose agent carries a domain fragment and two defined subagents,
    and one session pinned to that agent (as session creation pins it)."""
    files = {
        f"ai.iowarp.clio/subagents/{subagent_id}.py": _subagent(subagent_id, description)
        for subagent_id, description in SUBAGENTS.items()
    }
    files["ai.iowarp.clio/prompt/frame.md"] = fragment(
        "earthscope", "frame", "domain", "You coordinate the EarthScope workflow."
    )
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={"earthscope": {"files": files}},
        agents={"earthscope.py": AGENT},
    )
    service = PluginService()
    sessions = SessionStore(path=tmp_path / "sessions.json")
    metadata: dict[str, Any] = {}
    prepare_session_agent(
        SimpleNamespace(state=SimpleNamespace(plugin_service=service)),
        metadata,
        "clio/earthscope-main",
    )
    session = sessions.create(workspace_id="ws_default", metadata=metadata)
    return SimpleNamespace(service=service, sessions=sessions, sid=session.id)


def _assembly_app(world: SimpleNamespace, *, arc: Any = None) -> SimpleNamespace:
    """A FRESH app (empty per-app caches) with real MCP declaration binding, without
    starting or executing tools."""

    def reject_execution(_submission: SubmissionContext) -> None:
        raise AssertionError("Prompt assembly must not execute a built-in tool")

    app = SimpleNamespace(
        state=SimpleNamespace(arc=arc, sessions=world.sessions, plugin_service=world.service)
    )
    app.state.builtin_boundary = BuiltinBoundary(app, authorize=reject_execution)
    return app


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the agent build deterministic and offline.

    The LM / adapter are stubbed (we never actually call a model -- we inspect the
    ASSEMBLED prompt), the lm-config is a trivial namespace, and no workspace is bound.
    """

    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(
            materialize=lambda cred_resolver=None: SimpleNamespace(
                provider="argonne", model="gpt-oss-120b", temperature=0.0
            )
        ),
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_workspace_catalog_cwd",
        lambda app, session_id="", workspace_id="": None,
    )


def _resolve(world: SimpleNamespace) -> Resolution:
    """A fresh stage-2 resolution, as a new turn's session would pin."""
    return world.service.resolve("clio/earthscope-main", pin=False)


def _build(world: SimpleNamespace, *, arc: Any = None) -> Any:
    """Build the agent module exactly as a turn would, on a FRESH app."""
    app = _assembly_app(world, arc=arc)
    resolution = _resolve(world)
    sid_token = ctx.set_session_id(world.sid)
    try:
        with _gact_app_context(app):
            return builders.build_agent_module(
                SimpleNamespace(), resolution_agent_def(resolution), resolution
            )
    finally:
        ctx.reset(sid_token)


def _spawn_description(module: Any) -> str:
    [tool] = [tool for tool in module.tools if tool.name == "spawn_agent_task"]
    return str(tool.args["agent"]["description"])


# ---- (iii) SYSTEM-PROMPT byte-stability ------------------------------------- #


def test_agent_system_prompt_byte_stable_across_turns(
    monkeypatch: pytest.MonkeyPatch, world: SimpleNamespace
) -> None:
    """The SAME agent with the SAME plugins renders a byte-identical system prompt on
    turn 1 and turn 2. A datetime/uuid/reordered-dict leak would break this."""
    _patch_runtime(monkeypatch)
    turn1 = _build(world).system_prompt
    turn2 = _build(world).system_prompt
    assert turn1 == turn2
    # And it actually carries the composed identity, core guidance and domain fragment (so
    # the equality above is over the real, load-bearing prompt, not an empty string).
    assert turn1.startswith("You are EarthScope, a GNSS workflow coordinator.")
    assert "wait_tasks" in turn1
    assert "You coordinate the EarthScope workflow." in turn1
    assert world.sid not in turn1


def test_signature_instructions_byte_stable_across_turns(
    monkeypatch: pytest.MonkeyPatch, world: SimpleNamespace
) -> None:
    """The dspy signature *instructions* ARE the literal system message ChatAdapter
    emits. They must be byte-stable for the SAME agent across turns -- the signature is
    static (the workflow_state field type is stable and the rest is fixed)."""
    _patch_runtime(monkeypatch)
    app = _assembly_app(world)
    sid_token = ctx.set_session_id(world.sid)
    try:
        with _gact_app_context(app):
            sig1 = builders._agent_signature(resolution_agent_def(_resolve(world)))
            sig2 = builders._agent_signature(resolution_agent_def(_resolve(world)))
    finally:
        ctx.reset(sid_token)
    assert sig1.instructions == sig2.instructions
    assert list(sig1.output_fields) == list(sig2.output_fields)


def test_subagent_catalog_byte_stable(
    monkeypatch: pytest.MonkeyPatch, world: SimpleNamespace
) -> None:
    """The spawn tool's subagent catalog (the child-description injection the model sees
    in its tool list) is byte-stable across turns and lists every defined subagent."""
    _patch_runtime(monkeypatch)
    first = _spawn_description(_build(world))
    second = _spawn_description(_build(world))
    assert first == second
    for subagent_id, description in SUBAGENTS.items():
        assert f"{subagent_id} ({description})" in first


def test_subagent_order_is_deterministic(world: SimpleNamespace) -> None:
    """Subagents resolve in one fixed order on every resolution, so the rendered catalog
    never depends on filesystem listing or dict/set iteration order."""
    orders = {tuple(row["id"] for row in delegable_subagents(_resolve(world))) for _ in range(3)}
    assert len(orders) == 1
    assert sorted(next(iter(orders))) == sorted(SUBAGENTS)


# ---- (i) ARC-vs-native multi-turn equivalence ------------------------------- #


def test_system_prompt_identical_arc_vs_native(
    monkeypatch: pytest.MonkeyPatch, world: SimpleNamespace
) -> None:
    """The assembled agent system prompt is byte-identical whether or not an ARC is
    bound on the app -- the prompt assembly does not branch on ARC. (ARC governs the
    per-turn trajectory render, not the system prompt.)"""
    _patch_runtime(monkeypatch)
    native = _build(world, arc=None).system_prompt
    arc_backed = _build(world, arc=SimpleNamespace(name="fake-arc")).system_prompt
    assert native == arc_backed


def test_system_prompt_identical_arc_vs_native_two_turns(
    monkeypatch: pytest.MonkeyPatch, world: SimpleNamespace
) -> None:
    """Multi-turn: run the agent build across two turns on BOTH the ARC path and the
    native (arc=None) path; every one of the four system prompts is identical. This is
    the cross-path AND cross-turn equivalence in one assertion."""
    _patch_runtime(monkeypatch)
    prompts: list[str] = []
    for arc in (None, SimpleNamespace(name="fake-arc")):
        for _turn in range(2):
            prompts.append(_build(world, arc=arc).system_prompt)
    assert len(set(prompts)) == 1, "system prompt differs across turn/ARC-path"


# ---- (ii) cross-turn prefix stability (ties to KV reuse) -------------------- #


def test_system_message_is_a_byte_prefix_across_turns(
    monkeypatch: pytest.MonkeyPatch, world: SimpleNamespace
) -> None:
    """The cacheable prefix -- the literal system message (dspy signature instructions)
    PLUS the static assembled system_prompt the agent always emits first -- is a byte
    PREFIX of the next turn's. With a no-compaction, same-agent setup the two are in fact
    EQUAL, so the trivial prefix relation (equality => prefix) holds and the KV cache hits
    from byte 0 through the entire system region. The per-turn user content (the question
    + prepended transcript) appends AFTER this prefix."""
    _patch_runtime(monkeypatch)
    module1 = _build(world)
    prefix_1 = module1.program.signature.instructions + "\n\n" + module1.system_prompt
    module2 = _build(world)
    prefix_2 = module2.program.signature.instructions + "\n\n" + module2.system_prompt
    assert prefix_2.startswith(prefix_1)
    assert prefix_1 == prefix_2  # no-compaction same-agent: exact, not just prefix
