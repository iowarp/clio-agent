"""Retained independent elicitation algorithms; lifecycle tests use the replacement owners."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import agent_elicitation as ae
from clio_agent.gact.agent_elicitation_reply import answer_field_text
from clio_agent.gact.parts import Part
from clio_agent.gact.types import Message


def test_audience_hint_reads_the_declared_meta_key() -> None:
    params = SimpleNamespace(meta={ae.AGENT_AUDIENCE_META_KEY: "agent"})
    assert ae.audience_hint(params) == "agent"


def test_audience_hint_absent_meta_is_empty() -> None:
    assert ae.audience_hint(SimpleNamespace(meta=None)) == ""
    assert ae.audience_hint(SimpleNamespace()) == ""


def test_audience_hint_ignores_unrelated_meta_keys() -> None:
    params = SimpleNamespace(meta={"progress_token": "abc"})
    assert ae.audience_hint(params) == ""


def test_audience_hint_is_case_and_whitespace_normalized() -> None:
    params = SimpleNamespace(meta={ae.AGENT_AUDIENCE_META_KEY: "  AGENT  "})
    assert ae.audience_hint(params) == "agent"


class _FakeSessions:
    def __init__(self, sessions: dict[str, Any]) -> None:
        self._sessions = sessions

    def get(self, sid: str) -> Any:
        return self._sessions.get(sid)


def _fake_app(sessions: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(state=SimpleNamespace(sessions=_FakeSessions(sessions or {})))


def test_decide_routing_unhinted_form_defaults_to_agent_by_contract() -> None:
    """fastmcp drops the InputRequiredResult ``_meta`` (2026-09-07), so a
    guard-style server's audience tag never arrives. Because clio advertises the
    agent-driven-elicitation extension on every execution client, an unhinted FORM
    elicitation reaching the handler is agent-directed by that contract and routes
    to the agent under the typed unhinted reason (default ON)."""

    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    decision = ae.decide_routing(app, mode="form", session_id="sid1", namespace="v2ex", audience="")
    assert decision.route is True
    assert decision.reason == ae.ROUTED_UNHINTED_REASON
    assert decision.depth == 1


def test_decide_routing_unhinted_empty_namespace_still_fails_closed() -> None:
    """An unhinted elicitation with no identifiable server namespace fails closed."""

    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    decision = ae.decide_routing(app, mode="form", session_id="sid1", namespace="", audience="")
    assert decision.route is False
    assert decision.detail == "unknown_server"


def test_decide_routing_strict_mode_makes_unhinted_a_pure_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``default_unhinted`` OFF, an unhinted elicitation is the byte-identical
    pre-2026 no-op again (strict explicit-tag routing)."""

    monkeypatch.setattr(ae, "_default_unhinted", lambda: False)
    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    decision = ae.decide_routing(app, mode="form", session_id="sid1", namespace="v2ex", audience="")
    assert decision.route is False
    assert decision.reason == ""
    assert ae.routing_fields(decision) == {}


def test_decide_routing_unrecognized_audience_value_is_also_a_noop() -> None:
    app = _fake_app()
    decision = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="", audience="human"
    )
    assert decision.route is False
    assert decision.reason == ""


def test_decide_routing_routes_when_audience_agent_and_policy_allows() -> None:
    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    decision = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="v2ex", audience="agent"
    )
    assert decision.route is True
    assert decision.reason == ae.ROUTED_REASON
    assert decision.depth == 1
    assert ae.routing_fields(decision) == {"agent_elicitation_routing": ae.ROUTED_REASON}


def test_decide_routing_no_session_falls_back_typed() -> None:
    app = _fake_app()
    decision = ae.decide_routing(app, mode="form", session_id="", namespace="", audience="agent")
    assert decision.route is False
    assert decision.reason == ae.FALLBACK_REASON
    assert decision.detail == "no_session"


def test_decide_routing_url_mode_never_routes_even_with_audience_agent() -> None:
    """url-mode consent must always come from the human (never an LM's answer)."""

    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    # A KNOWN namespace, so this pins the url-mode check specifically -- the
    # empty/unknown-namespace case is its own fail-closed test (F2).
    decision = ae.decide_routing(
        app, mode="url", session_id="sid1", namespace="v2ex", audience="agent"
    )
    assert decision.route is False
    assert decision.reason == ae.FALLBACK_REASON
    assert decision.detail == "url_mode_requires_human_consent"


