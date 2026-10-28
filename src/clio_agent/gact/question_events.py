"""Application-owned Question presentation hints and explicit deadline timers."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from clio_agent.arc.task_types import QuestionDefinition, TaskSnapshot
from clio_agent.gact.events import Event
from clio_agent.gact.question_surface import (
    prepare_question_gate,
    question_view,
    resolve_plan_question,
)

logger = logging.getLogger(__name__)


class QuestionEvents:
    """Publish committed Question views; timers never own lifecycle or delivery."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.loop = asyncio.get_running_loop()
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._active: set[asyncio.Task[None]] = set()
        self._closing = False

    def changed(self, task: TaskSnapshot) -> None:
        """Signal a confirmed owner observation from its finite Core writer thread."""
        try:
            row = question_view(self.app, task)
        except Exception as error:
            logger.exception(
                "Task boundary reason=question_view_pending error_type=%s", type(error).__name__
            )

            async def repair() -> None:
                current = await self.app.state.task_history.io.run(
                    self.app.state.task_history.task_snapshot, task.handle
                )
                if current is not None:
                    projected = await self.app.state.task_history.io.run(
                        question_view, self.app, current
                    )
                    self._publish(current, projected)

            self.loop.call_soon_threadsafe(
                self.app.state.task_recovery.defer, "question-view:" + task.handle, repair
            )
            return
        self.loop.call_soon_threadsafe(self._publish, task, row)

    def _publish(self, task: TaskSnapshot, row: dict[str, Any]) -> None:
        timer = self._timers.pop(task.handle, None)
        if timer is not None:
            timer.cancel()
        event_type = {
            "pending": "user_question.created",
            "answered": "user_question.answered",
            "cancelled": "user_question.cancelled",
            "expired": "user_question.expired",
        }.get(task.raw_status, "user_question.updated")
        owner, attended = row["session_id"], row["attended_session_id"]
        if row["source"] == "hook_defer":
            from clio_agent.gact.hooks.turn_approval import project

            project(self.app, task, row)
        else:
            for sid in dict.fromkeys((owner, attended)):
                self.app.state.bus.publish(Event(type=event_type, session_id=sid, payload=row))
        if task.terminal_at is not None and (
            row["source"] in {"plan_exit", "hook_defer"}
            or row["metadata"].get("plan_exit_approval")
        ):

            async def resolve() -> None:
                await self.app.state.task_history.io.run(resolve_plan_question, self.app, task)

            async def repair() -> None:
                try:
                    await resolve()
                except Exception as error:
                    logger.exception(
                        "Task boundary reason=plan_question_repair_pending error_type=%s",
                        type(error).__name__,
                    )
                    if (
                        row["source"] != "hook_defer"
                        or self.app.state.task_history.requires_recovery
                    ):
                        self.app.state.task_recovery.defer(
                            "question-resolution:" + task.handle, resolve
                        )

            repairing = asyncio.create_task(repair())
            self._active.add(repairing)
            repairing.add_done_callback(self._active.discard)
        if not self._closing and task.raw_status == "pending" and row["expires_at"]:
            deadline = datetime.fromisoformat(row["expires_at"])
            delay = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
            self._timers[task.handle] = self.loop.call_later(delay, self._expired, task.handle)

    def _expired(self, handle: str) -> None:
        self._timers.pop(handle, None)

        async def expire() -> None:
            try:
                await self.app.state.task_history.io.run(self.app.state.questions.expire, handle)
            except Exception as error:
                logger.exception(
                    "Task boundary reason=question_expiry_recording_unconfirmed error_type=%s",
                    type(error).__name__,
                )

                async def recover() -> None:
                    await self.app.state.task_history.io.run(
                        self.app.state.questions.expire, handle
                    )

                self.app.state.task_recovery.defer("question-expiry:" + handle, recover)

        task = asyncio.create_task(expire())
        self._active.add(task)
        task.add_done_callback(self._active.discard)

    async def restore(self, session_id: str | None = None) -> None:
        """Rebuild only active presentation/deadline hints from confirmed Core indexes.

        With ``session_id``, only that conversation's open Questions (obligations lane);
        the execution-gate sweep then runs once on its own (:meth:`restore_gates`).
        """

        def replay() -> None:
            history = self.app.state.task_history
            for handle in history.task_handles(
                kind="Question", session_id=session_id, active_only=True
            ):
                task = history.task_snapshot(handle)
                if task is not None and task.details is not None:
                    if task.tool in {"hook_defer", "plan_exit"}:
                        definition = QuestionDefinition.model_validate(
                            history.read_content(task.details)
                        )
                        try:
                            prepare_question_gate(self.app, task, definition)
                        except ValueError:
                            logger.exception(
                                "Approval gate requires inspection handle=%s", task.handle
                            )
                    self.changed(task)
            if session_id is None:
                self._replay_gates()

        await self.app.state.task_history.io.run(replay)

    async def restore_gates(self) -> None:
        """Publish execution gates whose Question settled while the owner was down."""
        await self.app.state.task_history.io.run(self._replay_gates)

    def _replay_gates(self) -> None:
        history = self.app.state.task_history
        from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

        # The index names the sessions with an open execution gate: only those are read.
        for session in self.app.state.sessions.select(PageQuery(archived=None, gated=True)):
            gate = session.metadata.get("execution_gate")
            task = history.task_snapshot(gate) if isinstance(gate, str) and gate else None
            if task is not None and task.kind == "Question" and task.terminal_at is not None:
                self.changed(task)

    async def close(self) -> None:
        """Retire hints while leaving accepted open Questions durable."""
        self._closing = True
        self.app.state.questions.on_change = None
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        if self._active:
            await asyncio.gather(*tuple(self._active))
