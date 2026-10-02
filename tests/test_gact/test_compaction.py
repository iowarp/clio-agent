"""Compaction is ONE operation (``gact.compaction.compact_session_context``).

Between-turns behaviour of the manual route (``POST /v1/sessions/{sid}/compact``) and
the auto trigger's thresholds, on a real app over real clio-core: the record row, the
summarizer prompt, repeated compaction, the context frame, rewind, cold reload, typed
failures and skips. The mid-turn placement, events, recall and policy are pinned on the
real loop in ``test_compaction_visible.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.lane_chunking import drop_lane
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import build_app
from clio_agent.gact.context_rollback import rolled_back_compactions
from clio_agent.gact.conversation_projection import model_context_messages
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE
from clio_agent.gact.summarization_record import as_summarization
from clio_agent.gact.types import Message, Part, Tokens

from .test_post_messages import _create_session

_SCOPE = "main"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_message(message_id: str, sid: str, text: str, *, role: str = "user") -> Message:
    now = _now_iso()
    return Message(
        id=message_id,
        session_id=sid,
        role=role,
        created_at=now,
        updated_at=now,
        parts=[Part(id=f"part_{message_id}", type="text", text=text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )


def _record(client: TestClient, sid: str, messages: list[Message]) -> None:
    """Mirror messages onto the agent scope's plane, as a turn records them."""
    arc = client.app.state.arc
    for message in messages:
        text = "\n".join(p.text for p in message.parts if p.type == "text")
        kind = "user" if message.role == "user" else "thought"
        turn = message.turn_id or message.id
        arc.append_segment(sid, _SCOPE, kind, {"text": text}, turn_id=turn)


def _seed(client: TestClient, sid: str, messages: list[Message]) -> None:
    client.app.state.messages[sid] = messages
    client.app.state.message_store.replace_session(sid, messages)
    client.app.state.sessions.update(sid, message_count=len(messages))
    _record(client, sid, messages)


class _CapturingAgent:
    """Fake summarizer LM agent recording every prompt."""

    def __init__(self, summaries: list[str] | None = None) -> None:
        self.prompts: list[str] = []
        self._summaries = list(summaries or [])

    def _run_chat_agent(self, question: str, _session_id: str) -> str:
        self.prompts.append(question)
        return self._summaries.pop(0) if self._summaries else "a summary"

    def _call_with_transient_provider_retries(self, _label: str, call: Callable[[], Any]) -> Any:
        return call()


def _record_of(row: Any) -> Any:
    [part] = row.parts
    record = as_summarization(part)
    assert record is not None, part
    return record


