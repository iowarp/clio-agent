"""Wiring pins for the stateful-delta transport across the three #891 seams.

These lock the three fixes whose *wiring* (not the shared detector, proved in
``test_claude_code_stateful`` / ``test_codex_stateful``) is the deliverable:

* **T1 — V2+codex routing.** A codex model id that collides with a litellm-registered
  OpenAI model name (``gpt-5.6-sol``) must reach the codex ``CustomLLM`` handler, NOT
  litellm's OpenAI handler (which raises ``'codex' is not a valid LlmProviders``). The
  clio-side guard is the ``cdx-`` namespace marker in
  :func:`clio_agent.lm.factory._resolve_model_name`. **Sabotage:** drop the marker →
  ``create_lm`` yields the bare ``codex/gpt-5.6-sol`` → litellm routes it to OpenAI →
  this test goes red.

* **T2 — ops_reset.** When ARC autocompaction rewrites the History prefix
  (``_RetainingReActV2._maybe_autocompact`` → ``arc.summarize_segments``), the active
  stateful scope must be flagged for a typed ``ops_reset`` on BOTH provider legs so the
  next send classifies precisely instead of the generic ``prefix_mismatch``.
  **Sabotage:** unwire the ``note_prefix_reset_for_active_scope`` call → the next plan
  returns ``prefix_mismatch``/``delta`` → red.

* **T3 — Tier-1-shaped delta.** The legacy ``ClioAgent.forward`` planner-loop
  scope binding was deleted with the planner (#948 S4b); the delta mechanism it
  relied on (append-only sends under an active ``stateful_scope`` classify as a
  delta over the retained prefix) is pinned below directly on the shared codex
  registry, which does not depend on the deleted loop.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest

from clio_agent.providers import claude_code_stateful as ccs
from clio_agent.providers import codex_stateful as cst
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
# T2 — ops_reset: the shared hook flags BOTH legs; the V2 compaction op wires it.
# --------------------------------------------------------------------------- #
def test_note_prefix_reset_flags_both_provider_registries() -> None:
    """The shared ARC-op hook flags the active scope in the codex AND claude registries.

    An ARC compact/delete rewrites the prefix for whichever leg the active loop drives,
    so the hook must mark every registered registry (not just one). Both legs' next
    plan over a would-be-valid extension is then a typed ``ops_reset`` full send.
    """
    cst.codex_stateful_registry().reset_for_tests()
    ccs.stateful_registry().reset_for_tests()
    with stateful_scope("s"):
        # Prime a live session on each leg (call 1 = full/first_call).
        cst.codex_stateful_registry().plan(
            session_key=_key("s"), scope_token="s", messages=_m("a", "b")
        )
        ccs.stateful_registry().plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
        assert note_prefix_reset_for_active_scope("ops_reset") is True
        # An append-only extension that WOULD be a delta is forced full=ops_reset on both.
        for reg in (cst.codex_stateful_registry(), ccs.stateful_registry()):
            plan, _handle = reg.plan(
                session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c")
            )
            assert plan.mode == "full"
            assert plan.reason == "ops_reset"
    cst.codex_stateful_registry().reset_for_tests()
    ccs.stateful_registry().reset_for_tests()


def test_note_prefix_reset_is_noop_off_scope() -> None:
    """Off the V2 loop (no active scope) the hook is a safe no-op returning False."""
    assert active_stateful_scope() is None
    assert note_prefix_reset_for_active_scope("ops_reset") is False


def test_maybe_autocompact_wires_ops_reset_through_the_v2_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced V2 auto-compaction flags ``ops_reset`` on the active scope's registries.

    Integration-shaped: drives ``_RetainingReActV2._maybe_autocompact`` with its ARC /
    runtime dependencies stubbed to trigger a real ``arc.summarize_segments`` (the
    History-prefix rewrite), then asserts the next send on both legs is a typed
    ``ops_reset``. Sabotage: delete the ``note_prefix_reset_for_active_scope`` call in
    ``_maybe_autocompact`` → the next plan is ``delta`` (or ``prefix_mismatch``) → red.
    """
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.agents import reactv2_events as _events
    from clio_agent.gact.agents import runtime as _rt
    from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls
    from clio_agent.gact.runtime import context_tokens as _ctok

    class _Seg:
        def __init__(self, sid: str) -> None:
            self.id = sid

    summarized: dict[str, Any] = {}

    class _FakeArc:
        def render_working_set(self, session: str, scope: str) -> list[_Seg]:
            return [_Seg("s0"), _Seg("s1")]  # len > 1 so compaction proceeds

        def summarize_segments(
            self, session: str, scope: str, ids: list[str], payload: dict[str, Any]
        ) -> None:
            summarized["ids"] = ids

    monkeypatch.setattr(_events, "_arc_scope", lambda: (_FakeArc(), "sess", "arcscope"))
    monkeypatch.setattr(_ctx, "active_react_context_window", lambda: 1000)
    monkeypatch.setattr(_rt, "_last_prompt_tokens", lambda: 950)
    monkeypatch.setattr(_rt, "_summarize_segments_llm", lambda live: "summary")
    monkeypatch.setattr(_ctok, "_autocompact_threshold", lambda: 0.5)

    class _Sig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    def _tool(x: str) -> str:
        """A tool."""
        return x

    agent = retaining_reactv2_cls()(_Sig, tools=[_tool], max_iters=1)

    cst.codex_stateful_registry().reset_for_tests()
    ccs.stateful_registry().reset_for_tests()
    with stateful_scope("s"):
        cst.codex_stateful_registry().plan(
            session_key=_key("s"), scope_token="s", messages=_m("a", "b")
        )
        ccs.stateful_registry().plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
        agent._maybe_autocompact()
        assert summarized.get("ids") == ["s0", "s1"]  # the op really fired
        for reg in (cst.codex_stateful_registry(), ccs.stateful_registry()):
            plan, _handle = reg.plan(
                session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c")
            )
            assert plan.mode == "full"
            assert plan.reason == "ops_reset"
    cst.codex_stateful_registry().reset_for_tests()
    ccs.stateful_registry().reset_for_tests()


# --------------------------------------------------------------------------- #
# T3 — Tier-1-shaped stateful scope: append-only sends delta on call 2+.
#
# The legacy ``ClioAgent.forward`` planner-loop scope-binding test was deleted
# with the planner (#948 S4b). The delta mechanism it exercised is pinned below
# directly on the shared codex registry (the append-only growing message list
# under an active ``stateful_scope``), which does not depend on the deleted loop.
# --------------------------------------------------------------------------- #
def test_tier1_shaped_forward_deltas_on_call_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Tier-1-shaped forward with append-only sends deltas on call 2+ under the scope.

    The mechanism the T3 binding unlocks: bound stateful scope + an append-only growing
    message list ⇒ the second send is a delta over the retained prefix. Pinned on the
    real codex registry (the same shared detector the claude leg uses).
    """
    monkeypatch.setattr(cst, "codex_stateful_delta_enabled", lambda: True)
    cst.codex_stateful_registry().reset_for_tests()
    reg = cst.codex_stateful_registry()
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
    cst.codex_stateful_registry().reset_for_tests()
