"""#1339: compaction is ONE operation -- an appended checkpoint, two triggers.

Covers :mod:`clio_agent.gact.compaction` end to end: the manual route
(``POST /v1/sessions/{sid}/compact``), the auto trigger
(:func:`clio_agent.gact.compaction.maybe_autocompact`), staging while a turn's
minter is open, the typed ``arc_status`` catalog, and reload/rewind survival.
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
from clio_agent.gact.compaction import ARC_STATUSES, compact_session_context
from clio_agent.gact.conversation_projection import model_context_messages
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE
from clio_agent.gact.types import Message, Part, Tokens

from .test_post_messages import _create_session

_NOW = "2026-09-11T00:00:00+00:00"


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


def _seed(client: TestClient, sid: str, messages: list[Message]) -> None:
    client.app.state.messages[sid] = messages
    client.app.state.message_store.replace_session(sid, messages)
    client.app.state.sessions.update(sid, message_count=len(messages))


class _CapturingAgent:
    """Fake compact agent recording every prompt it summarises."""

    def __init__(self, summaries: list[str] | None = None) -> None:
        self.prompts: list[str] = []
        self._summaries = list(summaries or [])
        self._default = "a summary"

    def _run_chat_agent(self, question: str, _session_id: str) -> str:
        self.prompts.append(question)
        if self._summaries:
            return self._summaries.pop(0)
        return self._default

    def _call_with_transient_provider_retries(self, _label: str, call: Callable[[], Any]) -> Any:
        return call()


# --------------------------------------------------------------------------- #
# 1. appends a checkpoint and keeps every row.
# --------------------------------------------------------------------------- #


def test_appends_a_checkpoint_and_keeps_every_row(tmp_path: Path) -> None:
    agent = _CapturingAgent(["the summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "seed text")])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["compacted"] is True
        assert body["archived_count"] == 1
        assert body["checkpoint_placement"] == "appended"

        ledger = client.app.state.messages[sid]
        assert len(ledger) == 2
        assert ledger[0].id == "msg_seed"
        assert ledger[0].parts[0].text == "seed text"  # seed row untouched

        checkpoint = ledger[1]
        part = checkpoint.parts[0]
        assert part.type == "compaction"
        assert part.summary == "the summary"
        assert part.auto is False
        assert part.compacted_message_ids == ["msg_seed"]

        on_disk = client.app.state.message_store.load_session(sid)
        assert on_disk is not None
        assert [m.id for m in on_disk] == ["msg_seed", checkpoint.id]


# --------------------------------------------------------------------------- #
# 2. publishes message.created then session.compacted, frozen payload keys.
# --------------------------------------------------------------------------- #


def test_publishes_message_created_then_session_compacted(tmp_path: Path) -> None:
    agent = _CapturingAgent(["evt summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "seed text")])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()

        history = client.app.state.bus._history.get(sid, [])
        created = [e for e in history if e.type == "message.created"]
        compacted = [e for e in history if e.type == "session.compacted"]
        assert created, "no message.created published"
        assert compacted, "no session.compacted published"
        # message.created for the checkpoint arrives before session.compacted.
        assert history.index(created[-1]) < history.index(compacted[-1])

        checkpoint_id = client.app.state.messages[sid][-1].id
        created_payload = created[-1].payload
        assert created_payload["id"] == checkpoint_id
        assert created_payload["parts"][0]["type"] == "compaction"
        assert created_payload["parts"][0]["summary"] == "evt summary"

        compacted_payload = compacted[-1].payload
        assert set(compacted_payload.keys()) == {
            "event_id",
            "archived_count",
            "summary_chars",
            "summary_message_id",
            "version",
            "trigger",
        }
        assert compacted_payload["event_id"] == body["event_id"]
        assert compacted_payload["version"] == 1
        assert compacted_payload["trigger"] == "manual"
        assert compacted_payload["archived_count"] == 1
        assert compacted_payload["summary_message_id"] == created_payload["id"]


# --------------------------------------------------------------------------- #
# 3. the prompt is the model context, not the last fifty.
# --------------------------------------------------------------------------- #


def test_prompt_is_the_full_model_context_not_the_last_fifty(tmp_path: Path) -> None:
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
        assert len(agent.prompts) == 1
        assert "ROW-IDENTIFIER-0" in agent.prompts[0]
        assert "ROW-IDENTIFIER-59" in agent.prompts[0]


# --------------------------------------------------------------------------- #
# 4. repeated compaction retains the transcript, advances the checkpoint.
# --------------------------------------------------------------------------- #


def test_repeated_compaction_retains_the_transcript_and_advances_the_checkpoint(
    tmp_path: Path,
) -> None:
    agent = _CapturingAgent(["SUMMARY 1", "SUMMARY 2"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "ORIGINAL TRANSCRIPT ROW")])

        first = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert first.status_code == 200, first.text
        first_checkpoint_id = client.app.state.messages[sid][-1].id

        ledger = list(client.app.state.messages[sid])
        after_first = _text_message(
            "msg_after_first", sid, "AFTER FIRST CHECKPOINT", role="assistant"
        )
        ledger.append(after_first)
        client.app.state.messages[sid] = ledger
        client.app.state.message_store.replace_session(sid, ledger)

        second = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert second.status_code == 200, second.text

        final_ledger = client.app.state.messages[sid]
        assert [p.type for m in final_ledger for p in m.parts] == [
            "text",
            "compaction",
            "text",
            "compaction",
        ]
        second_checkpoint = final_ledger[-1]
        assert second_checkpoint.parts[0].compacted_message_ids == [
            first_checkpoint_id,
            "msg_after_first",
        ]

        assert len(agent.prompts) == 2
        second_prompt = agent.prompts[1]
        assert "SUMMARY 1" in second_prompt
        assert "AFTER FIRST CHECKPOINT" in second_prompt
        assert "ORIGINAL TRANSCRIPT ROW" not in second_prompt


# --------------------------------------------------------------------------- #
# 5. history prepend renders the checkpoint, not the pre-checkpoint text.
# --------------------------------------------------------------------------- #


def test_history_prepend_renders_compacted_context_not_pre_checkpoint_text(
    tmp_path: Path,
) -> None:
    from clio_agent.gact.session_store import _compile_session_conversation_history

    agent = _CapturingAgent(["the checkpoint summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(
            client,
            sid,
            [
                _text_message("msg_seed", sid, "PRE CHECKPOINT SECRET", role="assistant"),
                _text_message("msg_user_2", sid, "a follow-up question", role="user"),
            ],
        )

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text

        # A fresh user message for the NEXT turn (the trailing-user-pop leaves prior
        # turns only); the compaction row is `role="assistant"` so it survives the pop.
        ledger = list(client.app.state.messages[sid])
        ledger.append(_text_message("msg_user_3", sid, "next question", role="user"))
        client.app.state.messages[sid] = ledger

        history = _compile_session_conversation_history(client.app, sid, "CURRENT PROMPT")

        assert "Compacted context: the checkpoint summary" in history
        assert "PRE CHECKPOINT SECRET" not in history
        assert "CURRENT PROMPT" in history


# --------------------------------------------------------------------------- #
# 6. context frame marks pre-checkpoint rows excluded.
# --------------------------------------------------------------------------- #


def test_context_frame_marks_pre_checkpoint_rows_excluded(tmp_path: Path) -> None:
    from clio_agent.gact.enrichment import _record_context_frame

    agent = _CapturingAgent(["frame summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        seed = _text_message("msg_seed", sid, "pre-checkpoint content")
        _seed(client, sid, [seed])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text

        app = client.app
        sess = app.state.sessions.get(sid)
        frame = _record_context_frame(
            app,
            sid,
            sess,
            seed,
            user_text="pre-checkpoint content",
            enriched_text="pre-checkpoint content",
            context_error=None,
        )

        message_items = [item for item in frame["items"] if item["kind"] == "message"]
        by_id = {item["source_id"]: item for item in message_items}
        assert by_id["msg_seed"]["included"] is False
        assert by_id["msg_seed"]["reason"] == "compacted"
        checkpoint_id = next(iid for iid in by_id if iid != "msg_seed")
        assert by_id[checkpoint_id]["included"] is True
        assert by_id[checkpoint_id]["reason"] == "visible_transcript"


# --------------------------------------------------------------------------- #
# 7 + 8. auto trigger stages; manual compaction during a running turn stages too.
# --------------------------------------------------------------------------- #


def _open_minter_with_one_prior_message(client: TestClient, sid: str) -> tuple[Any, str]:
    from clio_agent.gact.part_atom_minter import open_turn_minter
    from clio_agent.gact.session_store import _append_session_message

    # Through the real persist seam (atoms minted synchronously, no loop running)
    # so a reload-equality assertion downstream sees this row too.
    _append_session_message(
        client.app, sid, _text_message("msg_user_1", sid, "do the thing", role="user")
    )
    minter = open_turn_minter(client.app, sid, "turn_1")
    return minter, "msg_user_1"


def test_auto_trigger_stages_and_flushes_after_the_turns_assistant_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clio_agent.gact.agents.reactv2_events as reactv2_events
    import clio_agent.gact.context as gact_context
    import clio_agent.gact.runtime.context_tokens as context_tokens
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.compaction import maybe_autocompact
    from clio_agent.gact.part_atom_minter import persist_finalized_message
    from clio_agent.gact.transcript_projection import assemble_session_messages

    agent = _CapturingAgent(["auto summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    arc = app.state.arc
    scope = "scope_auto"

    with TestClient(app) as client:
        sid = _create_session(client)
        _, _ = _open_minter_with_one_prior_message(client, sid)

        arc.append_segment(sid, scope, "observation", {"text": "first live segment"})
        arc.append_segment(sid, scope, "observation", {"text": "second live segment"})

        monkeypatch.setattr(reactv2_events, "_arc_scope", lambda: (arc, sid, scope))
        monkeypatch.setattr(gact_context, "active_react_context_window", lambda: 1000)
        monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: 950)

        summarize_calls: list[tuple[Any, ...]] = []
        real_summarize = arc.summarize_segments

        def _spy_summarize(*args: Any, **kwargs: Any) -> Any:
            summarize_calls.append(args)
            return real_summarize(*args, **kwargs)

        arc.summarize_segments = _spy_summarize  # type: ignore[method-assign]

        reset_calls: list[str] = []
        import clio_agent.providers.stateful_common as stateful_common

        real_reset = stateful_common.note_prefix_reset_for_active_scope

        def _spy_reset(reason: str = "ops_reset") -> bool:
            reset_calls.append(reason)
            return real_reset(reason)

        monkeypatch.setattr(stateful_common, "note_prefix_reset_for_active_scope", _spy_reset)

        app_token = _ctx.set_app(app)
        try:
            maybe_autocompact()
        finally:
            _ctx.reset(app_token)

        assert len(summarize_calls) == 1
        assert reset_calls == ["ops_reset"]
        # Staged: the ledger is unchanged so far.
        assert [m.id for m in client.app.state.messages[sid]] == ["msg_user_1"]

        assistant_msg = _text_message(
            "msg_assistant_1", sid, "the turn's own answer", role="assistant"
        )
        persist_finalized_message(app, sid, assistant_msg)

        ledger = client.app.state.messages[sid]
        assert [m.id for m in ledger][:2] == ["msg_user_1", "msg_assistant_1"]
        assert len(ledger) == 3
        part_types = [p.type for m in ledger for p in m.parts]
        assert part_types == ["text", "text", "compaction"]
        assert ledger[-1].parts[0].auto is True

        reloaded = assemble_session_messages(arc, sid)
        assert [m.id for m in reloaded] == [m.id for m in ledger]


def test_maybe_autocompact_skips_typed_with_no_active_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No silent fallback (owner rule): with no active app bound,
    ``maybe_autocompact`` returns without raising AND records a typed,
    audited skip -- never a bare no-op.

    ``_ctx.active_app()`` is documented nullable; ``compact_session_context``
    reads ``app.state.sessions`` unguarded, so this path must never reach it.
    """

    import clio_agent.gact.compaction as compaction_module
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.agents import reactv2_events as _events
    from clio_agent.gact.compaction import AUDIT_AUTO_SKIPPED, maybe_autocompact

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        compaction_module,
        "stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    monkeypatch.setattr(_events, "_arc_scope", lambda: (object(), "sess-no-app", "scope"))

    assert _ctx.active_app() is None  # precondition: nothing bound in this thread
    maybe_autocompact()  # must not raise

    skipped = [f for stage, f in audits if stage == AUDIT_AUTO_SKIPPED]
    assert skipped, audits
    assert skipped[-1]["reason"] == "no_active_app"
    assert skipped[-1]["session_id"] == "sess-no-app"


