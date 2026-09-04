"""Agent-driven elicitation (#1309, C1-S7) — failing-first per behavior.

Covers: the audience-hint signal, the routing decision matrix (policy /
url-mode / recursion depth), the typed routing + fallback events, the
semantic-firewall invariant (a schema-invalid agent answer NEVER reaches the
transition/resolution primitives a real answer would), and the regression
lock (no audience hint => byte-identical to the pre-#1309 elicitation path).

Uses a lightweight fake ``app`` for the pure routing-decision tests (no server
boot needed) and the real :func:`clio_agent.gact.app.build_app` for the
question-store / dispatch-flow tests, matching the house pattern in
``test_elicitation_hitl.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM
from fastapi.testclient import TestClient
from fastmcp import Context, FastMCP

from clio_agent.gact import agent_elicitation as ae
from clio_agent.gact.app import build_app
from clio_agent.gact.elicitation_bridge import (
    claim_question_transition,
    make_elicitation_client,
)
from clio_agent.gact.parts import Part
from clio_agent.gact.types import Message, UserQuestion
from clio_agent.tools.mcp_handlers import MCPInvocationContext

# --------------------------------------------------------------------------- #
# audience_hint — pure, total, precise                                       #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# decide_routing — the routing matrix (pure function, fake app)               #
# --------------------------------------------------------------------------- #


class _FakeSessions:
    def __init__(self, sessions: dict[str, Any]) -> None:
        self._sessions = sessions

    def get(self, sid: str) -> Any:
        return self._sessions.get(sid)


def _fake_app(sessions: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(state=SimpleNamespace(sessions=_FakeSessions(sessions or {})))


def test_decide_routing_no_audience_hint_is_a_pure_noop() -> None:
    """Regression lock: absent/unrecognized audience never routes and never
    records a reason — the byte-identical-today case."""

    app = _fake_app()
    decision = ae.decide_routing(app, mode="form", session_id="sid1", namespace="", audience="")
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


# --------------------------------------------------------------------------- #
# _parse_agent_reply — structural JSON parsing, never a prose scrape          #
# --------------------------------------------------------------------------- #


def test_parse_agent_reply_accepts_answer_object() -> None:
    parsed = ae._parse_agent_reply('{"answer": {"nonce": "xyz"}}')
    assert parsed == {"answer": {"nonce": "xyz"}}


def test_parse_agent_reply_accepts_decline() -> None:
    parsed = ae._parse_agent_reply('{"decline": true, "reason": "no context"}')
    assert parsed is not None
    assert parsed.get("decline") is True


def test_parse_agent_reply_tolerates_fenced_code_block() -> None:
    text = '```json\n{"answer": {"nonce": "xyz"}}\n```'
    assert ae._parse_agent_reply(text) == {"answer": {"nonce": "xyz"}}


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

    assert ae._parse_agent_reply(text) is None


# --------------------------------------------------------------------------- #
# _parse_agent_reply — STRUCTURAL balanced-block extraction (the live-red     #
# fix's defense-in-depth hardening, #1309). Bracket-depth counting only,      #
# never a keyword/regex scrape of the model's prose -- a candidate located    #
# this way still round-trips through the SAME strict json.loads + answer/    #
# decline Mapping contract as a bare-JSON reply.                             #
# --------------------------------------------------------------------------- #


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
    assert ae._parse_agent_reply(text) == {"answer": {"nonce": "xyz-42"}}


def test_parse_agent_reply_extracts_json_followed_by_trailing_prose() -> None:
    text = '{"answer": {"nonce": "xyz-42"}} Let me know if that is not correct.'
    assert ae._parse_agent_reply(text) == {"answer": {"nonce": "xyz-42"}}


def test_parse_agent_reply_ignores_braces_inside_json_string_values() -> None:
    """A ``}``/``{`` inside a quoted string value must never perturb the
    balanced-depth count (the structural, never-regex guarantee)."""

    text = 'prose before {"answer": {"nonce": "a{b}c"}} prose after'
    assert ae._parse_agent_reply(text) == {"answer": {"nonce": "a{b}c"}}


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

    assert ae._parse_agent_reply(text) is None


# --------------------------------------------------------------------------- #
# _answer_field_text — reads ONLY the answer child's own ``answer``-field     #
# part, untruncated (#1309 fix items 1+2: root cause + truncation disposition)#
# --------------------------------------------------------------------------- #


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
    assert ae._answer_field_text(app, "sid1", "msg_a") == answer_json


def test_answer_field_text_is_untruncated_behind_a_long_reasoning_block() -> None:
    """The truncation-interaction disposition: this reads the ``answer``-field
    part DIRECTLY, so a long preceding ``reasoning`` block can never truncate a
    legitimately long JSON answer -- unlike ``turn_spawn.py``'s generic
    ``answer_excerpt`` (a 2000-char cap over the WHOLE joined message text)."""

    from clio_agent.gact.turn_spawn import _ANSWER_EXCERPT_MAX

    long_reasoning = "reasoning prose " * 300  # far exceeds _ANSWER_EXCERPT_MAX (2000 chars)
    assert len(long_reasoning) > _ANSWER_EXCERPT_MAX
    long_code_word = "x" * 2200  # a valid answer alone longer than the generic cap
    answer_json = json.dumps({"answer": {"code_word": long_code_word}})
    msg = _answer_message(
        "msg_b",
        "sid1",
        [_text_part("reasoning", long_reasoning), _text_part("answer", answer_json)],
    )
    app = SimpleNamespace(state=SimpleNamespace(messages={"sid1": [msg]}))

    result = ae._answer_field_text(app, "sid1", "msg_b")

    assert result == answer_json
    parsed = ae._parse_agent_reply(result)
    assert parsed == {"answer": {"code_word": long_code_word}}


def test_answer_field_text_unknown_message_ref_is_empty() -> None:
    app = SimpleNamespace(state=SimpleNamespace(messages={"sid1": []}))
    assert ae._answer_field_text(app, "sid1", "msg_missing") == ""


def test_answer_field_text_empty_ref_is_empty() -> None:
    app = SimpleNamespace(state=SimpleNamespace(messages={}))
    assert ae._answer_field_text(app, "sid1", "") == ""


# --------------------------------------------------------------------------- #
# The dispatch flow, on the real app — including the SEMANTIC FIREWALL        #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def _create_session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "agent-elicit"}).json()["id"]


def _mint_pending_form_question(app: Any, sid: str) -> UserQuestion:
    """A minimal pending, form-mode, schema-bearing elicitation question — the
    shape :func:`clio_agent.gact.elicitation_bridge.handle_elicitation` mints,
    built directly so these tests exercise the dispatch/firewall logic without
    needing a live threaded MCP tool call."""

    from clio_agent.gact.elicitation_schema import ELICITATION_QUESTION_SOURCE

    now = "2026-09-03T00:00:00+00:00"
    return UserQuestion(
        id="q_test1",
        session_id=sid,
        prompt="What nonce did the user state earlier?",
        status="pending",
        kind="freeform",
        created_at=now,
        updated_at=now,
        source=ELICITATION_QUESTION_SOURCE,
        audience="agent",
        metadata={
            "elicitation": {
                "mode": "form",
                "fields": [
                    {
                        "name": "nonce",
                        "type": "string",
                        "enum": None,
                        "multi": False,
                        "required": True,
                        "title": "nonce",
                        "description": "",
                        "default": None,
                    }
                ],
                "additional_properties": True,
            }
        },
    )


def test_agent_answer_passing_schema_resolves_and_attributes_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    question = _mint_pending_form_question(app, sid)
    app.state.user_questions[question.id] = question

    def answer_turn(*args: Any, **kwargs: Any) -> str:
        del args
        kwargs["on_spawn"](SimpleNamespace(task_id="task_answer", child_session_id="sess_answer"))
        return '{"answer": {"nonce": "xyz-42"}}'

    monkeypatch.setattr(ae, "_run_agent_answer_turn", answer_turn)
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="v2ex", tool_name="agent_guarded_input"
    )
    decision = ae.AgentElicitationDecision(route=True, reason=ae.ROUTED_REASON, depth=1)

    asyncio.run(ae._dispatch_agent_answer(app, question, invocation, None, decision))

    updated = app.state.user_questions[question.id]
    assert updated.status == "answered"
    assert updated.answered_by == "agent"
    assert updated.answer_metadata == {"nonce": "xyz-42"}
    assert updated.metadata["agent_answer_task"] == {
        "task_id": "task_answer",
        "child_session_id": "sess_answer",
    }


def test_agent_answer_failing_schema_never_reaches_the_server(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE SEMANTIC FIREWALL (owner ruling, 2026-09-03): an agent answer that
    fails the server's own ``requestedSchema`` must NEVER reach
    ``claim_question_transition``/``resolve_elicitation`` — the question stays
    pending (exactly what a real server would see: nothing), and the fallback
    is typed ``agent_answer_schema_invalid``."""

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    question = _mint_pending_form_question(app, sid)
    app.state.user_questions[question.id] = question

    transition_calls: list[Any] = []
    real_transition = claim_question_transition

    def _spying_transition(*args: Any, **kwargs: Any) -> Any:
        transition_calls.append((args, kwargs))
        return real_transition(*args, **kwargs)

    monkeypatch.setattr(
        "clio_agent.gact.elicitation_bridge.claim_question_transition", _spying_transition
    )
    # The required "nonce" field is MISSING from the agent's answer -- schema-invalid.
    monkeypatch.setattr(ae, "_run_agent_answer_turn", lambda *a, **k: '{"answer": {}}')

    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="v2ex", tool_name="agent_guarded_input"
    )
    decision = ae.AgentElicitationDecision(route=True, reason=ae.ROUTED_REASON, depth=1)

    asyncio.run(ae._dispatch_agent_answer(app, question, invocation, None, decision))

    assert transition_calls == [], "a schema-invalid agent answer must never reach the transition"
    still_pending = app.state.user_questions[question.id]
    assert still_pending.status == "pending"
    assert still_pending.answered_by is None
    assert still_pending.agent_elicitation_routing == ae.FALLBACK_REASON
    assert still_pending.agent_elicitation_fallback_detail == "agent_answer_schema_invalid"