def test_decide_routing_policy_disabled_falls_back_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ae, "_enabled", lambda: False)
    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    decision = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="", audience="agent"
    )
    assert decision.route is False
    assert decision.detail == "policy_disabled"


def test_decide_routing_denied_server_falls_back_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ae, "_denied_servers", lambda: frozenset({"v2ex"}))
    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    decision = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="v2ex", audience="agent"
    )
    assert decision.route is False
    assert decision.detail == "policy_denied_server"
    # An UNDENIED namespace on the same deny-list config still routes.
    decision2 = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="other", audience="agent"
    )
    assert decision2.route is True


def test_decide_routing_recursion_depth_exceeded_falls_back_typed() -> None:
    """A session already at the max agent-elicitation depth never routes again —
    the recursion/convergence-safety guard (item 4 of the design)."""

    app = _fake_app({"sid1": SimpleNamespace(metadata={"agent_elicitation_depth": 1})})
    decision = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="v2ex", audience="agent"
    )
    assert decision.route is False
    assert decision.detail == "recursion_depth_exceeded"


def test_decide_routing_max_depth_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ae, "_max_depth", lambda: 3)
    app = _fake_app({"sid1": SimpleNamespace(metadata={"agent_elicitation_depth": 1})})
    decision = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="v2ex", audience="agent"
    )
    assert decision.route is True
    assert decision.depth == 2


def test_parse_agent_reply_accepts_answer_object() -> None:
    parsed = ae.parse_agent_reply('{"answer": {"nonce": "xyz"}}')
    assert parsed == {"answer": {"nonce": "xyz"}}


def test_parse_agent_reply_accepts_decline() -> None:
    parsed = ae.parse_agent_reply('{"decline": true, "reason": "no context"}')
    assert parsed is not None
    assert parsed.get("decline") is True


def test_parse_agent_reply_tolerates_fenced_code_block() -> None:
    text = '```json\n{"answer": {"nonce": "xyz"}}\n```'
    assert ae.parse_agent_reply(text) == {"answer": {"nonce": "xyz"}}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I cannot answer that.",
        "the nonce is xyz",
        "{}",
        '{"unexpected_key": 1}',
        "not json at all {{{",
        '["answer", "not", "an", "object"]',
    ],
)
def test_parse_agent_reply_rejects_anything_not_the_declared_contract(text: str) -> None:
    """Never a keyword/phrase scrape (superseding principle #1): a chatty prose
    reply, an empty answer, or a wrong JSON shape is ``None`` — a typed
    fallback, never a guessed answer."""

    assert ae.parse_agent_reply(text) is None


def test_parse_agent_reply_extracts_json_prefaced_by_reasoning_prose() -> None:
    """The exact live #1309 C1-S7 shape (leg_c2_verdict.json's agent-elicitation
    avenue): a CoT expert's genuinely-visible ``reasoning`` text lands directly
    ahead of the declared JSON ``answer`` with no separator. STRUCTURAL
    balanced-brace extraction recovers the JSON without interpreting the
    prose."""

    text = (
        "I need to remember this nonce and call the tool with no arguments, "
        'then report exactly what it returns.{"answer": {"nonce": "xyz-42"}}'
    )
    assert ae.parse_agent_reply(text) == {"answer": {"nonce": "xyz-42"}}


def test_parse_agent_reply_extracts_json_followed_by_trailing_prose() -> None:
    text = '{"answer": {"nonce": "xyz-42"}} Let me know if that is not correct.'
    assert ae.parse_agent_reply(text) == {"answer": {"nonce": "xyz-42"}}


def test_parse_agent_reply_ignores_braces_inside_json_string_values() -> None:
    """A ``}``/``{`` inside a quoted string value must never perturb the
    balanced-depth count (the structural, never-regex guarantee)."""

    text = 'prose before {"answer": {"nonce": "a{b}c"}} prose after'
    assert ae.parse_agent_reply(text) == {"answer": {"nonce": "a{b}c"}}


@pytest.mark.parametrize(
    "text",
    [
        "I cannot answer that, sorry.",
        "the nonce is xyz, no braces here",
        "unbalanced start {{{ still no closing",
    ],
)
def test_parse_agent_reply_prose_without_a_valid_block_stays_unparseable(text: str) -> None:
    """Prose that never contains a genuinely balanced top-level ``{...}``
    block is STILL a typed fallback -- the extraction only locates a
    structurally valid candidate, it never fabricates one."""

    assert ae.parse_agent_reply(text) is None


