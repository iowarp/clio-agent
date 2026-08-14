"""Stateful session-delta transport for ``claude_code`` (#901, the TTFT closer).

These pin the delta detector + the bounded per-loop session registry + the
transport seam on the REAL objects. The heart is the pure classifier
(:func:`classify_delta`): given call N's rendered messages and call N+1's, it must
extract the append-only delta OR classify a typed reset across the whole matrix.

Each load-bearing pin carries an inline SABOTAGE note — the exact change that turns
it red — so the assertion is not vacuous. Three headline sabotages the task names:

(a) make the prefix check fuzzy (compare only lengths) -> a mismatch pin goes red;
(b) drop the ops-reset -> restart mapping -> the pinned ops-reset pin goes red;
(c) force delta mode on the classic (flag-off) path -> the classic-contract pin
    (full prompt under a fresh session id) goes red.
"""

from __future__ import annotations

import os
from types import ModuleType
from typing import Any

import pytest

from clio_agent.providers import claude_code_litellm
from clio_agent.providers import claude_code_stateful as st
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Every test starts and ends with an empty registry + streaming pool."""
    st.stateful_registry().reset_for_tests()
    _reset_sessions_for_tests()
    yield
    st.stateful_registry().reset_for_tests()
    _reset_sessions_for_tests()


def _m(*texts: str) -> list[dict[str, Any]]:
    """A rendered message list (role/content dicts — the prefix-check operand)."""
    return [{"role": "user", "content": t} for t in texts]


def _serialize(messages: list[dict[str, Any]]) -> str:
    """A deterministic test serializer (so delta payloads are predictable)."""
    return "|".join(str(m["content"]) for m in messages)


# --------------------------------------------------------------------------- #
# 1. The pure prefix predicate.
# --------------------------------------------------------------------------- #
def test_is_strict_prefix_matrix() -> None:
    a, b, c = _m("a"), _m("a", "b"), _m("a", "b", "c")
    assert st.is_strict_prefix(a, b) is True
    assert st.is_strict_prefix(a, c) is True
    assert st.is_strict_prefix(b, c) is True
    # Equal is NOT a strict prefix (no tail to send).
    assert st.is_strict_prefix(b, b) is False
    # Shorter target is not an extension.
    assert st.is_strict_prefix(c, b) is False
    # Divergent content at an interior index fails on DICT compare, not length.
    diverge = [{"role": "user", "content": "a"}, {"role": "user", "content": "X"}, {"role": "user", "content": "c"}]
    assert st.is_strict_prefix(b, diverge) is False
    # SABOTAGE (a): `return len(new) > len(prior)` (length-only) makes the divergent
    # case return True -> this pin goes red.
    assert st.is_strict_prefix([], _m("a")) is True
    assert st.is_strict_prefix([], []) is False


# --------------------------------------------------------------------------- #
# 2. The pure classifier — the delta-detector unit proof (the whole matrix).
# --------------------------------------------------------------------------- #
def test_classify_first_call_is_full() -> None:
    plan = st.classify_delta(None, _m("a", "b"))
    assert plan.mode == "full"
    assert plan.reason == "first_call"
    assert plan.messages == _m("a", "b")
    assert plan.prefix_len == 0


def test_classify_extension_is_delta_of_the_tail() -> None:
    prior, new = _m("a", "b"), _m("a", "b", "c", "d")
    plan = st.classify_delta(prior, new)
    assert plan.mode == "delta"
    assert plan.reason is None
    assert plan.messages == _m("c", "d")  # ONLY the appended tail
    assert plan.prefix_len == 2


def test_classify_divergent_prefix_is_full_mismatch() -> None:
    prior = _m("a", "b")
    new = [{"role": "user", "content": "a"}, {"role": "user", "content": "ZZ"}, {"role": "user", "content": "c"}]
    plan = st.classify_delta(prior, new)
    assert plan.mode == "full"
    assert plan.reason == "prefix_mismatch"
    assert plan.messages == new  # a full send, not a bogus delta


def test_classify_equal_lists_is_full_mismatch_not_empty_delta() -> None:
    # A resample produces the SAME list: there is NO append-only tail, so it must be
    # a full send, never an empty delta.
    prior = _m("a", "b")
    plan = st.classify_delta(prior, _m("a", "b"))
    assert plan.mode == "full"
    assert plan.reason == "prefix_mismatch"


def test_classify_shorter_new_is_full_mismatch() -> None:
    plan = st.classify_delta(_m("a", "b", "c"), _m("a", "b"))
    assert plan.mode == "full"
    assert plan.reason == "prefix_mismatch"


def test_classify_forced_reason_overrides_even_a_valid_extension() -> None:
    # The ops-reset -> restart mapping at the PURE layer: a forced reason forces a
    # full send even when `new` IS a valid prefix-extension of `prior`.
    prior, new = _m("a", "b"), _m("a", "b", "c")
    plan = st.classify_delta(prior, new, forced_reason="ops_reset")
    assert plan.mode == "full"
    assert plan.reason == "ops_reset"
    assert plan.messages == new
    # SABOTAGE (b): drop the `if forced_reason is not None` branch -> this returns a
    # delta -> red.


def test_classify_arc_compaction_simulation_declines_delta() -> None:
    # An ARC compaction replaces the head turns with ONE summary message: the new
    # list is NOT a prefix-extension, so the prefix check MUST decline a delta (the
    # "never delta over a rewritten prefix" safety, caught structurally).
    prior = _m("thought_0", "obs_0", "thought_1", "obs_1")
    compacted = _m("SUMMARY(0..1)", "thought_2", "obs_2")
    plan = st.classify_delta(prior, compacted)
    assert plan.mode == "full"
    assert plan.reason == "prefix_mismatch"
    # SABOTAGE (a): a length-only prefix check would (mis)classify a same-or-longer
    # compacted list as a delta -> this pin goes red.


# --------------------------------------------------------------------------- #
# 2b. The static-tail contract (#901): an append-only body beneath a byte-identical
#     trailing block (the dspy ChatAdapter closing instruction, which never changes
#     bytes but MOVES position as the history grows).
# --------------------------------------------------------------------------- #
def test_delta_beneath_static_tail_engages_over_a_moving_tail() -> None:
    # The real V2 wire: a growing head [system, head, turns...] plus ONE byte-static
    # trailing block T that moves each call. A plain strict-prefix declines (T moved);
    # the extended contract extracts the append-only BODY delta and leaves T unshipped.
    prior = _m("sys", "head", "turn0", "TAIL")
    new = _m("sys", "head", "turn0", "turn1", "TAIL")
    # Plain strict prefix fails (TAIL is at a different index).
    assert st.is_strict_prefix(prior, new) is False
    plan = st.classify_delta(prior, new)  # default static_tail_len=1
    assert plan.mode == "delta"
    assert plan.reason is None
    assert plan.messages == _m("turn1")  # ONLY the new body; the static tail is not re-sent
    assert plan.prefix_len == 3  # sys, head, turn0 reused as a byte-stable body prefix


def test_static_tail_delta_requires_a_byte_identical_tail() -> None:
    # SABOTAGE / safety: if the trailing block is NOT byte-identical (a genuinely moved
    # or mutated tail), the static-tail rung MUST decline -> full prefix_mismatch. This
    # is the "reintroduce a moving tail" sabotage: change T and the delta collapses.
    prior = _m("sys", "head", "turn0", "TAIL")
    new = _m("sys", "head", "turn0", "turn1", "TAIL_CHANGED")
    plan = st.classify_delta(prior, new)
    assert plan.mode == "full"
    assert plan.reason == "prefix_mismatch"


def test_static_tail_contract_can_be_disabled() -> None:
    # With static_tail_len=0 the contract narrows to a PURE strict prefix, so the same
    # moving-tail wire declines — proving the tolerance is the (typed) reason it engages.
    prior = _m("sys", "head", "turn0", "TAIL")
    new = _m("sys", "head", "turn0", "turn1", "TAIL")
    plan = st.classify_delta(prior, new, static_tail_len=0)
    assert plan.mode == "full"
    assert plan.reason == "prefix_mismatch"


def test_static_tail_delta_still_requires_a_growing_body() -> None:
    # A resample (same body, same tail) is NOT a delta even under the static-tail rung —
    # there is no appended body to send.
    prior = _m("sys", "head", "turn0", "TAIL")
    plan = st.classify_delta(prior, _m("sys", "head", "turn0", "TAIL"))
    assert plan.mode == "full"
    assert plan.reason == "prefix_mismatch"


def test_delta_beneath_static_tail_helper_matrix() -> None:
    # Direct unit-proof of the pure helper.
    prior = _m("a", "b", "T")
    new = _m("a", "b", "c", "T")
    assert st._delta_beneath_static_tail(prior, new, 1) == (2, _m("c"))
    # tail_len 0 falls back to the pure strict-prefix (which fails here — T moved).
    assert st._delta_beneath_static_tail(prior, new, 0) is None
    # A diverged head body fails even with an identical tail.
    assert st._delta_beneath_static_tail(_m("a", "X", "T"), new, 1) is None


# --------------------------------------------------------------------------- #
# 3. Reason-catalog discipline (#775 no-silent-fallback).
# --------------------------------------------------------------------------- #
def test_reset_payload_is_typed_and_rejects_unknown_reasons() -> None:
    payload = st.stateful_reset_payload("ops_reset", "compacted")
    assert payload["reason"] == "ops_reset"
    assert payload["category"] == "stateful_reset"
    assert payload["message"] == "compacted"
    with pytest.raises(ValueError, match="Unknown stateful reset reason"):
        st.stateful_reset_payload("not_a_reason")


def test_reset_catalog_covers_the_declared_reasons() -> None:
    assert set(st.STATEFUL_RESET_REASONS) == {
        "first_call",
        "prefix_mismatch",
        "ops_reset",
        "session_evicted",
        "provider_error",
    }


def test_mark_reset_rejects_unknown_reason() -> None:
    reg = st.StatefulSessionRegistry(capacity=8)
    with pytest.raises(ValueError, match="Unknown stateful reset reason"):
        reg.mark_reset("scopeX", "bogus")


# --------------------------------------------------------------------------- #
# 4. The bounded, per-loop session registry.
# --------------------------------------------------------------------------- #
def _key(scope: str) -> tuple[Any, ...]:
    return (scope, "haiku", "/w", None)


def test_registry_first_call_then_delta_reuses_one_session() -> None:
    reg = st.StatefulSessionRegistry(capacity=8)
    p1, sid1 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
    assert p1.mode == "full" and p1.reason == "first_call"
    p2, sid2 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c"))
    assert p2.mode == "delta"
    assert p2.messages == _m("c")
    assert sid2 == sid1  # the SAME stable session id across the delta run


def test_registry_prefix_mismatch_restarts_with_fresh_session() -> None:
    reg = st.StatefulSessionRegistry(capacity=8)
    _p1, sid1 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
    p2, sid2 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "ZZ"))
    assert p2.mode == "full" and p2.reason == "prefix_mismatch"
    assert sid2 != sid1  # a fresh session id on the restart


def test_registry_ops_reset_forces_full_even_on_a_valid_extension() -> None:
    # The ops-reset -> restart mapping at the REGISTRY layer (the pinned test for
    # sabotage (b)). Open a session, delta once, then flag an ARC op: the NEXT call
    # is a full ops_reset send even though the messages ARE a prefix-extension.
    reg = st.StatefulSessionRegistry(capacity=8)
    _p1, sid1 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
    p2, _sid2 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c"))
    assert p2.mode == "delta"
    reg.mark_reset("s", "ops_reset")
    p3, sid3 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c", "d"))
    assert p3.mode == "full"
    assert p3.reason == "ops_reset"
    assert sid3 != sid1
    # SABOTAGE (b): remove the `forced = self._pending.pop(...)` handling in
    # `StatefulSessionRegistry.plan` -> p3 becomes a delta -> red.


def test_registry_provider_error_drops_session_and_flags_next_call() -> None:
    reg = st.StatefulSessionRegistry(capacity=8)
    _p1, _sid1 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
    reg.note_provider_error(_key("s"), "s")
    p2, _sid2 = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c"))
    assert p2.mode == "full"
    assert p2.reason == "provider_error"


def test_registry_lru_eviction_flags_session_evicted() -> None:
    reg = st.StatefulSessionRegistry(capacity=1)
    # Open scope A, then scope B — B evicts A (capacity 1).
    reg.plan(session_key=_key("A"), scope_token="A", messages=_m("a"))
    reg.plan(session_key=_key("B"), scope_token="B", messages=_m("b"))
    assert reg.live_count == 1
    # A's next call sees its entry gone and is flagged session_evicted (not a bare
    # first_call), so the eviction is a queryable reason.
    pA, _ = reg.plan(session_key=_key("A"), scope_token="A", messages=_m("a", "a2"))
    assert pA.mode == "full"
    assert pA.reason == "session_evicted"


def test_registry_parallel_scopes_never_share_a_session() -> None:
    # Two experts (distinct scope tokens) with byte-identical message lists must get
    # DISTINCT sessions — no cross-expert bleed.
    reg = st.StatefulSessionRegistry(capacity=8)
    _pA, sidA = reg.plan(session_key=_key("A"), scope_token="A", messages=_m("a", "b"))
    _pB, sidB = reg.plan(session_key=_key("B"), scope_token="B", messages=_m("a", "b"))
    assert sidA != sidB
    # And each continues its OWN session on a delta.
    pA2, sidA2 = reg.plan(session_key=_key("A"), scope_token="A", messages=_m("a", "b", "c"))
    assert pA2.mode == "delta" and sidA2 == sidA


def test_registry_release_drops_scope_state() -> None:
    reg = st.StatefulSessionRegistry(capacity=8)
    reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
    assert reg.live_count == 1
    reg.release("s")
    assert reg.live_count == 0
    # A pending flag for a released scope is cleared too.
    reg.mark_reset("s", "ops_reset")
    reg.release("s")
    p, _ = reg.plan(session_key=_key("s"), scope_token="s", messages=_m("a"))
    assert p.reason == "first_call"  # not the stale ops_reset


# --------------------------------------------------------------------------- #
# 5. The scope contextmanager (per-forward token + teardown).
# --------------------------------------------------------------------------- #
def test_stateful_scope_sets_and_releases() -> None:
    assert st.active_stateful_scope() is None
    with st.stateful_scope("tok") as token:
        assert token == "tok"
        assert st.active_stateful_scope() == "tok"
        st.stateful_registry().plan(session_key=_key("tok"), scope_token="tok", messages=_m("a"))
        assert st.stateful_registry().live_count == 1
    # Exiting releases the contextvar AND the scope's registry entries (#900 teardown).
    assert st.active_stateful_scope() is None
    assert st.stateful_registry().live_count == 0


def test_stateful_scopes_nest_and_restore() -> None:
    with st.stateful_scope("outer"):
        assert st.active_stateful_scope() == "outer"
        with st.stateful_scope("inner"):
            assert st.active_stateful_scope() == "inner"
        assert st.active_stateful_scope() == "outer"
    assert st.active_stateful_scope() is None


def test_note_prefix_reset_for_active_scope_hook() -> None:
    # No-op (False) off-scope; flags the active scope on-scope.
    assert st.note_prefix_reset_for_active_scope() is False
    with st.stateful_scope("s"):
        st.stateful_registry().plan(session_key=_key("s"), scope_token="s", messages=_m("a", "b"))
        assert st.note_prefix_reset_for_active_scope("ops_reset") is True
        p, _ = st.stateful_registry().plan(
            session_key=_key("s"), scope_token="s", messages=_m("a", "b", "c")
        )
        assert p.mode == "full" and p.reason == "ops_reset"


# --------------------------------------------------------------------------- #
# 6. resolve_stateful_send — the transport seam (inert vs engaged).
# --------------------------------------------------------------------------- #
def test_resolve_stamps_a_call_id_for_the_ttft_join(monkeypatch: pytest.MonkeyPatch) -> None:
    # #901 join hygiene: every resolved send carries a call_id (engaged AND inert) so the
    # provider.stateful row and the emit_call_started TTFT marker join on ONE id. The
    # transport reuses send.call_id for emit_call_started (proven in _astream_sdk).
    monkeypatch.setattr(st, "stateful_delta_enabled", lambda: True)
    with st.stateful_scope("s"):
        engaged = st.resolve_stateful_send(
            messages=_m("a", "b"), full_prompt="FULL", model="haiku", cwd="/w",
            thinking=None, serialize=_serialize,
        )
    inert = st.resolve_stateful_send(
        messages=_m("a", "b"), full_prompt="FULL", model="haiku", cwd="/w",
        thinking=None, serialize=_serialize,
    )
    assert engaged.call_id and inert.call_id
    assert engaged.call_id != inert.call_id  # one fresh id per LM call


def test_resolve_is_inert_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(st, "stateful_delta_enabled", lambda: False)
    with st.stateful_scope("s"):
        send = st.resolve_stateful_send(
            messages=_m("a", "b"),
            full_prompt="FULL",
            model="haiku",
            cwd="/w",
            thinking=None,
            serialize=_serialize,
        )
    assert send.engaged is False
    assert send.mode == "full"
    assert send.payload == "FULL"  # the full prompt, unchanged
    assert send.reason is None


def test_resolve_is_inert_without_a_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(st, "stateful_delta_enabled", lambda: True)
    # No active scope (classic loop) -> inert even with the flag on.
    send = st.resolve_stateful_send(
        messages=_m("a", "b"),
        full_prompt="FULL",
        model="haiku",
        cwd="/w",
        thinking=None,
        serialize=_serialize,
    )
    assert send.engaged is False
    assert send.payload == "FULL"


def test_resolve_engages_delta_when_flag_on_and_scope_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(st, "stateful_delta_enabled", lambda: True)
    with st.stateful_scope("s"):
        first = st.resolve_stateful_send(
            messages=_m("a", "b"),
            full_prompt=_serialize(_m("a", "b")),
            model="haiku",
            cwd="/w",
            thinking=None,
            serialize=_serialize,
        )
        assert first.engaged is True and first.mode == "full" and first.reason == "first_call"
        second = st.resolve_stateful_send(
            messages=_m("a", "b", "c"),
            full_prompt=_serialize(_m("a", "b", "c")),
            model="haiku",
            cwd="/w",
            thinking=None,
            serialize=_serialize,
        )
    assert second.mode == "delta"
    assert second.payload == _serialize(_m("c"))  # ONLY the tail serialized
    assert second.delta_chars == len(_serialize(_m("c")))
    assert second.session_id == first.session_id  # same stable session id


# --------------------------------------------------------------------------- #
# 7. Live transport pins (_astream_sdk + a fake SDK client).
# --------------------------------------------------------------------------- #
def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"clients": []}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeStreamEvent:
        def __init__(self, event: dict[str, Any]) -> None:
            self.event = event

    class FakeAssistantMessage:
        def __init__(self) -> None:
            self.content = [FakeTextBlock("Answer")]
            self.usage = {"input_tokens": 2, "output_tokens": 3}
            self.stop_reason = "end_turn"

    class FakeResultMessage:
        usage = {"input_tokens": 2, "output_tokens": 3}
        stop_reason = "end_turn"
        result = "Answer"
        is_error = False

    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClaudeSDKClient:
        def __init__(self, options: FakeClaudeAgentOptions) -> None:
            self.options = options
            self.queries: list[tuple[str, str]] = []
            state["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            self.queries.append((prompt, session_id))

        async def receive_response(self) -> Any:
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Answer"}}
            )
            yield FakeAssistantMessage()
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = FakeAssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeClaudeAgentOptions
    fake_sdk.ClaudeSDKClient = FakeClaudeSDKClient
    fake_sdk.ResultMessage = FakeResultMessage
    fake_sdk.StreamEvent = FakeStreamEvent
    fake_sdk.TextBlock = FakeTextBlock
    import sys  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


def _all_queries(state: dict[str, Any]) -> list[tuple[str, str]]:
    return [q for client in state["clients"] for q in client.queries]


async def _drain(prompt: str, send: st.StatefulSend) -> None:
    async for _ in claude_code_litellm._astream_sdk(
        prompt=prompt, model="haiku", timeout=5.0, cwd="/w", send=send
    ):
        pass


async def test_astream_sdk_sends_delta_over_a_stable_session_when_engaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(st, "stateful_delta_enabled", lambda: True)
    m1, m2 = _m("a", "b"), _m("a", "b", "c")
    with st.stateful_scope("s"):
        s1 = st.resolve_stateful_send(
            messages=m1, full_prompt=_serialize(m1), model="haiku", cwd="/w",
            thinking=None, serialize=_serialize,
        )
        await _drain(_serialize(m1), s1)
        s2 = st.resolve_stateful_send(
            messages=m2, full_prompt=_serialize(m2), model="haiku", cwd="/w",
            thinking=None, serialize=_serialize,
        )
        await _drain(_serialize(m2), s2)
    queries = _all_queries(state)
    assert len(queries) == 2
    # Call 1: full prompt under session S. Call 2: ONLY the delta tail, SAME session.
    assert queries[0] == (_serialize(m1), s1.session_id)
    assert queries[1] == (_serialize(_m("c")), s1.session_id)


async def test_astream_sdk_classic_path_sends_full_prompt_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The classic-contract pin (sabotage (c)). Flag OFF -> send is inert -> the wire
    # is the FULL prompt under a FRESH session id, byte-for-byte the pre-#901 path.
    state = _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(st, "stateful_delta_enabled", lambda: False)
    m1, m2 = _m("a", "b"), _m("a", "b", "c")
    with st.stateful_scope("s"):
        s1 = st.resolve_stateful_send(
            messages=m1, full_prompt=_serialize(m1), model="haiku", cwd="/w",
            thinking=None, serialize=_serialize,
        )
        await _drain(_serialize(m1), s1)
        s2 = st.resolve_stateful_send(
            messages=m2, full_prompt=_serialize(m2), model="haiku", cwd="/w",
            thinking=None, serialize=_serialize,
        )
        await _drain(_serialize(m2), s2)
    queries = _all_queries(state)
    # Both send the FULL prompt; sessions are DISTINCT (fresh per call).
    assert queries[0][0] == _serialize(m1)
    assert queries[1][0] == _serialize(m2)  # NOT the delta tail
    assert queries[0][1] != queries[1][1]
    # SABOTAGE (c): make `resolve_stateful_send` engage regardless of the flag -> the
    # second query becomes the delta tail under a stable id -> red.


async def test_astream_sdk_provider_error_drops_the_stateful_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A mid-flight transport death drops the poisoned session so the retried call is a
    # typed provider_error full resend (bounded by the LM retry layer).
    monkeypatch.setattr(st, "stateful_delta_enabled", lambda: True)
    with st.stateful_scope("s"):
        s1 = st.resolve_stateful_send(
            messages=_m("a", "b"), full_prompt=_serialize(_m("a", "b")), model="haiku",
            cwd="/w", thinking=None, serialize=_serialize,
        )
        assert s1.mode == "full" and s1.reason == "first_call"
        s1.note_error()  # simulate the transport's mid-flight failure hook
        s2 = st.resolve_stateful_send(
            messages=_m("a", "b", "c"), full_prompt=_serialize(_m("a", "b", "c")),
            model="haiku", cwd="/w", thinking=None, serialize=_serialize,
        )
    assert s2.mode == "full"
    assert s2.reason == "provider_error"
    assert s2.session_id != s1.session_id


# --------------------------------------------------------------------------- #
# live: the real mid-loop delta send against the real SDK/API (task #58 /
# #1211 A4 -- "the layer-3 SDK 400"). CLIO_RUN_LIVE=1 only; a real billed call.
# --------------------------------------------------------------------------- #


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live claude_code SDK stateful-delta probe: set CLIO_RUN_LIVE=1 "
    "(needs `claude` on PATH + `claude login`; 2 billed API calls)",
)
async def test_live_mid_loop_delta_send_does_not_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """iowarp/clio-agent#1211 A4 (task #58, "the layer-3 SDK 400"): drive the
    claude_code SDK streaming path with a real mid-loop delta send (turn 2
    continues turn 1's session with only the appended messages) and confirm it
    does NOT 400. Verified live 2026-08-14 against claude-agent-sdk 0.2.128 +
    CLI 2.1.228: both turns succeed (turn 1 "full"/first_call, turn 2 "delta"
    reusing the same session_id, confirmed via the provider.stateful stream-
    audit row) -- CLEARED. The fix that makes this work is
    ``build_sdk_options``'s ``max_turns=0`` (unlimited assistant turns per SDK
    session; a stale ``max_turns=1`` would reject exactly this second-call
    shape with ``error_max_turns`` -- see that function's docstring, the
    AGENT-COPPER14 finding), NOT cc44a593's unrelated POST /messages
    empty-body retag (a different layer: gact's own route validation, not the
    provider/SDK boundary).
    """
    monkeypatch.setattr(st, "stateful_delta_enabled", lambda: True)
    handler = claude_code_litellm.ClaudeCodeLLM()
    claude_code_litellm.ensure_registered()

    turn1 = [{"role": "user", "content": "Probe turn 1: what is 2+2? Reply with just the digit."}]
    turn2 = turn1 + [
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "Probe turn 2: what is 3+3? Reply with just the digit."},
    ]

    async def _drive(messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        async for chunk in handler.astreaming(
            model="claude_code/haiku",
            messages=messages,
            api_base="",
            custom_prompt_dict={},
            model_response=None,
            print_verbose=lambda *_: None,
            encoding=None,
            api_key=None,
            logging_obj=None,
            optional_params={"claude_code_transport": "sdk"},
        ):
            if chunk.get("text"):
                parts.append(chunk["text"])
        return "".join(parts)

    with st.stateful_scope():
        text1 = await _drive(turn1)
        text2 = await _drive(turn2)  # the mid-loop DELTA send -- the failing-first target

    assert text1.strip()
    assert text2.strip()
