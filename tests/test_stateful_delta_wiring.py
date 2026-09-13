"""Wiring pins for Codex routing and the Claude SDK stateful-delta transport.

These lock the three fixes whose *wiring* (not the shared detector, proved in
``test_claude_code_stateful``) is the deliverable:

* **T1 — V2+codex routing.** A codex model id that collides with a litellm-registered
  OpenAI model name (``gpt-5.6-sol``) must reach the codex ``CustomLLM`` handler, NOT
  litellm's OpenAI handler (which raises ``'codex' is not a valid LlmProviders``). The
  clio-side guard is the ``cdx-`` namespace marker in
  :func:`clio_agent.lm.factory._resolve_model_name`. **Sabotage:** drop the marker →
  ``create_lm`` yields the bare ``codex/gpt-5.6-sol`` → litellm routes it to OpenAI →
  this test goes red.

* **T2 — ops_reset.** When ARC autocompaction rewrites the History prefix
  (``_RetainingReActV2._maybe_autocompact`` → ``arc.summarize_segments``), the active
  stateful scope must be flagged for a typed ``ops_reset`` so the
  next send classifies precisely instead of the generic ``prefix_mismatch``.
  **Sabotage:** unwire the ``note_prefix_reset_for_active_scope`` call → the next plan
  returns ``prefix_mismatch``/``delta`` → red.

* **T3 — Tier-1-shaped delta.** The legacy ``ClioAgent.forward`` planner-loop
  scope binding was deleted with the planner (#948 S4b); the delta mechanism it
  relied on (append-only sends under an active ``stateful_scope`` classify as a
  delta over the retained prefix) is pinned below on the Claude SDK registry.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest

from clio_agent.providers import claude_code_stateful as ccs
from clio_agent.providers.stateful_common import (
    active_stateful_scope,
    note_prefix_reset_for_active_scope,
    stateful_scope,
)


def _m(*texts: str) -> list[dict[str, Any]]:
    """A rendered chat-message list (the prefix-check operand)."""
    return [{"role": "user", "content": t} for t in texts]


def _key(scope: str) -> tuple[Any, ...]:
    """A registry session key under ``scope`` (shape matches the real legs)."""
    return (scope, "m", None, None)


# --------------------------------------------------------------------------- #
# T1 — V2+codex routing: the collision-avoidance marker reaches the transport. #
# --------------------------------------------------------------------------- #
def test_codex_colliding_model_reaches_custom_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """A codex model whose id collides with an OpenAI model name still routes to codex.

    The regression pin for the V2+codex routing bug: ``gpt-5.6-sol`` is a litellm-
    registered OpenAI chat model, so the bare ``codex/gpt-5.6-sol`` is hijacked to
    litellm's OpenAI handler (``'codex' is not a valid LlmProviders``). ``create_lm``'s
    ``cdx-`` marker (``_resolve_model_name``) is the guard: the resolved
    ``codex/cdx-gpt-5.6-sol`` reaches the codex ``CustomLLM`` handler instead. Removing
    the marker turns both assertions red.
    """
    import litellm

    from clio_agent.config import LMProviderConfig, create_lm
    from clio_agent.providers import codex_litellm

    codex_litellm.ensure_registered()
    litellm.utils.custom_llm_setup()
    # The bare model id WOULD collide with a registered OpenAI model — that is the trap.
    assert "gpt-5.6-sol" in litellm.open_ai_chat_completion_models

    cfg = LMProviderConfig(provider="codex", model="gpt-5.6-sol")
    resolved = create_lm(cfg).model
    # The marker namespaces the id out of the OpenAI collision set.
    assert resolved == "codex/cdx-gpt-5.6-sol"

    reached: dict[str, Any] = {}

    def _stub_completion(self: Any, *args: Any, **kwargs: Any) -> Any:
        reached["model"] = kwargs.get("model") or (args[0] if args else None)
        raise RuntimeError("REACHED-CODEX-TRANSPORT")

    monkeypatch.setattr(codex_litellm.CodexLLM, "completion", _stub_completion)

    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - message is asserted below
        litellm.completion(
            model=resolved,
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
        )
    # NOT the OpenAI-hijack routing error; the codex handler WAS reached (litellm hands
    # the custom handler the provider-prefix-stripped id — the ``cdx-`` marker survives
    # so the handler's own ``removeprefix('cdx-')`` recovers the real ``gpt-5.6-sol``).
    assert "is not a valid LlmProviders" not in str(excinfo.value)
    assert reached.get("model") == "cdx-gpt-5.6-sol"


# --------------------------------------------------------------------------- #
# T2 — ops_reset: the shared hook flags the Claude SDK stateful registry.
# --------------------------------------------------------------------------- #
def test_note_prefix_reset_flags_claude_sdk_registry() -> None:
    """The shared ARC-op hook flags the active Claude SDK scope.

    An ARC compact/delete rewrites the prefix for whichever leg the active loop drives,
    so the hook must mark its registered stateful registry. Its next plan over a
    would-be-valid extension is then a typed ``ops_reset`` full send.
    """
    ccs.stateful_registry().reset_for_tests()
    with stateful_scope("s"):
        # Prime a live Claude SDK session (call 1 = full/first_call).
        ccs.stateful_registry().plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
        assert note_prefix_reset_for_active_scope("ops_reset") is True
        plan, _handle = ccs.stateful_registry().plan(
            session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c")
        )
        assert plan.mode == "full"
        assert plan.reason == "ops_reset"
    ccs.stateful_registry().reset_for_tests()


def test_note_prefix_reset_is_noop_off_scope() -> None:
    """Off the V2 loop (no active scope) the hook is a safe no-op returning False."""
    assert active_stateful_scope() is None
    assert note_prefix_reset_for_active_scope("ops_reset") is False


def test_maybe_autocompact_wires_ops_reset_through_the_v2_loop(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced V2 auto-compaction flags ``ops_reset`` on the active scope's registries.

    Drives the real :func:`clio_agent.gact.compaction.maybe_autocompact` (the #1339
    unification target -- ``_RetainingReActV2._maybe_autocompact`` is now a 3-line
    delegation to it) with its ARC/runtime dependencies stubbed to trigger a real
    ``arc.summarize_segments`` (the History-prefix rewrite), then asserts the next
    Claude SDK send is a typed ``ops_reset``. Sabotage: delete the
    ``note_prefix_reset_for_active_scope`` call in ``compaction.py`` -> the next plan
    is ``delta`` (or ``prefix_mismatch``) -> red.

    #1339 review round: the ORIGINAL version of this test drove a bare ``_FakeArc``
    with no real app at all, patching ``agents.runtime._last_prompt_tokens`` -- both
    stale relative to the #1339 unification, which routes through the real
    ``compact_session_context`` (needing ``app.state.sessions``/``.messages``/``.agent``)
    and reads ``_last_prompt_tokens`` from its true origin module
    (``runtime.context_tokens``, matched by ``tests/test_gact/test_compaction.py``'s
    already-passing ``test_auto_trigger_stages_and_flushes_after_the_turns_assistant_row``
    sibling coverage of the SAME wiring). Rebuilt on that same proven pattern: a real
    ``build_app`` + a seeded ledger + ``_ctx.set_app`` to bind the active app
    ``compact_session_context`` requires (a bare fake object -- no app at all --
    crashed ``compact_session_context``'s unguarded ``app.state.sessions.get(sid)``,
    which is what ``compaction.py``'s new ``app is None`` early-return guards).
    """
    from clio_agent.arc.live import _MemoryStore
    from clio_agent.arc.memory import ARCMemory
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.agents import reactv2_events as _events
    from clio_agent.gact.app import build_app
    from clio_agent.gact.compaction import maybe_autocompact
    from clio_agent.gact.runtime import context_tokens as _ctok
    from clio_agent.gact.types import Message, Part

    class _CapturingAgent:
        """Minimal compact agent (matches test_compaction.py's ``_CapturingAgent``)."""

        def _run_chat_agent(self, question: str, _session_id: str) -> str:
            return "auto summary"

        def _call_with_transient_provider_retries(self, _label: str, call: Any) -> Any:
            return call()

    now = "2026-09-11T00:00:00+00:00"
    # This file lives outside tests/test_gact/, so it does not get that package's
    # conftest.py in-memory-ARC-by-default wrapper around build_app -- construct one
    # explicitly, the same shape (a real ARCMemory, no filesystem persistence).
    arc_store = ARCMemory(data_dir=str(tmp_path / "arc"), store=_MemoryStore())
    app = build_app(sessions_path=tmp_path / "s.json", agent=_CapturingAgent(), arc=arc_store)
    sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
    seeded = [
        Message(
            id="msg_user_1",
            session_id=sid,
            role="user",
            created_at=now,
            updated_at=now,
            parts=[Part(id="part_user_1", type="text", text="hello")],
        )
    ]
    app.state.messages[sid] = seeded
    app.state.message_store.replace_session(sid, seeded)

    arc = app.state.arc
    scope = "scope_auto"
    arc.append_segment(sid, scope, "observation", {"text": "first live segment"})
    arc.append_segment(sid, scope, "observation", {"text": "second live segment"})

    monkeypatch.setattr(_events, "_arc_scope", lambda: (arc, sid, scope))
    monkeypatch.setattr(_ctx, "active_react_context_window", lambda: 1000)
    monkeypatch.setattr(_ctok, "_last_prompt_tokens", lambda: 950)
    monkeypatch.setattr(_ctok, "_autocompact_threshold", lambda: 0.5)

    summarize_calls: list[tuple[Any, ...]] = []
    real_summarize = arc.summarize_segments

    def _spy_summarize(*args: Any, **kwargs: Any) -> Any:
        summarize_calls.append(args)
        return real_summarize(*args, **kwargs)

    arc.summarize_segments = _spy_summarize  # type: ignore[method-assign]

    ccs.stateful_registry().reset_for_tests()
    with stateful_scope("s"):
        ccs.stateful_registry().plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
        app_token = _ctx.set_app(app)
        try:
            maybe_autocompact()
        finally:
            _ctx.reset(app_token)
        assert len(summarize_calls) == 1  # the op really fired
        plan, _handle = ccs.stateful_registry().plan(
            session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c")
        )
        assert plan.mode == "full"
        assert plan.reason == "ops_reset"
    ccs.stateful_registry().reset_for_tests()