def _text_part(field: str, text: str) -> Part:
    return Part(type="text", text=text, agent_id="main", metadata={"signature_field_name": field})


def _answer_message(msg_id: str, sid: str, parts: list[Part]) -> Message:
    now = "2026-09-03T00:00:00+00:00"
    return Message(
        id=msg_id,
        session_id=sid,
        turn_id="turn1",
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=parts,
    )


def test_answer_field_text_ignores_reasoning_and_reads_only_the_answer_part() -> None:
    """The exact root-cause reproduction: a CoT expert's genuinely-visible
    ``reasoning`` part lands in the SAME message as the ``answer`` part (#878
    -- reasoning is never suppressed for chain_of_thought). Reading only the
    ``answer``-tagged part(s) never picks up the reasoning prose, unlike the
    generic ``_message_text``/``_flatten_message_text`` whole-message join."""

    answer_json = '{"answer": {"nonce": "xyz-42"}}'
    msg = _answer_message(
        "msg_a",
        "sid1",
        [
            _text_part("reasoning", "I need to remember this nonce and report it."),
            _text_part("answer", answer_json),
        ],
    )
    app = SimpleNamespace(state=SimpleNamespace(messages={"sid1": [msg]}))
    assert answer_field_text(app, "sid1", "msg_a") == answer_json


def test_answer_field_text_is_untruncated_behind_a_long_reasoning_block() -> None:
    """The truncation-interaction disposition: this reads the ``answer``-field
    part DIRECTLY, so a long preceding ``reasoning`` block can never truncate a
    legitimately long JSON answer -- unlike ``turn_spawn.py``'s generic
    ``answer_excerpt`` (a 2000-char cap over the WHOLE joined message text)."""

    historical_excerpt_bound = 2000

    long_reasoning = "reasoning prose " * 300  # far exceeds _ANSWER_EXCERPT_MAX (2000 chars)
    assert len(long_reasoning) > historical_excerpt_bound
    long_code_word = "x" * 2200  # a valid answer alone longer than the generic cap
    answer_json = json.dumps({"answer": {"code_word": long_code_word}})
    msg = _answer_message(
        "msg_b",
        "sid1",
        [_text_part("reasoning", long_reasoning), _text_part("answer", answer_json)],
    )
    app = SimpleNamespace(state=SimpleNamespace(messages={"sid1": [msg]}))

    result = answer_field_text(app, "sid1", "msg_b")

    assert result == answer_json
    parsed = ae.parse_agent_reply(result)
    assert parsed == {"answer": {"code_word": long_code_word}}


def test_answer_field_text_unknown_message_ref_is_empty() -> None:
    app = SimpleNamespace(state=SimpleNamespace(messages={"sid1": []}))
    assert answer_field_text(app, "sid1", "msg_missing") == ""


def test_answer_field_text_empty_ref_is_empty() -> None:
    app = SimpleNamespace(state=SimpleNamespace(messages={}))
    assert answer_field_text(app, "sid1", "") == ""


def test_apply_session_tool_allowlist_is_a_noop_without_the_stamped_metadata() -> None:
    """A session with no ``tool_allowlist`` metadata (every session but an
    agent-elicitation answer child) is untouched -- the regression lock for
    the F1 mechanism itself."""

    from clio_agent.gact.agents import resolution as _resolution
    from clio_agent.gact.types import AgentDef

    app: Any = SimpleNamespace(
        state=SimpleNamespace(
            sessions=_FakeSessions(
                {"sid1": SimpleNamespace(metadata={}, agent=SimpleNamespace(id="standard"))}
            )
        )
    )
    row = AgentDef(
        id="standard", title="CLIO", module={"kind": "react"}, tools=["fs_read", "shell_exec"]
    )
    out = _resolution.apply_session_tool_allowlist(app, row, session_id="sid1")
    assert out.tools == ["fs_read", "shell_exec"]
    assert out.module == {"kind": "react"}


def test_decide_routing_unknown_namespace_fails_closed() -> None:
    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    decision = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="", audience="agent"
    )
    assert decision.route is False
    assert decision.reason == ae.FALLBACK_REASON
    assert decision.detail == "unknown_server"


def test_decide_routing_known_namespace_still_routes_when_not_denied() -> None:
    app = _fake_app({"sid1": SimpleNamespace(metadata={})})
    decision = ae.decide_routing(
        app, mode="form", session_id="sid1", namespace="v2ex", audience="agent"
    )
    assert decision.route is True