def test_manual_compaction_during_a_running_turn_also_stages(tmp_path: Path) -> None:
    agent = _CapturingAgent(["staged summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = _create_session(client)
        _open_minter_with_one_prior_message(client, sid)

        result = compact_session_context(app, sid, trigger="manual")

        assert result["compacted"] is True
        assert result["checkpoint_placement"] == "staged_for_finalize"
        assert [m.id for m in client.app.state.messages[sid]] == ["msg_user_1"]

        from clio_agent.gact.compaction import staged_checkpoint

        assert staged_checkpoint(app, sid) is not None


# --------------------------------------------------------------------------- #
# 9. arc_status typed for every plane outcome.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "case",
    ["not_configured", "no_active_scope", "working_set_too_small", "folded"],
)
def test_arc_status_typed_for_every_plane_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Exercises ``compaction._fold_arc_working_set`` directly -- the exact function
    that types ``arc_status`` -- rather than the full route: with ``app.state.arc``
    truly absent, ``memory.compacted``'s semantic-event emission hard-fails (ARC is
    the mandatory source for the highway, by an unrelated, pre-existing invariant),
    so the "not_configured" leg cannot be driven end to end through a live app."""

    from types import SimpleNamespace

    import clio_agent.gact.agents.reactv2_events as reactv2_events
    from clio_agent.gact.compaction import (
        ARC_FOLDED,
        ARC_NO_ACTIVE_SCOPE,
        ARC_NOT_CONFIGURED,
        ARC_WORKING_SET_TOO_SMALL,
        _fold_arc_working_set,
    )

    expected = {
        "not_configured": ARC_NOT_CONFIGURED,
        "no_active_scope": ARC_NO_ACTIVE_SCOPE,
        "working_set_too_small": ARC_WORKING_SET_TOO_SMALL,
        "folded": ARC_FOLDED,
    }[case]

    if case == "not_configured":
        fake_app = SimpleNamespace(state=SimpleNamespace(arc=None))
        status = _fold_arc_working_set(fake_app, "summary text", "turn_1")
        assert status == expected
        assert status in ARC_STATUSES
        return

    agent = _CapturingAgent(["plane summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app):
        arc = app.state.arc
        sid = "sess_plane"
        scope = "scope_plane"
        if case != "no_active_scope":
            # "no_active_scope" needs no monkeypatch: a bare test thread has no
            # active react scope by construction, which is exactly the case.
            if case == "folded":
                arc.append_segment(sid, scope, "observation", {"text": "one"})
                arc.append_segment(sid, scope, "observation", {"text": "two"})
            monkeypatch.setattr(reactv2_events, "_arc_scope", lambda: (arc, sid, scope))

        status = _fold_arc_working_set(app, "summary text", "turn_1")
        assert status == expected
        assert status in ARC_STATUSES


# --------------------------------------------------------------------------- #
# 10. rewind past the checkpoint restores the full model context.
# --------------------------------------------------------------------------- #


def test_rewind_past_the_checkpoint_restores_the_full_model_context(tmp_path: Path) -> None:
    agent = _CapturingAgent(["rewind summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "original text")])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text
        assert len(client.app.state.messages[sid]) == 2

        # ``include_target=False`` (default): rewind to right AFTER the target,
        # keeping msg_seed and dropping everything after it (the checkpoint).
        rewind = client.post(
            f"/v1/sessions/{sid}/rewind",
            json={"message_id": "msg_seed"},
        )
        assert rewind.status_code == 200, rewind.text

        ledger = client.app.state.messages[sid]
        assert [m.id for m in ledger] == ["msg_seed"]
        assert model_context_messages(ledger) == ledger

        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert len(messages) == 1
        assert messages[0]["id"] == "msg_seed"
        assert messages[0]["parts"][0]["text"] == "original text"


# --------------------------------------------------------------------------- #
# 11. checkpoint and history survive a cold reload.
# --------------------------------------------------------------------------- #


def test_checkpoint_and_history_survive_a_cold_reload(tmp_path: Path) -> None:
    from clio_agent.gact.session_store import _append_session_message

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = _CapturingAgent(["reload summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent, arc=arc)
    with TestClient(app) as client:
        sid = _create_session(client)
        # Seed through the real persist seam (not the raw-dict ``_seed`` helper) so
        # the seed row's atoms exist too -- otherwise it would only be reload-visible
        # by accident of ``has_atoms`` being False, which the checkpoint's own atoms
        # (minted below) would flip, defeating the point of this test.
        _append_session_message(app, sid, _text_message("msg_seed", sid, "original text"))

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert resp.status_code == 200, resp.text
        event_id = resp.json()["event_id"]
        summary = resp.json()["summary"]

        live = list(app.state.messages.get(sid, []))
        live_ids = [m.id for m in live]

        # Variant A: evict the resident ledger; atoms rehydrate it.
        app.state.messages.clear()
        reloaded = list(app.state.messages.get(sid, []))
        assert [m.id for m in reloaded] == live_ids
        checkpoint = reloaded[-1]
        assert checkpoint.parts[0].summary == summary
        assert checkpoint.metadata["memory_event_id"] == event_id
        assert model_context_messages(reloaded) == [checkpoint]

        # Variant B: erase the atom lane too -- rehydrate must come from the
        # retained per-session file (MessageStore), not the (now empty) lane.
        app.state.messages.clear()
        drop_lane(arc._segments, sid, MESSAGE_PART_SCOPE)
        reloaded_from_file = list(app.state.messages.get(sid, []))
        assert [m.id for m in reloaded_from_file] == live_ids
        assert reloaded_from_file[-1].parts[0].summary == summary


# --------------------------------------------------------------------------- #
# 12. coverage keeps the compacting turn's own assistant answer in the next prompt.
# --------------------------------------------------------------------------- #


def test_coverage_keeps_the_compacting_turns_own_assistant_answer(tmp_path: Path) -> None:
    """Turn-boundary placement: this turn's own answer lands BEFORE the checkpoint
    it was (not) summarised by, so it must still reach the NEXT compaction's prompt."""

    agent = _CapturingAgent(["first pass summary", "second pass summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "seed row")])

        first = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert first.status_code == 200, first.text

        # Simulate this turn's own answer landing (turn-boundary placement: it is
        # appended BEFORE any checkpoint that would cover it).
        ledger = list(client.app.state.messages[sid])
        own_answer = _text_message("msg_own_answer", sid, "THIS TURNS OWN ANSWER", role="assistant")
        ledger.append(own_answer)
        client.app.state.messages[sid] = ledger
        client.app.state.message_store.replace_session(sid, ledger)

        second = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert second.status_code == 200, second.text

        assert len(agent.prompts) == 2
        assert "THIS TURNS OWN ANSWER" in agent.prompts[1]
        second_checkpoint = client.app.state.messages[sid][-1]
        assert "msg_own_answer" in second_checkpoint.parts[0].compacted_message_ids


# --------------------------------------------------------------------------- #
# Review fixes (F1/F2): typed persist-failure surfaces, empty-transcript skip.
# --------------------------------------------------------------------------- #


def test_append_checkpoint_persist_failure_returns_typed_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1339 review F1: a raw exception out of the checkpoint landing (here,
    run_transcript_job/on_message_appended) must reach the client as the ONE typed
    500 memory_update_failed, never an untyped internal_error from the generic error
    middleware -- and the failure is audited before it is raised."""

    import clio_agent.gact.compaction as compaction_module
    from clio_agent.gact import part_atom_minter

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        compaction_module,
        "stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(part_atom_minter, "run_transcript_job", _raise)

    agent = _CapturingAgent(["a summary"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "seed text")])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["error"]["error"] == "memory_update_failed"
    assert body["error"]["recoverable"] is False
    assert body["error"]["details"]["stage"] == "append_checkpoint"

    persist_failed = [f for stage, f in audits if stage == "compaction.persist_failed"]
    assert persist_failed, audits
    assert persist_failed[-1]["landing_stage"] == "append_checkpoint"
    assert "simulated store failure" in persist_failed[-1]["error"]


def test_fold_arc_working_set_failure_returns_typed_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same wrap covers the ARC fold (arc.summarize_segments is a real store
    RPC and can raise): the client still sees the one typed 500."""

    import clio_agent.gact.agents.reactv2_events as reactv2_events
    import clio_agent.gact.compaction as compaction_module

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        compaction_module,
        "stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )

    agent = _CapturingAgent(["a summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_seed", sid, "seed text")])

        arc = app.state.arc
        scope = "scope_fold_fail"
        arc.append_segment(sid, scope, "observation", {"text": "one"})
        arc.append_segment(sid, scope, "observation", {"text": "two"})
        monkeypatch.setattr(reactv2_events, "_arc_scope", lambda: (arc, sid, scope))

        def _raise(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("simulated summarize failure")

        monkeypatch.setattr(arc, "summarize_segments", _raise)

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["error"]["error"] == "memory_update_failed"
    assert body["error"]["details"]["stage"] == "fold_arc_working_set"

    persist_failed = [f for stage, f in audits if stage == "compaction.persist_failed"]
    assert persist_failed and persist_failed[-1]["landing_stage"] == "fold_arc_working_set"


def test_staged_flush_failure_never_fails_the_turns_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1339 review F1: a staged checkpoint that fails to flush must not take the
    turn's own, already-real, assistant answer down with it -- the message persists,
    the checkpoint is dropped, and the failure is audited."""

    import clio_agent.gact.compaction as compaction_module
    from clio_agent.gact import part_atom_minter
    from clio_agent.gact.part_atom_minter import persist_finalized_message

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        part_atom_minter,
        "stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )

    agent = _CapturingAgent(["staged summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = _create_session(client)
        _open_minter_with_one_prior_message(client, sid)

        result = compact_session_context(app, sid, trigger="manual")
        assert result["checkpoint_placement"] == "staged_for_finalize"
        event_id = result["event_id"]

        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated flush failure")

        monkeypatch.setattr(compaction_module, "append_checkpoint", _raise)

        assistant_msg = _text_message(
            "msg_assistant_1", sid, "the turn's own answer", role="assistant"
        )
        persist_finalized_message(app, sid, assistant_msg)  # must not raise

        ledger = client.app.state.messages[sid]
        assert [m.id for m in ledger] == ["msg_user_1", "msg_assistant_1"]
        assert all(p.type != "compaction" for m in ledger for p in m.parts)

        failed = [f for stage, f in audits if stage == "compaction.staged_flush_failed"]
        assert failed, audits
        assert failed[-1]["event_id"] == event_id
        assert failed[-1]["session_id"] == sid


def test_empty_model_context_transcript_skips_without_calling_the_llm(
    tmp_path: Path,
) -> None:
    """#1339 review F2: a session whose model-context rows render to nothing (only
    an a2ui part here, no text-bearing class) skips typed, before dispatch_pre_compact
    and the LLM -- never an empty --- transcript --- block sent to the model."""

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
        _seed(client, sid, [a2ui_only])

        resp = client.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["compacted"] is False
        assert body["reason"] == "model_context_empty"
        assert agent.prompts == []
        # The row is untouched -- a skip is not a checkpoint.
        assert [m.id for m in client.app.state.messages[sid]] == ["msg_a2ui_only"]