# --------------------------------------------------------------------------- #
# T3 — Tier-1-shaped stateful scope: append-only sends delta on call 2+.
#
# The legacy ``ClioAgent.forward`` planner-loop scope-binding test was deleted
# with the planner (#948 S4b). Post-S4b the top-level orchestrator IS the reactv2
# retention forward, whose ``with stateful_scope():`` (reactv2.py:193) is the
# surviving equivalent binding. Two locks below:
#   * ``test_reactv2_forward_binds_stateful_scope`` drives a REAL V2 forward and
#     asserts the scope is active INSIDE the loop body — the sabotage guard on the
#     orchestrator-level binding (remove the ``with`` and it goes red), restored
#     in the new world to replace the deleted planner-loop guard.
#   * ``test_tier1_shaped_forward_deltas_on_call_two`` pins the delta MECHANISM
#     the binding unlocks, directly on the Claude SDK registry (append-only
#     growing message list under an active scope).
# --------------------------------------------------------------------------- #
def test_reactv2_forward_binds_stateful_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The V2 orchestrator forward binds a per-forward stateful scope for its LM sends.

    Post-#948-S4b the top-level orchestrator is the reactv2 retention forward, not the
    deleted ``ClioAgent.forward`` planner. Its ``with stateful_scope():`` binding
    (reactv2.py:193) is what makes consecutive append-only orchestrator LM sends
    classify as prefix deltas. This drives a real V2 forward and asserts
    ``active_stateful_scope()`` is non-None from INSIDE the loop body — the
    orchestrator-level invariant the deleted planner test used to guard.

    Sabotage: remove ``with stateful_scope():`` in the reactv2 forward → the captured
    scope is ``None`` → this test goes red.
    """
    from clio_agent.gact.agents import reactv2_events as _events
    from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls

    class _Sig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    def _tool(x: str) -> str:
        """A tool."""
        return x

    captured: dict[str, Any] = {}

    def _fake_instrumented_forward(agent: Any, **input_args: Any) -> Any:
        # Runs where the real append-only V2 loop runs: under the forward's
        # ``with stateful_scope():``. Record what the rail carries.
        captured["scope"] = active_stateful_scope()
        return dspy.Prediction(answer="ok")

    monkeypatch.setattr(_events, "instrumented_forward", _fake_instrumented_forward)

    agent = retaining_reactv2_cls()(_Sig, tools=[_tool], max_iters=1)
    pred = agent.forward(question="hi")

    assert pred.answer == "ok"
    # The forward bound a live stateful scope around the loop (unbinding → None).
    assert captured["scope"] is not None


# --------------------------------------------------------------------------- #
def test_tier1_shaped_forward_deltas_on_call_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Tier-1-shaped forward with append-only sends deltas on call 2+ under the scope.

    The mechanism the T3 binding unlocks: bound stateful scope + an append-only growing
    message list ⇒ the second send is a delta over the retained prefix. Pinned on the
    real Claude SDK registry.
    """
    monkeypatch.setattr(ccs, "stateful_delta_enabled", lambda: True)
    ccs.stateful_registry().reset_for_tests()
    reg = ccs.stateful_registry()
    with stateful_scope("tier1"):
        plan1, _h1 = reg.plan(session_key=_key("tier1"), scope_token="tier1", messages=_m("q", "a"))
        assert plan1.mode == "full" and plan1.reason == "first_call"
        # Call 2: the message list grew append-only (a Tier-1 planner step appended).
        plan2, _h2 = reg.plan(
            session_key=_key("tier1"), scope_token="tier1", messages=_m("q", "a", "b")
        )
        assert plan2.mode == "delta"
        assert plan2.prefix_len == 2
        assert plan2.messages == _m("b")  # only the appended tail is sent
    ccs.stateful_registry().reset_for_tests()


