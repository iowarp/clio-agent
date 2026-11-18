"""Transient startup reads retain recovery instead of silently holding admission.

Startup restoration reads the assignment's details from Core. A transient failure of
that read keeps admission held, arms Core recovery, and the readiness callback resumes
the same restoration, preserving the one original assignment.

The conversation's context is not read at startup: a call left without a result is
repaired when the next forward folds the scope (FN-16,
``gact.agents.context_repair``), whose read failure is a typed turn failure
(``tests/test_arc/test_context_repair.py``). A context outage at startup therefore
cannot hold admission and is not a reason to wait.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import AssignmentDetails, ContextBoundary, CoreReference
from clio_agent.errors import ClioError
from clio_agent.gact.question_events import QuestionEvents
from tests.test_arc.test_conversation_owner_contracts import create_environment


def _interrupted_assignment(arc: ARCMemory, owner: Any, root: Any) -> CoreReference:
    """A busy assignment whose retained context ends in a call with no result."""
    history, sid = owner.history, root.owner.session_id
    arc.append_segment(sid, "main", "thought", {"text": "Retained checkpoint"})
    arc.append_segment(sid, "main", "tool_call", {"id": "call_cut", "name": "shell", "args": {}})
    details = history.write_content(
        sid,
        AssignmentDetails(
            context=ContextBoundary(session_id=sid, scope="main", logical_time=0)
        ).model_dump(mode="json"),
    )
    history.record_snapshot(
        root.model_copy(update={"activity": "busy", "details": details}),
        observation_id="interrupted-model-segment",
    )
    return details


def _start_restoring(app: Any, owner: Any) -> None:
    """Exercise the actual readiness callback, while preventing test inference."""
    app.state.question_events = QuestionEvents(app)
    owner.runner.close_admission()
    owner.start()
    owner._restoring = True


async def _assert_one_recovery(owner: Any, root: Any) -> list[Any]:
    """The original assignment is recovering with exactly one recovery mail, not run."""
    history, sid = owner.history, root.owner.session_id
    assert not history.requires_recovery and not owner._restoring
    assert owner.recovery.pending == ()
    current = history.latest_assignment(sid)
    assert current is not None and current.handle == root.handle
    assert current.activity == "recovering" and current.terminal_at is None
    pending = await history.io.run(history.pending_mail, sid)
    assert len(pending) == 1 and pending[0].id.startswith("recovery:" + root.handle)
    assert pending[0].handle == root.handle and owner.eligible(sid, pending[0])
    assert not history.acknowledged_notifications(sid)
    assert not owner.runner.busy(sid), "Restoration cannot replay model execution"
    return pending


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["initial_details", "recovery_details"])
@pytest.mark.parametrize("error_type", [OSError, ClioError])
async def test_startup_core_read_failure_retries_before_releasing_admission(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    error_type: type[Exception],
) -> None:
    """The real recovery worker replays reads and preserves one original assignment."""
    app, owner, root = await create_environment(arc, tmp_path)
    history, sid = owner.history, root.owner.session_id
    details = await history.io.run(_interrupted_assignment, arc, owner, root)
    original_read = history.read_content
    detail_reads = 0
    failed = False
    outage = True

    def read(reference: CoreReference) -> Any:
        nonlocal detail_reads, failed
        if reference == details:
            detail_reads += 1
            selected = site == "initial_details" or (
                site == "recovery_details" and detail_reads >= 2
            )
            if outage and selected:
                failed = True
                raise error_type("Transient assignment-details read outage")
        return original_read(reference)

    monkeypatch.setattr(history, "read_content", read)
    _start_restoring(app, owner)
    try:
        await owner.restore()
        assert failed and history.requires_recovery and owner._restoring
        await asyncio.sleep(0)  # Register the same application's recovery entry.
        assert "core-writer" in owner.recovery.pending
        assert not owner.runner.busy(sid)

        outage = False
        await asyncio.wait_for(owner.recovery.drain(), 10)
        pending = await _assert_one_recovery(owner, root)
        await owner.restore()
        assert await history.io.run(history.pending_mail, sid) == pending
    finally:
        outage = False
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [OSError, ClioError])
async def test_startup_restoration_does_not_wait_on_the_assignment_context(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """A context outage at startup neither holds admission nor is repaired around.

    The restart-time writer that read the context to answer cut calls was removed
    (FN-16): the fold repairs them on the next forward. Restoration releases admission
    in one pass with the one recovery mail, and leaves the cut call for the fold.
    """
    app, owner, root = await create_environment(arc, tmp_path)
    history, sid = owner.history, root.owner.session_id
    await history.io.run(_interrupted_assignment, arc, owner, root)
    original_view = arc.context_view
    context_reads = 0

    def view(session_id: str, scope: str, **kwargs: Any) -> Any:
        nonlocal context_reads
        if (session_id, scope) == (sid, "main"):
            context_reads += 1
            raise error_type("Transient assignment-context read outage")
        return original_view(session_id, scope, **kwargs)

    monkeypatch.setattr(arc, "context_view", view)
    _start_restoring(app, owner)
    try:
        await owner.restore()
        assert context_reads == 0, "startup restoration reads no conversation context"
        pending = await _assert_one_recovery(owner, root)
        content = await history.io.run(history.read_content, pending[0].content)
        assert content["context"]["scope"] == "main", "the boundary is carried, not read"
        monkeypatch.setattr(arc, "context_view", original_view)
        live = arc.context_view(sid, "main").segments
        assert [s.kind for s in live] == ["thought", "tool_call"], (
            "no restart-time observation: the next fold repairs the cut call"
        )
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