def test_agent_decline_falls_back_to_human_typed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    question = _mint_pending_form_question(app, sid)
    app.state.user_questions[question.id] = question

    monkeypatch.setattr(
        ae, "_run_agent_answer_turn", lambda *a, **k: '{"decline": true, "reason": "unsure"}'
    )
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="v2ex", tool_name="agent_guarded_input"
    )
    decision = ae.AgentElicitationDecision(route=True, reason=ae.ROUTED_REASON, depth=1)

    asyncio.run(ae._dispatch_agent_answer(app, question, invocation, None, decision))

    still_pending = app.state.user_questions[question.id]
    assert still_pending.status == "pending"
    assert still_pending.agent_elicitation_fallback_detail == "agent_declined"


def test_agent_answer_error_falls_back_typed_never_crashes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    question = _mint_pending_form_question(app, sid)
    app.state.user_questions[question.id] = question

    def _boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(ae, "_run_agent_answer_turn", _boom)
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="v2ex", tool_name="agent_guarded_input"
    )
    decision = ae.AgentElicitationDecision(route=True, reason=ae.ROUTED_REASON, depth=1)

    # Must never raise -- the dispatcher fails SAFE, always.
    asyncio.run(ae._dispatch_agent_answer(app, question, invocation, None, decision))

    still_pending = app.state.user_questions[question.id]
    assert still_pending.status == "pending"
    assert still_pending.agent_elicitation_fallback_detail == "agent_answer_error"