# --------------------------------------------------------------------------- #
# T4 — scope-keyed stream connections (the AGENT-COPPER12 cross-conversation   #
# defect): concurrent expert loops must never multiplex ENGAGED (delta-capable)#
# sends over one pooled SDK connection — the connection, not the per-call      #
# session_id, is the real conversation boundary for resumed sends.             #
# --------------------------------------------------------------------------- #
def test_stream_pool_isolates_engaged_scopes() -> None:
    """Distinct stateful scopes get distinct pooled connections; base is shared.

    **Sabotage:** drop ``scope`` from the pool key -> both scopes share one entry
    -> a child expert's delta rides the parent's conversation -> red.
    """
    from clio_agent.providers.claude_code_sessions import ClaudeStreamClientPool

    pool = ClaudeStreamClientPool()
    try:
        base_one = pool.entry_for(model="m", cwd=None, thinking=None)
        base_two = pool.entry_for(model="m", cwd=None, thinking=None, scope=None)
        scope_a = pool.entry_for(model="m", cwd=None, thinking=None, scope="loop-a")
        scope_a2 = pool.entry_for(model="m", cwd=None, thinking=None, scope="loop-a")
        scope_b = pool.entry_for(model="m", cwd=None, thinking=None, scope="loop-b")
        assert base_one is base_two  # non-engaged sends share the base connection
        assert scope_a is scope_a2  # one connection per loop, reused across its calls
        assert scope_a is not base_one
        assert scope_b is not scope_a
    finally:
        pool.close_blocking()


