"""Public recording/reload boundaries against real Core, including persistence faults."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import session_store, transcript_recording
from clio_agent.gact.messages import MessageStore
from clio_agent.gact.metrics_counters import MetricsCounters
from clio_agent.gact.part_atom_minter import persist_finalized_message
from clio_agent.gact.resident_ledgers import build_resident_ledger_set
from clio_agent.gact.transcript_file import RegistryTranscriptIndex, boot_transcript_store
from clio_agent.gact.transcript_migration import (
    IMPORT_DONE,
    LEGACY_SCOPE,
    legacy_transcript,
    migrate_transcripts,
)
from clio_agent.gact.transcript_projection import assemble_session_messages
from clio_agent.gact.transcript_recording import TranscriptRecordingPending
from clio_agent.gact.types import ErrorInfo, Message
from tests.test_arc.test_accepted_transcript_repair import raw_atoms, retained
from tests.test_arc.test_conversation_owner_contracts import create_environment


def install(app: Any, root: Path, *, file_on: bool = True) -> MessageStore:
    """Use the production cold loader, registry index and optional JSON projection."""
    cache = MessageStore(root / "messages")
    app.state.transcript_file = file_on
    app.state.message_store = cache if file_on else None
    app.state.transcript_index = RegistryTranscriptIndex(app.state.sessions)
    app.state.metrics_counters = MetricsCounters()
    app.state.messages = build_resident_ledger_set(app)
    return cache


@pytest.mark.asyncio
@pytest.mark.parametrize("file_on", [False, True])
async def test_cold_reads_never_write_or_replace_core_from_json(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_on: bool
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    cache = install(app, tmp_path, file_on=file_on)

    def exercise() -> None:
        message = retained(sid, "canonical", 10, role="assistant")
        persist_finalized_message(app, sid, message)
        if file_on:
            cache.replace_session(sid, [retained(sid, "stale-cache", 9)])
        before = raw_atoms(arc, sid)

        def forbid_write(*_args: Any, **_kwargs: Any) -> None:
            pytest.fail("A normal transcript read attempted a Core write")

        with monkeypatch.context() as reads:
            reads.setattr(arc._store, "put", forbid_write)
            for _ in range(3):
                app.state.messages.discard(sid)
                assert app.state.messages[sid] == [message]
        assert raw_atoms(arc, sid) == before
        if file_on:
            assert cache.load_session(sid) == [retained(sid, "stale-cache", 9)]

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_lost_envelope_reply_recovers_original_output_without_duplicate_or_failed_turn(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    cache = install(app, tmp_path)
    message = retained(sid, "original-completed-output", 10, role="assistant")

    def first_attempt() -> tuple[TranscriptRecordingPending, list[dict[str, Any]]]:
        append = transcript_recording.append_part_atom

        def lose_reply(store: Any, session: str, atom: dict[str, Any], **kwargs: Any) -> Any:
            result = append(store, session, atom, **kwargs)
            if atom.get("atom_role") == "envelope":
                raise OSError("Original envelope committed; transport reply lost")
            return result

        with monkeypatch.context() as faults:
            faults.setattr(transcript_recording, "append_part_atom", lose_reply)
            with pytest.raises(TranscriptRecordingPending) as caught:
                persist_finalized_message(app, sid, message)
        assert caught.value.message == message
        assert cache.load_session(sid) is None
        assert assemble_session_messages(arc, sid) == [message]
        return caught.value, raw_atoms(arc, sid)

    try:
        pending, before = await asyncio.to_thread(first_attempt)
        assert await owner.confirm_transcript_recording(pending) is True

        def recovered() -> None:
            assert raw_atoms(arc, sid) == before
            app.state.messages.discard(sid)
            assert app.state.messages[sid] == cache.load_session(sid) == [message]
            assert app.state.sessions.get(sid).message_count == 1
            assert not owner.history.pending_mail(sid)
            assert not owner._model_recording

        await asyncio.to_thread(recovered)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_clear_withdraws_volatile_output_instead_of_resurrecting_it(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    install(app, tmp_path)
    message = retained(sid, "unconfirmed-output", 10, role="assistant")

    def interrupted_then_cleared() -> TranscriptRecordingPending:
        def unavailable(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("Injected canonical recording outage")

        with monkeypatch.context() as faults:
            faults.setattr(transcript_recording, "append_part_atom", unavailable)
            with pytest.raises(TranscriptRecordingPending) as caught:
                persist_finalized_message(app, sid, message)
        session_store._delete_session_messages(app, sid)
        return caught.value

    try:
        pending = await asyncio.to_thread(interrupted_then_cleared)
        assert await owner.confirm_transcript_recording(pending) is False
        assert await asyncio.to_thread(assemble_session_messages, arc, sid) == []
        assert not await asyncio.to_thread(owner.history.pending_mail, sid)
        assert not owner._model_recording
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_json_failure_does_not_turn_completed_output_into_a_failure(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    cache = install(app, tmp_path)

    def exercise() -> None:
        message = retained(sid, "completed-with-cache-lag", 10, role="assistant")

        def unavailable(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("Optional projection unavailable")

        monkeypatch.setattr(cache, "ensure_message", unavailable)
        persist_finalized_message(app, sid, message)
        before = raw_atoms(arc, sid)
        app.state.messages.discard(sid)
        assert app.state.messages[sid] == [message]
        assert raw_atoms(arc, sid) == before
        assert not owner.recovery.pending

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_legacy_custody_preserves_duplicate_occurrences_without_inventing_visibility(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    cache = install(app, tmp_path)

    def exercise() -> None:
        original_assignment = owner.history.latest_assignment(sid)
        canonical = retained(sid, "canonical", 10, role="assistant")
        persist_finalized_message(app, sid, canonical)
        duplicate = retained(sid, "duplicate-legacy-id", 9)
        legacy = [duplicate, duplicate, canonical]
        cache.replace_session(sid, legacy)
        source = cache.read_source(sid)
        migrate_transcripts(app)
        first = legacy_transcript(app, sid)
        migrate_transcripts(app)
        assert legacy_transcript(app, sid) == first
        assert first is not None and first["content"]["text"] == source
        assert [
            Message.model_validate(row) for row in json.loads(first["content"]["text"])
        ] == legacy
        occurrences = first["occurrences"]
        assert len({row["identity"] for row in occurrences}) == 3
        assert [row["index"] for row in occurrences] == [0, 1, 2]
        assert [row["visibility"] for row in occurrences] == [
            "unknown",
            "unknown",
            "canonical_match",
        ]
        assert all(row["delivery"] == "unknown" for row in occurrences)
        assert assemble_session_messages(arc, sid) == [canonical]
        session_store._delete_session_messages(app, sid)
        cache.replace_session(sid, legacy)  # Stale copy left after an interrupted file cleanup.
        migrate_transcripts(app)
        assert assemble_session_messages(arc, sid) == []
        assert legacy_transcript(app, sid) == first
        assert len(arc._segments.list_segments(sid, LEGACY_SCOPE)) == 1
        assert not owner.history.pending_mail(sid)
        assert owner.history.latest_assignment(sid) == original_assignment

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_one_complete_import_pass_ends_the_boot_scan(arc: ARCMemory, tmp_path: Path) -> None:
    """Conversations created after a complete pass are Core-native: later boots skip the scan."""
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    cache = install(app, tmp_path)

    def exercise() -> None:
        cache.replace_session(sid, [retained(sid, "legacy-request", 9)])
        assert owner.history.index.done_marker(IMPORT_DONE) is None
        migrate_transcripts(app)
        done = owner.history.index.done_marker(IMPORT_DONE)
        assert done is not None and done["version"] == 1 and done["sessions"] >= 1
        assert legacy_transcript(app, sid) is not None
        later = "sess_created_after_the_pass"
        cache.replace_session(later, [retained(later, "core-native", 9)])
        migrate_transcripts(app)
        assert legacy_transcript(app, later) is None
        assert arc._segments.list_segments(later, LEGACY_SCOPE) == []

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_boot_captures_legacy_before_reconciling_and_reads_metrics_from_core(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    boot_app = FastAPI()
    for name, value in vars(app.state).items():
        setattr(boot_app.state, name, value)
    app = boot_app
    sid = root.owner.session_id
    cache = install(app, tmp_path)

    def exercise() -> None:
        original = retained(sid, "accepted-request", 10)
        session_store._append_session_message(app, sid, original)
        legacy = retained(sid, "stale-cache-request", 9)
        cache.replace_session(sid, [legacy])
        source = cache.read_source(sid)
        app.state.sessions.update(sid, status="running")
        monkeypatch.setattr("clio_agent.gact.transcript_file.resolve_transcript_file", lambda: True)
        boot_transcript_store(app, tmp_path)
        assert app.state.transcript_boot_ready.is_set()
        rows = assemble_session_messages(arc, sid)
        assert rows[0] == original and len(rows) == 2
        assert rows[1].error_info is not None
        assert rows[1].error_info.error == "server_restart_interrupted"
        assert app.state.sessions.get(sid).message_count == 2
        preserved = legacy_transcript(app, sid)
        assert preserved is not None and preserved["content"]["text"] == source
        assert preserved["occurrences"][0]["visibility"] == "unknown"

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_stopped_recording_waiter_retains_original_completion_and_cleanup(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shutdown: bool
) -> None:
    """A cancelled turn cannot discard the recovery owner's original publication."""
    from clio_agent.gact.turn_finalize import publish_failed_finalize

    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    install(app, tmp_path)
    message = retained(sid, "retained-original-failure", 10, role="assistant").model_copy(
        update={"error_info": ErrorInfo(error="original_failure", message="Original error")}
    )
    stop = threading.Event()
    app.state.cancel_events = {sid: stop}
    for name in ("live_assistant_message_ids", "live_assistant_parts", "live_assistant_part_keys"):
        setattr(app.state, name, {sid: message.id if name.endswith("ids") else []})
    updates: list[str] = []

    def prepare() -> TranscriptRecordingPending:
        def unavailable(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("Canonical recording was temporarily unavailable")

        with monkeypatch.context() as faults:
            faults.setattr(transcript_recording, "append_part_atom", unavailable)
            with pytest.raises(TranscriptRecordingPending) as caught:
                persist_finalized_message(app, sid, message)
        return caught.value

    pending = await asyncio.to_thread(prepare)

    def update_retry(status: str, **_kwargs: Any) -> None:
        updates.append(status)

    pending.after_confirmed = lambda: publish_failed_finalize(
        app,
        sid,
        message,
        {"message_id": message.id, "stop_reason": "error"},
        turn_id=message.turn_id or message.id,
        trace_id="original-trace",
        turn_cancel_event=stop,
        update_retry_attempt=update_retry,
    )
    waiter = asyncio.create_task(transcript_recording.resume_recording(app, pending))
    try:
        while not owner.recovery.pending:
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        if shutdown:
            await owner.shutdown()
        await owner.recovery.drain()
        assert await asyncio.to_thread(assemble_session_messages, arc, sid) == [message]
        assert updates == ["failed"]
        assert all(
            sid not in getattr(app.state, name)
            for name in (
                "live_assistant_message_ids",
                "live_assistant_parts",
                "live_assistant_part_keys",
            )
        )
        assert not owner._model_recording and sid not in app.state.cancel_events
        completions = [
            event
            for event in app.state.bus.session_events_since(sid)
            if event.type == "message.completed"
        ]
        assert len(completions) == 1 and completions[0].payload["message_id"] == message.id
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "withdraw"), [("visibility", False), ("publication", False), ("publication", True)]
)
async def test_final_recording_boundary_retains_original_confirmation_and_publication(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, withdraw: bool
) -> None:
    """An initial epoch RPC or a later publication fault retains the same output."""
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    install(app, tmp_path)
    message = retained(sid, "original-boundary-output", 10, role="assistant")
    from clio_agent.gact.turn_finalize import settle_withdrawn_turn

    stop = threading.Event()
    app.state.cancel_events = {sid: stop}
    retry_states: list[str] = []
    for name in ("live_assistant_message_ids", "live_assistant_parts", "live_assistant_part_keys"):
        setattr(app.state, name, {sid: message.id if name.endswith("ids") else ["old content"]})
    publications: list[str] = []
    failed = False

    def publish() -> None:
        nonlocal failed
        if phase == "publication" and not failed:
            failed = True
            raise OSError("Publication failed before effects")
        publications.append(message.id)

    epoch = transcript_recording.visibility_epoch

    def unavailable_epoch(*args: Any, **kwargs: Any) -> str:
        nonlocal failed
        if phase == "visibility" and not failed:
            failed = True
            raise OSError("Epoch read temporarily unavailable")
        return epoch(*args, **kwargs)

    monkeypatch.setattr(transcript_recording, "visibility_epoch", unavailable_epoch)

    def prepare() -> TranscriptRecordingPending:
        app.state.sessions.update(sid, status="running")

        def update_retry(status: str, **_kwargs: Any) -> None:
            retry_states.append(status)

        with pytest.raises(TranscriptRecordingPending) as caught:
            persist_finalized_message(
                app,
                sid,
                message,
                after_confirmed=publish,
                after_withdrawn=lambda: settle_withdrawn_turn(
                    app, sid, stop, update_retry_attempt=update_retry
                ),
            )
        return caught.value

    try:
        pending = await asyncio.to_thread(prepare)
        assert pending.message == message and pending.after_confirmed is publish
        if withdraw:
            await asyncio.to_thread(session_store._delete_session_messages, app, sid)
        assert await transcript_recording.resume_recording(app, pending) is not withdraw
        assert publications == ([] if withdraw else [message.id])
        assert await asyncio.to_thread(assemble_session_messages, arc, sid) == (
            [] if withdraw else [message]
        )
        if withdraw:
            assert retry_states == ["cancelled"]
            assert app.state.sessions.get(sid).status == "idle"
            assert sid not in app.state.cancel_events
            assert all(
                sid not in getattr(app.state, name)
                for name in (
                    "live_assistant_message_ids",
                    "live_assistant_parts",
                    "live_assistant_part_keys",
                )
            )
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