def test_appends_a_record_row_and_keeps_every_row(tmp_path: Path) -> None:
    agent = _CapturingAgent(["the summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "seed text")])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        [done] = resp.json()["compactions"]
        assert (done["trigger"], done["scope"], done["turn_id"]) == ("manual", _SCOPE, "")
        ledger = client.app.state.messages[sid]
        assert [m.id for m in ledger] == ["msg_seed", done["message_id"]]
        assert ledger[0].parts[0].text == "seed text"  # seed row untouched
        record = _record_of(ledger[1])
        assert record.text.startswith("the summary") and record.trigger == "manual"
        assert record.compacted_message_ids == ("msg_seed",)
        assert record.compaction_id == done["compaction_id"]
        on_disk = client.app.state.message_store.load_session(sid)
        assert on_disk is not None and [m.id for m in on_disk] == [m.id for m in ledger]


def test_prompt_is_every_replaced_segment_not_the_last_fifty(tmp_path: Path) -> None:
    agent = _CapturingAgent(["ok"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        rows = [
            _text_message(
                f"msg_{i}", sid, f"ROW-IDENTIFIER-{i}", role="user" if i % 2 else "assistant"
            )
            for i in range(60)
        ]
        _seed(client, sid, rows)

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        [prompt] = agent.prompts
        assert "ROW-IDENTIFIER-0" in prompt and "ROW-IDENTIFIER-59" in prompt


def test_prompt_preserves_session_context_file_inventory(tmp_path: Path) -> None:
    """A summary must not describe an attached dataset as absent: context files live in
    a session registry, so the prompt carries their stable identity."""
    agent = _CapturingAgent(["ok"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "analyze the attached dataset")])
        dataset = tmp_path / "materials_scan_speed_fatigue.csv"
        client.app.state.context_files[sid] = {
            str(dataset): {
                "path": str(dataset),
                "display_path": "fixtures/materials_scan_speed_fatigue.csv",
                "mode": "read",
                "size": 1_337,
                "language": "csv",
            }
        }

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        prompt = agent.prompts[0]
        assert "--- attached session files ---" in prompt
        for fact in ("fixtures/materials_scan_speed_fatigue.csv", "mode=read", "size=1337"):
            assert fact in prompt


def test_repeated_compaction_retains_the_transcript_and_advances_the_record(
    tmp_path: Path,
) -> None:
    agent = _CapturingAgent(["SUMMARY 1", "SUMMARY 2"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "ORIGINAL TRANSCRIPT ROW")])

        first = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert first.status_code == 200, first.text
        first_row = client.app.state.messages[sid][-1].id

        ledger = list(client.app.state.messages[sid])
        after = _text_message("msg_after_first", sid, "AFTER FIRST RECORD", role="assistant")
        ledger.append(after)
        _record(client, sid, [after])
        client.app.state.messages[sid] = ledger
        client.app.state.message_store.replace_session(sid, ledger)

        second = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert second.status_code == 200, second.text

        final = client.app.state.messages[sid]
        assert [p.type for m in final for p in m.parts] == [
            "text",
            "injection",
            "text",
            "injection",
        ]
        assert _record_of(final[-1]).compacted_message_ids == (first_row, "msg_after_first")
        assert model_context_messages(final) == [final[-1]]
        second_prompt = agent.prompts[1]
        assert "SUMMARY 1" in second_prompt and "AFTER FIRST RECORD" in second_prompt
        assert "ORIGINAL TRANSCRIPT ROW" not in second_prompt


def test_context_frame_marks_rows_a_record_covers_excluded(tmp_path: Path) -> None:
    from clio_agent.gact.enrichment import _record_context_frame

    agent = _CapturingAgent(["frame summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        seed = _text_message("msg_seed", sid, "pre-record content")
        _seed(client, sid, [seed])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text

        app = client.app
        frame = _record_context_frame(
            app,
            sid,
            app.state.sessions.get(sid),
            seed,
            user_text="pre-record content",
            enriched_text="pre-record content",
            context_error=None,
        )
        by_id = {i["source_id"]: i for i in frame["items"] if i["kind"] == "message"}
        assert (by_id["msg_seed"]["included"], by_id["msg_seed"]["reason"]) == (False, "compacted")
        record_row = resp.json()["compactions"][0]["message_id"]
        assert by_id[record_row]["included"] is True


def test_auto_trigger_uses_durable_usage_when_new_lm_binding_has_no_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh subscription LM binding (no measured call yet) must not disable
    automatic compaction: the previous turn's durable usage stands in."""
    import clio_agent.gact.agents.clio_react_record as clio_react_record
    import clio_agent.gact.context as gact_context
    import clio_agent.gact.runtime.context_tokens as context_tokens
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.compaction import maybe_autocompact

    agent = _CapturingAgent(["durable usage summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    arc = app.state.arc
    with TestClient(app) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "the question")])
        app.state.sessions.update(
            sid, metadata_patch={"context_usage_by_scope": {_SCOPE: {"used_tokens": 950}}}
        )
        monkeypatch.setattr(clio_react_record, "arc_scope", lambda: (arc, sid, _SCOPE))
        monkeypatch.setattr(gact_context, "active_react_context_window", lambda: 1000)
        monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: 0)

        token = _ctx.set_app(app)
        try:
            maybe_autocompact()
        finally:
            _ctx.reset(token)

        record = _record_of(app.state.messages[sid][-1])
        assert record.trigger == "auto" and record.text.startswith("durable usage summary")
        assert [s.kind for s in arc.render_working_set(sid, _SCOPE)] == ["summary"]


def test_a_measured_count_wins_over_the_previous_turns_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once this binding measured a call, the previous turn's (high) usage no longer
    counts: the compaction it caused is not repeated at every later step."""
    import clio_agent.gact.agents.clio_react_record as clio_react_record
    import clio_agent.gact.context as gact_context
    import clio_agent.gact.runtime.context_tokens as context_tokens
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.compaction import maybe_autocompact

    agent = _CapturingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "the question")])
        app.state.sessions.update(
            sid, metadata_patch={"context_usage_by_scope": {_SCOPE: {"used_tokens": 950}}}
        )
        monkeypatch.setattr(clio_react_record, "arc_scope", lambda: (app.state.arc, sid, _SCOPE))
        monkeypatch.setattr(gact_context, "active_react_context_window", lambda: 1000)
        monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: 100)

        token = _ctx.set_app(app)
        try:
            maybe_autocompact()
        finally:
            _ctx.reset(token)

        assert agent.prompts == []


def test_maybe_autocompact_skips_typed_with_no_active_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No active app bound: a typed, audited skip -- never a bare no-op or a crash."""
    import clio_agent.gact.compaction as compaction_module
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.agents import clio_react_record
    from clio_agent.gact.compaction import AUDIT_AUTO_SKIPPED, maybe_autocompact

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        compaction_module, "stream_audit", lambda stage, **fields: audits.append((stage, fields))
    )
    monkeypatch.setattr(clio_react_record, "arc_scope", lambda: (object(), "sess-no-app", "scope"))

    assert _ctx.active_app() is None
    maybe_autocompact()

    skipped = [f for stage, f in audits if stage == AUDIT_AUTO_SKIPPED]
    assert skipped and skipped[-1]["reason"] == "no_active_app"
    assert skipped[-1]["session_id"] == "sess-no-app"


def test_rewind_past_the_record_restores_the_full_model_context(tmp_path: Path) -> None:
    agent = _CapturingAgent(["rewind summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "original text")])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text
        [done] = resp.json()["compactions"]
        record_row = client.app.state.messages[sid][-1]
        # Undo/rewind find the compaction by its recorded part.
        assert rolled_back_compactions([record_row]) == {done["compaction_id"]}

        rewind = client.post(f"/v1/sessions/{sid}/rewind", json={"message_id": "msg_seed"})
        assert rewind.status_code == 200, rewind.text

        ledger = client.app.state.messages[sid]
        assert [m.id for m in ledger] == ["msg_seed"]
        assert model_context_messages(ledger) == ledger
        live = client.app.state.arc.render_working_set(sid, _SCOPE)
        assert [(s.kind, s.content["text"]) for s in live] == [("user", "original text")]


def test_record_and_history_survive_a_cold_reload(tmp_path: Path) -> None:
    from clio_agent.gact.session_store import _append_session_message

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = _CapturingAgent(["reload summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent, arc=arc)
    with TestClient(app) as client:
        sid = _create_session(client)
        seed = _text_message("msg_seed", sid, "original text")
        _append_session_message(app, sid, seed)  # atoms minted too
        _record(client, sid, [seed])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text
        [done] = resp.json()["compactions"]
        live_ids = [m.id for m in app.state.messages.get(sid, [])]

        # Evict the resident ledger: rehydrated from the atoms.
        app.state.messages.clear()
        reloaded = list(app.state.messages.get(sid, []))
        assert [m.id for m in reloaded] == live_ids
        assert _record_of(reloaded[-1]).text == done["summary"]
        assert model_context_messages(reloaded) == [reloaded[-1]]

        # Erase the atom lane too: rehydrated from the retained per-session file.
        app.state.messages.clear()
        drop_lane(arc._segments, sid, MESSAGE_PART_SCOPE)
        from_file = list(app.state.messages.get(sid, []))
        assert [m.id for m in from_file] == live_ids
        assert _record_of(from_file[-1]).text == done["summary"]


def test_a_record_write_failure_is_a_typed_500_and_folds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import part_atoms

    real_append = part_atoms.append_part_atom

    def _refuse_records(store: Any, session_id: str, content: dict[str, Any]) -> Any:
        if as_summarization(content.get("part") or {}) is not None:
            raise RuntimeError("simulated store failure")
        return real_append(store, session_id, content)

    monkeypatch.setattr(part_atoms, "append_part_atom", _refuse_records)
    agent = _CapturingAgent(["a summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "seed text")])
        before = [s.id for s in client.app.state.arc.render_working_set(sid, _SCOPE)]

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 500, resp.text
        assert resp.json()["error"]["error"] == "compaction_record_write_failed"
        assert [s.id for s in client.app.state.arc.render_working_set(sid, _SCOPE)] == before
        ledger = client.app.state.messages[sid]
        assert [p.type for m in ledger for p in m.parts] == ["text", "notice"]


def test_no_live_context_skips_without_calling_the_llm(tmp_path: Path) -> None:
    agent = _CapturingAgent(["should never be used"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        now = _now_iso()
        a2ui_only = Message(
            id="msg_a2ui_only",
            session_id=sid,
            role="assistant",
            created_at=now,
            updated_at=now,
            parts=[Part(id="part_a2ui_only", type="a2ui", metadata={})],
            tokens=Tokens(),
            stop_reason="end_turn",
        )
        client.app.state.messages[sid] = [a2ui_only]

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        assert (resp.json()["compacted"], resp.json()["reason"]) == (False, "no_live_context")
        assert agent.prompts == []
        assert [m.id for m in client.app.state.messages[sid]] == ["msg_a2ui_only"]


def test_a_lone_summary_is_nothing_new_to_compact(tmp_path: Path) -> None:
    agent = _CapturingAgent()
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        client.app.state.arc.append_segment(sid, _SCOPE, "summary", {"text": "earlier"})

        resp = client.post(f"/v1/sessions/{sid}/compact", params={"scope": _SCOPE})

        assert resp.json()["reason"] == "nothing_new_since_last_compaction"
        assert agent.prompts == []


def test_compaction_summarizes_the_scopes_own_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary is written from the segments it replaces (the scope's own steps,
    which the transcript ledger does not hold yet), never from the ledger."""
    import clio_agent.gact.agents.clio_react_record as clio_react_record
    import clio_agent.gact.context as gact_context
    import clio_agent.gact.runtime.context_tokens as context_tokens
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.compaction import maybe_autocompact

    agent = _CapturingAgent(["mid-turn summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    arc = app.state.arc
    with TestClient(app) as client:
        sid = _create_session(client)
        arc.append_segment(sid, _SCOPE, "user", {"text": "do the thing"})
        arc.append_segment(sid, _SCOPE, "thought", {"text": "Reading the file."})
        arc.append_segment(
            sid, _SCOPE, "tool_call", {"id": "c1", "name": "read", "args": {"path": "a.csv"}}
        )
        arc.append_segment(
            sid, _SCOPE, "observation", {"call_id": "c1", "text": "IN-TURN OBSERVATION 42"}
        )
        monkeypatch.setattr(clio_react_record, "arc_scope", lambda: (arc, sid, _SCOPE))
        monkeypatch.setattr(gact_context, "active_react_context_window", lambda: 1000)
        monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: 950)

        token = _ctx.set_app(app)
        try:
            maybe_autocompact()
        finally:
            _ctx.reset(token)

        [prompt] = agent.prompts
        assert "IN-TURN OBSERVATION 42" in prompt and "a.csv" in prompt
        live = arc.render_working_set(sid, _SCOPE)
        assert [s.kind for s in live] == ["summary"]
        assert live[0].content["text"].startswith("mid-turn summary")
