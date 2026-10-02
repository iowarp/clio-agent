"""Wiring pins for Codex routing and the Claude SDK stateful-delta transport.

These lock the three fixes whose *wiring* (not the shared detector, proved in
``test_claude_code_stateful``) is the deliverable:

* **T1 — codex routing.** A codex model id that collides with a litellm-registered
  OpenAI model name (``gpt-5.6-sol``) runs on clio's own Codex direct engine (an engine
  LM LiteLLM never routes), the backend receives the bare id, and the model-string
  prefix is ``codex_direct`` -- never bare ``codex``, which collides with litellm's OWN
  native ``codex`` provider (:data:`clio_agent.providers.codex.constants.LITELLM_PROVIDER`).

* **T2 — ops_reset.** When ARC autocompaction rewrites the History prefix
  (``ClioReAct``'s per-step ``maybe_autocompact`` → ``arc.summarize_segments``), the active
  stateful scope must be flagged for a typed ``ops_reset`` so the
  next send is a precise typed reset instead of the generic ``prefix_mismatch``.
  **Sabotage:** unwire the ``note_prefix_reset_for_active_scope`` call → the next plan
  returns ``prefix_mismatch``/``delta`` → red.

* **T3 — Tier-1-shaped delta.** ``ClioReAct.forward`` binds the stateful scope; the
  delta mechanism it unlocks (append-only typed messages under an active scope
  continue the kept conversation with only the new messages) is pinned below on the
  Claude Code engine's conversation registry.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.lm15 import Message, Request

from clio_agent.providers import claude_code_engine
from clio_agent.providers.stateful_common import (
    active_stateful_scope,
    note_prefix_reset_for_active_scope,
    stateful_scope,
)
from tests._scripted_engine import AsyncScriptedEngine, Reply, ScriptedEngine, calls


def _r(*texts: str) -> Request:
    """A request whose messages alternate user / assistant, starting with the user."""
    messages = tuple(
        Message.user(t) if i % 2 == 0 else Message.assistant(t) for i, t in enumerate(texts)
    )
    return Request(model="claude_code/m", messages=messages)


def _key(scope: str) -> tuple[str, ...]:
    """A conversation key (session, scope, model, cwd, thinking) -- the engine's shape."""
    return ("sess", scope, "m", "/w", "")


def _prime(scope: str) -> None:
    """Record a kept conversation the scope's forward drove (call 1 = full/first_call)."""
    registry = claude_code_engine._CONVERSATIONS
    assert registry.plan(_key(scope), _r("q"), "sys").reason == "first_call"
    registry.opened(_key(scope), "sid-1", _r("q"), "sys")