def test_human_still_answers_normally_after_a_fallback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human path is NEVER dropped -- a question that fell back to human
    can still be answered exactly as if the feature never existed."""

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    question = _mint_pending_form_question(app, sid)
    app.state.user_questions[question.id] = question
    monkeypatch.setattr(ae, "_run_agent_answer_turn", lambda *a, **k: '{"decline": true}')
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="v2ex", tool_name="agent_guarded_input"
    )
    decision = ae.AgentElicitationDecision(route=True, reason=ae.ROUTED_REASON, depth=1)
    asyncio.run(ae._dispatch_agent_answer(app, question, invocation, None, decision))

    resp = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer",
        json={"metadata": {"nonce": "human-value"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "answered"
    assert body.get("answered_by") in (None, "human")


# --------------------------------------------------------------------------- #
# THE MISSING CONTRACT TEST (live-red fix, C1-S7, #1309, merged @73f2bacf).   #
# Drives ONE REAL answer child turn -- the actual blueprint chain_of_thought  #
# module (module.kind flips there via ad351a59's F1 tool-allowlist mint)     #
# through the REAL spawn/turn machinery -- with a MOCKED dspy LM (house      #
# rule: LM tests mock dspy responses) that ALSO drives the real live-        #
# streaming tap for the reasoning/answer fields, so the real transcript      #
# pipeline mints the SAME shape of message the live #1309 C1-S7 avenue saw   #
# (out/live-verification/leg_c2_verdict.json: agent_elicitation_routing ==   #
# "elicitation_routed_to_agent" -> fallback, detail "agent_answer_           #
# unparseable"). Prior tests only ever fed _parse_agent_reply a hand-written #
# string; this is the reviewer's prescribed missing condition.               #
# --------------------------------------------------------------------------- #


class _RealisticCoTAnswerLM(DummyLM):
    """A DummyLM that ALSO drives the real ``note_lm_answer_delta`` live-stream
    tap for its ``reasoning``/``answer`` fields before returning its formatted
    response, reproducing exactly how a real live-streamed provider call
    (claude_code_sdk in the live #1309 avenue) feeds the turn's transcript:
    per-field text deltas through the SAME production tap
    ``test_react_extract_suppression.py`` drives directly.

    For a ``chain_of_thought`` module (what the F1 tool-allowlist mint flips
    the answer turn to), ``reasoning`` is NEVER suppressed (#878's pinned
    truth table -- it is that expert kind's entire visible conversation), so
    it lands as its own VISIBLE ``text`` part alongside the ``answer`` field's
    own VISIBLE ``text`` part -- two SEPARATE parts, each tagged with its own
    ``signature_field_name``. A reader that joins every ``text`` part of the
    message without regard to which field produced it (the pre-fix
    ``_message_text``/``_flatten_message_text`` behavior) concatenates the
    reasoning prose immediately ahead of the declared JSON answer -- exactly
    the live failure.
    """

    def __init__(self, reasoning: str, answer: str) -> None:
        # A few repeats: harmless if a bounded schema-repair retry ever fires
        # (it shouldn't for a well-formed reply), never a hard StopIteration.
        super().__init__([{"reasoning": reasoning, "answer": answer}] * 3)
        self._reasoning = reasoning
        self._answer = answer

    def forward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
        from clio_agent.runtime.lm_activity import note_lm_answer_delta

        note_lm_answer_delta(self._reasoning, field="reasoning")
        note_lm_answer_delta(self._answer, field="answer")
        return super().forward(prompt=prompt, messages=messages, **kwargs)


def _stub_resolved_lm_spec() -> Any:
    """A minimal ``ResolvedLMSpec`` stand-in: ``.materialize(cred)`` returns a
    plain config object, sidestepping real handshake/credential resolution
    (network) entirely -- the same stub shape proven in
    ``test_module_variants_s5.py::_resolved_spec_stub``."""

    return SimpleNamespace(
        materialize=lambda cred=None: SimpleNamespace(
            provider="argonne", model="gpt-oss-120b", temperature=0.0
        )
    )


def _write_minimal_answer_blueprint(root: Path) -> Path:
    """A minimal, valid Agent Blueprint (root expert id ``main``, declaring
    ``structured_outputs.workflow_state: false`` to keep the runtime signature
    to just ``answer`` -- a DummyLM forward is then trivial, matching
    ``test_module_variants_s5.py``'s ``_build_variant_module`` convention).

    Session-scoped path activation (never the bare/no-blueprint ``main``)
    is DELIBERATE here: a bare session's real turn build takes the
    ``run_builtin_main`` shortcut in ``turn_forward.py``, which constructs a
    fresh ``_builtin_main_agent()`` directly and never reaches
    ``_resolve_runtime_dynamic_agent``/``_apply_session_tool_allowlist`` at
    all -- an unrelated, pre-existing gap this test must route around (out of
    scope for #1309) rather than trip over. An ACTIVATED blueprint (like the
    live #1309 avenue's own ``v2ex-avenues``) always goes through the real
    resolve-and-flip seam.
    """

    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        """---
id: elicit-answer-bp
version: 0.1.0
title: Elicit Answer Test Agent
root_expert: main
---
Minimal test blueprint for the agent-elicitation answer-turn contract test.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "main.md").write_text(
        """---
id: main
title: Main
tier: 1
module:
  kind: predict
structured_outputs:
  workflow_state: false
---
Minimal main expert.
""",
        encoding="utf-8",
    )
    return root / "AGENT.md"


def test_real_answer_turn_reply_parses_through_the_real_message_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED before the fix, GREEN after: a REAL answer child turn's reply must
    parse via :func:`clio_agent.gact.agent_elicitation._parse_agent_reply`.

    Mocks only the dspy LM (house rule) -- the real spawn machinery
    (:func:`agent_elicitation._spawn_agent_answer_turn` ->
    ``InProcessExpertInvoker``/``spawn_child_turn_threadsafe``), the real
    blueprint ``chain_of_thought`` module build, the real ``ChatAdapter``
    parse, and the real transcript/message-part pipeline all run for real.
    """

    reasoning_prose = (
        "I need to remember this nonce and call the tool with no arguments, "
        "then report exactly what it returns."
    )
    nonce = "leg-c2-nonce-f391a49e00d8"
    answer_json = f'{{"answer": {{"nonce": "{nonce}"}}}}'

    monkeypatch.setattr(
        "clio_agent.config.create_lm",
        lambda config: _RealisticCoTAnswerLM(reasoning_prose, answer_json),
    )
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: dspy.ChatAdapter())
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: _stub_resolved_lm_spec(),
    )

    agent_md = _write_minimal_answer_blueprint(tmp_path / "elicit-answer-bp")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace_root),
                "storage_root": str(workspace_root / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions", json={"workspace_id": wid, "title": "agent-elicit-real"}
        ).json()["id"]
        activated = client.post(f"/v1/sessions/{sid}/agent-blueprint", json={"path": str(agent_md)})
        assert activated.status_code == 200, activated.text

        reply_text = ae._run_agent_answer_turn(
            app,
            answer_session_id=sid,
            prompt="What nonce did the user state earlier in this conversation?",
            depth=1,
            timeout_s=20.0,
        )

    parsed = ae._parse_agent_reply(reply_text)
    assert parsed == {"answer": {"nonce": nonce}}, (
        f"the real answer turn's reply text failed to parse: {reply_text!r}"
    )


# --------------------------------------------------------------------------- #
# F1 (owner gate review, BLOCKING) — the answer child gets ZERO tools         #
# --------------------------------------------------------------------------- #


def test_agent_answer_child_resolves_to_an_empty_toolset(client: TestClient) -> None:
    """THE AUTHORITY TEST (owner gate review F1): the answer child turn must
    have NO tool surface at all -- not the declared fleet, not the
    auto-attached tools (create_artifact/plan_exit/write_todos/...).

    Drives the REAL spawn path (:func:`agent_elicitation._spawn_agent_answer_turn`,
    which itself calls the real ``InProcessExpertInvoker``/``spawn_child_turn_
    threadsafe`` machinery) against a BARE session running the built-in ``main``
    agent -- no active blueprint, the worst case: ``main`` declares the full
    ``TOOL_CATALOG`` (``gact/catalog.py::_builtin_main_agent``) and a bare
    session has no per-session overlay to lean on. Then reads back the CHILD's
    resolved toolset through the SAME read route a live client would
    (``GET /v1/agents?session_id=...``), which shares the ONE resolution seam
    (``agents/resolution.py``) the real turn-build path uses -- never a unit
    stub of the resolver.
    """

    from clio_agent.gact.agents import resolution as _resolution

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    # Sanity precondition: the PARENT session's own agent is NOT already
    # tool-less (else this test would vacuously pass no matter what F1 did).
    parent_tools = _resolution._resolve_runtime_dynamic_agent(app, "main", session_id=sid)
    assert parent_tools is not None and parent_tools.tools, (
        "fixture assumption broken: the parent session's own agent must have "
        "a non-empty tool surface for this test to mean anything"
    )

    # spawn_child_turn's _launch stages the child's background turn via
    # ``app.state.turn_runner.spawn``, which needs a BOUND running loop
    # (normally anchored by the app's own lifespan startup, which a bare
    # ``TestClient(app)`` fixture -- used without ``with`` -- never runs).
    # Bind it to this call's own loop, matching what a real request handler
    # already has, so the REAL spawn path runs exactly as it does in
    # production, not a stubbed one.
    async def _spawn() -> Any:
        app.state.turn_runner.bind_loop(asyncio.get_running_loop())
        return ae._spawn_agent_answer_turn(
            app, answer_session_id=sid, prompt="What nonce did the user state earlier?", depth=1
        )

    handle = asyncio.run(_spawn())
    try:
        task = app.state.agent_task_registry.get(handle.task_id)
        assert task is not None
        assert task.project_to_parent is False
        assert not any(
            event.type.startswith("agent.task.") for event in app.state.bus._history.get(sid, ())
        ), "an internal elicitation helper leaked into the attended task feed"
        rows = client.get("/v1/agents", params={"session_id": handle.child_session_id}).json()[
            "agents"
        ]
        child_row = next((r for r in rows if r.get("id") == "main"), None)
        assert child_row is not None, f"the answer child's bound agent row was not found: {rows}"
        assert child_row.get("tools") == [], (
            "the answer child resolved a NON-EMPTY toolset -- F1 requires zero "
            f"tools (declared + auto-attached + skill); got {child_row.get('tools')!r}"
        )
    finally:
        # The spawned child's background turn has no LM bound in this test
        # app; cancel it so it does not linger past the test.
        from clio_agent.gact.agents.invoker import InProcessExpertInvoker

        InProcessExpertInvoker(app).cancel(handle)


def test_apply_session_tool_allowlist_is_a_noop_without_the_stamped_metadata() -> None:
    """A session with no ``tool_allowlist`` metadata (every session but an
    agent-elicitation answer child) is untouched -- the regression lock for
    the F1 mechanism itself."""

    from clio_agent.gact.agents import resolution as _resolution
    from clio_agent.gact.types import AgentDef

    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=_FakeSessions(
                {"sid1": SimpleNamespace(metadata={}, agent=SimpleNamespace(id="main"))}
            )
        )
    )
    row = AgentDef(
        id="main", title="Main", module={"kind": "react"}, tools=["fs_read", "shell_exec"]
    )
    out = _resolution._apply_session_tool_allowlist(app, [row], session_id="sid1")
    assert out[0].tools == ["fs_read", "shell_exec"]
    assert out[0].module == {"kind": "react"}


# --------------------------------------------------------------------------- #
# F2 (owner gate review, should-fix) — an unknown server namespace fails      #
# CLOSED, never open                                                          #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# F4 (owner gate review addendum) — declared minLength/maxLength enforced,   #
# identically for a human and an agent answer                                #
# --------------------------------------------------------------------------- #


def test_over_long_agent_answer_fails_the_schema_firewall_and_falls_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    from clio_agent.gact.elicitation_schema import ELICITATION_QUESTION_SOURCE

    now = "2026-09-03T00:00:00+00:00"
    question = UserQuestion(
        id="q_maxlen",
        session_id=sid,
        prompt="What is the code word?",
        status="pending",
        kind="freeform",
        created_at=now,
        updated_at=now,
        source=ELICITATION_QUESTION_SOURCE,
        audience="agent",
        metadata={
            "elicitation": {
                "mode": "form",
                "fields": [
                    {
                        "name": "code_word",
                        "type": "string",
                        "enum": None,
                        "multi": False,
                        "required": True,
                        "title": "code_word",
                        "description": "",
                        "default": None,
                        "min_length": None,
                        "max_length": 8,
                    }
                ],
                "additional_properties": True,
            }
        },
    )
    app.state.user_questions[question.id] = question
    monkeypatch.setattr(
        ae,
        "_run_agent_answer_turn",
        lambda *a, **k: '{"answer": {"code_word": "way-too-long-a-value"}}',
    )
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="v2ex", tool_name="agent_guarded_input"
    )
    decision = ae.AgentElicitationDecision(route=True, reason=ae.ROUTED_REASON, depth=1)
    asyncio.run(ae._dispatch_agent_answer(app, question, invocation, None, decision))

    still_pending = app.state.user_questions[question.id]
    assert still_pending.status == "pending"
    assert still_pending.agent_elicitation_fallback_detail == "agent_answer_schema_invalid"


def test_over_long_human_answer_is_rejected_with_a_typed_message(client: TestClient) -> None:
    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    from clio_agent.gact.elicitation_schema import ELICITATION_QUESTION_SOURCE

    now = "2026-09-03T00:00:00+00:00"
    question = UserQuestion(
        id="q_maxlen_human",
        session_id=sid,
        prompt="What is the code word?",
        status="pending",
        kind="freeform",
        created_at=now,
        updated_at=now,
        source=ELICITATION_QUESTION_SOURCE,
        metadata={
            "elicitation": {
                "mode": "form",
                "fields": [
                    {
                        "name": "code_word",
                        "type": "string",
                        "enum": None,
                        "multi": False,
                        "required": True,
                        "title": "code_word",
                        "description": "",
                        "default": None,
                        "min_length": None,
                        "max_length": 8,
                    }
                ],
                "additional_properties": True,
            }
        },
    )
    app.state.user_questions[question.id] = question
    resp = client.post(
        f"/v1/sessions/{sid}/questions/{question.id}/answer",
        json={"metadata": {"code_word": "way-too-long-a-value"}},
    )
    assert resp.status_code == 422
    assert "character" in resp.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# Regression lock — no audience hint is byte-identical to pre-#1309 behavior  #
# --------------------------------------------------------------------------- #


def _wait_for_pending_question(
    client: TestClient, sid: str, timeout: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = client.get(f"/v1/sessions/{sid}/questions", params={"status": "pending"}).json()
        questions = rows.get("questions", [])
        if questions:
            return questions[0]
        time.sleep(0.02)
    raise TimeoutError("elicitation question never appeared")


def test_no_audience_hint_mints_a_question_with_no_new_fields(client: TestClient) -> None:
    """Regression lock: a question minted with NO ``_meta`` audience hint carries
    none of the #1309 wire fields at all -- exact key-set parity with today."""

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    backend = FastMCP("plain-elicit-backend")

    @backend.tool
    async def pick(ctx: Context) -> str:
        result = await ctx.elicit("Pick a value", response_type=str)
        return f"action={getattr(result, 'action', None)}"

    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="ext", tool_name="pick"
    )
    client_ctx = make_elicitation_client(app, backend, invocation=invocation)
    holder: dict[str, Any] = {}

    def _worker() -> None:
        async def _call() -> None:
            client_ctx.mode = "legacy"
            async with client_ctx as c:
                await c.call_tool("pick", {})

        try:
            asyncio.run(_call())
        except Exception as exc:  # noqa: BLE001 - surfaced via holder
            holder["error"] = repr(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    question = _wait_for_pending_question(client, sid)
    for key in (
        "audience",
        "answered_by",
        "agent_elicitation_routing",
        "agent_elicitation_fallback_detail",
    ):
        assert key not in question, f"{key!r} leaked onto a no-hint question: {question}"

    client.post(f"/v1/sessions/{sid}/questions/{question['id']}/cancel")
    thread.join(timeout=5.0)