def test_stream_pool_releases_scope_entries_on_scope_exit() -> None:
    """``stateful_scope`` exit closes the loop's own connection; base survives.

    The registered-pool seam: the process singleton implements the scope-registry
    protocol, so the react forward's scope teardown drops the forward's stateful
    connection (#900 -- a loop's session never outlives the loop).
    **Sabotage:** unregister the pool (or no-op ``release``) -> the entry persists
    across loops -> red.
    """
    from clio_agent.providers.claude_code_sessions import _STREAM_CLIENT_POOL

    with stateful_scope("loop-rel"):
        held = _STREAM_CLIENT_POOL.entry_for(model="m", cwd=None, thinking=None, scope="loop-rel")
        again = _STREAM_CLIENT_POOL.entry_for(model="m", cwd=None, thinking=None, scope="loop-rel")
        assert held is again
    fresh = _STREAM_CLIENT_POOL.entry_for(model="m", cwd=None, thinking=None, scope="loop-rel")
    assert fresh is not held  # the scope's connection was closed+dropped on exit
    _STREAM_CLIENT_POOL.release("loop-rel")


def test_stream_scope_derivation_follows_engagement() -> None:
    """Engaged sends carry their scope to the pool; full/fresh sends stay base.

    **Sabotage:** route every send to the base entry (ignore ``engaged``) -> red.
    """
    from clio_agent.providers.claude_code_sessions import stream_scope_for
    from clio_agent.providers.claude_code_stateful import StatefulSend

    engaged = StatefulSend(
        payload="p",
        session_id="s",
        mode="delta",
        reason=None,
        delta_chars=1,
        engaged=True,
        session_key=("sc", "m", None, None),
        scope_token="sc",
        call_id="c",
    )
    inert = StatefulSend(
        payload="p",
        session_id="s2",
        mode="full",
        reason=None,
        delta_chars=1,
        engaged=False,
        call_id="c2",
    )
    assert stream_scope_for(engaged) == "sc"
    assert stream_scope_for(inert) is None
    assert stream_scope_for(None) is None