# --------------------------------------------------------------------------- #
# T1 — V2+codex routing: the collision-avoidance marker reaches the transport. #
# --------------------------------------------------------------------------- #
def test_codex_colliding_model_reaches_clios_own_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A codex model whose id collides with an OpenAI model name still runs on clio's own
    Codex direct engine, never LiteLLM's OpenAI dialect NOR LiteLLM's native "codex".

    ``gpt-5.6-sol`` is (made) a LiteLLM-registered OpenAI chat model. The LM is an
    engine LM, so LiteLLM never routes it; the backend receives the bare model id, and
    the model-string prefix is "codex_direct" (never bare "codex").
    """
    import json

    import litellm
    from dspy.lm15 import Message, OpenAICodexLM, Request

    from clio_agent.config import LMProviderConfig, create_lm
    from clio_agent.providers.codex import direct_engine

    monkeypatch.setattr(
        litellm,
        "open_ai_chat_completion_models",
        set(litellm.open_ai_chat_completion_models) | {"gpt-5.6-sol"},
    )
    monkeypatch.setattr(
        direct_engine, "default_wire", lambda: OpenAICodexLM(api_key="t", account_id="a")
    )
    frames: list[dict[str, Any]] = []

    class _Socket:
        def __init__(self) -> None:
            self._events: list[str] = []

        async def send(self, raw: str) -> None:
            frames.append(json.loads(raw))
            done = {"type": "response.completed", "response": {"id": "r1", "output": []}}
            self._events = [
                json.dumps({"type": "response.created", "response": {"id": "r1"}}),
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "delta": "ok",
                        "item_id": "m",
                        "output_index": 0,
                        "content_index": 0,
                    }
                ),
                json.dumps(done),
            ]

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

        async def close(self) -> None:
            return None

    async def _connect(*_a: Any) -> _Socket:
        return _Socket()

    monkeypatch.setattr(direct_engine, "_connect", _connect)

    lm = create_lm(LMProviderConfig(provider="codex", model="gpt-5.6-sol"))
    assert lm.model == "codex_direct/gpt-5.6-sol"
    lm(Request(model=lm.model, messages=(Message.user("hi"),)))
    assert [f["model"] for f in frames] == ["gpt-5.6-sol"]


# --------------------------------------------------------------------------- #
# T2 — ops_reset: the shared hook flags the Claude SDK stateful registry.
# --------------------------------------------------------------------------- #
def test_note_prefix_reset_flags_claude_sdk_registry() -> None:
    """The shared ARC-op hook flags the conversations the active forward drove.

    An ARC compact/delete rewrites the prefix for whichever leg the active loop drives,
    so the hook must reach every registered conversation registry. Its next plan over
    a would-be-valid extension is then a typed ``ops_reset`` full send.
    """
    registry = claude_code_engine._CONVERSATIONS
    registry.clear_for_tests()
    with stateful_scope("s"):
        _prime("s")
        assert note_prefix_reset_for_active_scope("ops_reset") is True
        send = registry.plan(_key("s"), _r("q", "a", "b"), "sys")
        assert (send.handle, send.reason) == (None, "ops_reset")
    registry.clear_for_tests()


def test_note_prefix_reset_is_noop_off_scope() -> None:
    """Off the loop (no active scope) the hook is a safe no-op returning False."""
    assert active_stateful_scope() is None
    assert note_prefix_reset_for_active_scope("ops_reset") is False


def test_maybe_autocompact_wires_ops_reset_through_the_loop(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced auto-compaction flags ``ops_reset`` on the active scope's registries.

    Drives the real :func:`clio_agent.gact.compaction.maybe_autocompact` (the trigger
    ``ClioReAct`` fires at every step boundary) with its ARC/runtime dependencies stubbed to trigger a real
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
    from clio_agent.gact.agents import clio_react_record
    from clio_agent.gact.app import build_app
    from clio_agent.gact.compaction import AutoCompactionGuard, maybe_autocompact
    from clio_agent.gact.runtime import context_tokens as _ctok
    from clio_agent.gact.types import Message, Part

    class _CapturingAgent:
        """Minimal compact agent (matches test_compaction.py's ``_CapturingAgent``)."""

        def _run_chat_agent(self, question: str, _session_id: str) -> str:
            summary_scopes.append(active_stateful_scope())
            return "auto summary"

        def _call_with_transient_provider_retries(self, _label: str, call: Any) -> Any:
            return call()

    # The summary is not a step of the agent's conversation: it runs outside the
    # forward's stateful scope, so it never replaces the agent's kept conversation.
    # Sabotage: drop ``outside_stateful_scope`` in ``compaction._summary`` -> ["s"] -> red.
    summary_scopes: list[str | None] = []
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
    # one coherent step, as the recorder writes it: calls answered by call id
    arc.append_segment(sid, scope, "thought", {"text": "working"})
    for i, text in enumerate(("first live segment", "second live segment")):
        call = {"id": f"call_{i}", "name": "t", "args": {}}
        arc.append_segment(sid, scope, "tool_call", call)
        obs = {"call_id": f"call_{i}", "text": text, "is_error": False}
        arc.append_segment(sid, scope, "observation", obs)

    monkeypatch.setattr(clio_react_record, "arc_scope", lambda: (arc, sid, scope))
    monkeypatch.setattr(_ctx, "active_react_context_window", lambda: 1000)
    monkeypatch.setattr(_ctok, "_last_prompt_tokens", lambda: 950)
    monkeypatch.setattr(_ctok, "_autocompact_threshold", lambda: 0.5)

    summarize_calls: list[tuple[Any, ...]] = []
    real_summarize = arc.summarize_segments

    def _spy_summarize(*args: Any, **kwargs: Any) -> Any:
        summarize_calls.append(args)
        return real_summarize(*args, **kwargs)

    arc.summarize_segments = _spy_summarize  # type: ignore[method-assign]

    registry = claude_code_engine._CONVERSATIONS
    registry.clear_for_tests()
    with stateful_scope("s"):
        _prime("s")
        app_token = _ctx.set_app(app)
        try:
            maybe_autocompact(AutoCompactionGuard())
        finally:
            _ctx.reset(app_token)
        assert len(summarize_calls) == 1  # the op really fired
        assert summary_scopes == [None], "the summary call ran outside the agent's scope"
        assert active_stateful_scope() == "s", "the forward's scope is restored"
        send = registry.plan(_key("s"), _r("q", "a", "b"), "sys")
        assert (send.handle, send.reason) == (None, "ops_reset")
    registry.clear_for_tests()


# --------------------------------------------------------------------------- #
# T3 — Tier-1-shaped stateful scope: append-only sends delta on call 2+.
#
# The top-level orchestrator is ``ClioReAct``, whose ``forward`` binds
# ``with stateful_scope():`` around its loop. Two locks below:
#   * ``test_clio_react_forward_binds_stateful_scope`` drives a REAL forward and
#     asserts the scope is active INSIDE the loop body (at the model call) and
#     released after -- remove the ``with`` and it goes red.
#   * ``test_tier1_shaped_forward_deltas_on_call_two`` pins the delta MECHANISM
#     the binding unlocks, directly on the Claude SDK registry (append-only
#     growing message list under an active scope).
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("clio_core_plane")
def test_clio_react_forward_binds_stateful_scope() -> None:
    """``ClioReAct.forward`` binds a fresh per-forward stateful scope for its LM sends.

    Its ``with stateful_scope():`` binding is what makes consecutive append-only
    orchestrator LM sends classify as prefix deltas. This drives a real forward and
    asserts ``active_stateful_scope()`` is non-None at every model call, stable
    within one forward, distinct across forwards, and released afterwards.

    Sabotage: remove ``with stateful_scope():`` in ``ClioReAct.forward`` → the
    captured scope is ``None`` → this test goes red.
    """
    from clio_agent.gact.agents.clio_react import ClioReAct

    class _Sig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    def _tool(x: str) -> str:
        """A tool."""
        return x

    captured: list[Any] = []

    class _Capturing(ScriptedEngine):
        def _next(self, request: Any) -> Any:
            # Runs where the real model call runs: inside the loop, under the
            # forward's ``with stateful_scope():``. Record what the rail carries.
            captured.append(active_stateful_scope())
            return super()._next(request)

    step = [calls(("_tool", {"x": "1"}), text="call"), Reply(text="ok")]
    engine = _Capturing(step * 2)
    lm = dspy.LM(
        "scripted/model",
        engine=engine,
        async_engine=AsyncScriptedEngine(engine),
        cache=False,
        num_retries=0,
    )
    agent = ClioReAct(_Sig, tools=[_tool], max_iters=4)
    with dspy.context(lm=lm):
        first = agent(question="hi")
        second = agent(question="again")

    assert (first.answer, second.answer) == ("ok", "ok")
    assert len(captured) == 4
    assert all(scope is not None for scope in captured)
    assert captured[0] == captured[1], "one forward = one scope across its calls"
    assert captured[2] == captured[3]
    assert captured[0] != captured[2], "each forward binds a fresh scope"
    assert active_stateful_scope() is None, "the scope is released after the forward"


# --------------------------------------------------------------------------- #
def test_tier1_shaped_forward_deltas_on_call_two() -> None:
    """A forward with append-only sends continues its conversation on call 2+.

    The mechanism the T3 binding unlocks: bound stateful scope + an append-only growing
    message list => the second send carries only what the provider has not seen.
    Pinned on the real Claude Code engine registry.
    """
    registry = claude_code_engine._CONVERSATIONS
    registry.clear_for_tests()
    with stateful_scope("tier1"):
        _prime("tier1")
        # Call 2: the provider's reply, then one appended user message.
        send = registry.plan(_key("tier1"), _r("q", "a", "b"), "sys")
        assert send.handle == "sid-1"
        assert send.messages == (Message.user("b"),)  # only the appended tail is sent
    registry.clear_for_tests()


# --------------------------------------------------------------------------- #
# T4 — S2 (B1): the stream pool isolates by GACT SESSION id, not react-loop
# scope, and (unlike the pre-S2 scope-keyed design) a stateful_scope's exit
# must NOT close the pool's connection — B1's whole point is that the SAME
# client survives across every turn (every forward) of one session. The
# kept conversations' correctness (the AGENT-COPPER12 cross-conversation defect
# this used to guard) lives in the engine's conversation registry: see
# test_claude_code_stateful.py / this file's T3 above.
# --------------------------------------------------------------------------- #
def test_stream_pool_isolates_by_gact_session_not_scope() -> None:
    """Distinct GACT sessions get distinct pooled connections; one session's
    connection is reused across calls regardless of which stateful scope (or
    none) is active for a given call.

    **Sabotage:** drop ``session_id`` from the pool key (or key on scope again)
    -> two unrelated sessions share one connection -> red.
    """
    from clio_agent.providers.claude_code_sessions import ClaudeStreamClientPool

    pool = ClaudeStreamClientPool()
    try:
        sess_a = pool.entry_for(session_id="sess-a")
        sess_a_again = pool.entry_for(session_id="sess-a")
        sess_b = pool.entry_for(session_id="sess-b")
        off_turn_one = pool.entry_for(session_id="")
        off_turn_two = pool.entry_for(session_id="")
        assert sess_a is sess_a_again  # one connection per session, reused
        assert sess_a is not sess_b  # distinct sessions never share a connection
        assert off_turn_one is off_turn_two  # the off-turn fallback key is shared
        assert off_turn_one is not sess_a
    finally:
        pool.close_blocking()


def test_stream_pool_entry_survives_a_stateful_scope_exit() -> None:
    """B1: a session's connection OUTLIVES the react-loop forward that used it.

    The pre-S2 design tore the connection down at every ``stateful_scope()``
    exit (a fresh connection — and a cold reconnect — every turn). B1 requires
    the opposite: the SAME entry serves the next turn's forward too.
    **Sabotage:** re-register the pool onto ``stateful_scope``'s per-forward
    scope-registry protocol -> the entry is dropped on scope exit -> red.
    """
    from clio_agent.providers.claude_code_sessions import _STREAM_CLIENT_POOL

    with stateful_scope("turn-1"):
        held = _STREAM_CLIENT_POOL.entry_for(session_id="sess-persist")
    still_here = _STREAM_CLIENT_POOL.entry_for(session_id="sess-persist")
    assert still_here is held  # NOT torn down by the forward's own scope exit
    _STREAM_CLIENT_POOL.release("sess-persist")
