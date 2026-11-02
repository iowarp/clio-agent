"""Late eligibility cannot render or acknowledge an input excluded by the final budget."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from dspy.lm15 import Message, TextPart

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import MailItem
from clio_agent.gact import context
from clio_agent.gact.agents.clio_react_record import StepRecorder
from clio_agent.gact.conversations import ConversationOutcome, ConversationSegment
from clio_agent.gact.native_work import now
from tests._result_room import pin_result_room
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_retirement_contracts import accept, idle


@pytest.mark.asyncio
async def test_late_eligible_input_excludes_trigger_until_a_later_committed_iteration(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    processed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    pin_result_room(monkeypatch, 300)
    marker = "DEFERRED-ORIGINAL-HUMAN-INPUT"
    observed: list[str] = []
    runs: list[str] = []

    def prepare() -> None:
        owner.history.record_mail(
            MailItem(
                id="earlier",
                recipient_session=sid,
                sender_human="human",
                kind="human",
                content=owner.history.write_content(
                    sid, {"text": "Earlier accepted input " + "x" * 500}
                ),
                created_at=now(),
                delivery="held",
                held_reason="awaiting_eligibility",
            )
        )

    async def model(segment: ConversationSegment) -> ConversationOutcome:
        def iterations() -> None:
            assert segment.trigger.id == "trigger"
            context.set_react_run(-1)
            earlier = owner.history.require_mail(sid, "earlier")
            owner.history.update_mail(
                earlier.model_copy(
                    update={
                        "delivery": "eligible",
                        "held_reason": "",
                        "revision": earlier.revision + 1,
                    }
                ),
                expected_revision=earlier.revision,
            )
            recorder = StepRecorder(arc, sid, "main", expert_id="main")
            recorder.turn_id = segment.turn_id
            recorder.user_message(Message(role="user", parts=[TextPart(text=marker)]))
            recorder.commit_input(0)
            text = "\n".join(
                part.text
                for row in recorder.read_steps()
                for part in row.parts
                if isinstance(part, TextPart)
            )
            assert "Earlier accepted input" in text and marker not in text
            assert owner.history.acknowledged_notifications(sid) == frozenset({"earlier"})
            assert [item.id for item in owner.history.pending_mail(sid)] == ["trigger"]
            owner.history.restore()
            replay = owner.history.latest_input(root.handle)
            assert replay is not None
            assert replay.notification_ids == ("earlier",)
            assert marker not in str(
                [owner.history.read_content(ref) for ref in replay.rendered_content]
            )
            assert [item.id for item in owner.history.pending_mail(sid)] == ["trigger"]
            recorder.commit_input(1)
            recorder.commit_input(2)
            final = "\n".join(
                part.text
                for row in recorder.read_steps()
                for part in row.parts
                if isinstance(part, TextPart)
            )
            assert final.count(marker) == 1
            assert owner.history.acknowledged_notifications(sid) == frozenset(
                {"earlier", "trigger"}
            )
            assert not owner.history.pending_mail(sid)
            observed.append(final)
            runs.append(segment.assignment.handle)

        try:
            await asyncio.to_thread(iterations)
        except Exception as error:
            processed.set_exception(error)
            raise
        processed.set_result(None)
        return ConversationOutcome({"answer": "Both inputs retained"}, disposition="yield")

    await asyncio.to_thread(prepare)
    owner.run_segment = model
    owner.start()
    owner.recovery.on_ready = None
    try:
        await accept(owner, sid, "trigger", marker)
        await asyncio.wait_for(asyncio.shield(processed), 10)
        await idle(owner, sid)
        assert runs == [root.handle] and len(observed) == 1
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
