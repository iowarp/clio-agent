"""Admission against the spend of a flow's durable child assignments."""

from __future__ import annotations

from typing import Any

from clio_agent.flows.journal import FlowJournal
from clio_agent.flows.outcomes import FlowLimitExceeded


class FlowBudget:
    """Read known child usage before admitting new work, including after replay."""

    def __init__(self, app: Any, journal: FlowJournal, tokens: int) -> None:
        self.app = app
        self.journal = journal
        self.tokens = tokens

    async def check(self, target: str) -> None:
        """Refuse new work once recorded child usage has consumed the budget.

        Already running work may finish beyond the limit. Queued calls must check
        here after acquiring their execution slot, rather than at construction.
        Session totals make repeated collection and replay idempotent.
        """
        if not self.tokens:
            return
        spent = await self.app.state.task_history.io.run(self._spent)
        if spent >= self.tokens:
            raise FlowLimitExceeded(
                f"flow {self.journal.flow_id} spent its {self.tokens}-token budget",
                limit="budget_tokens",
                spent=spent,
                target=target,
            )

    def _spent(self) -> int:
        """Count each admitted child's subtree once, including nested flows."""
        sessions = {
            str(row["session_id"])
            for row in self.journal.content["calls"].values()
            if row.get("session_id") and row.get("kind") in {"agent", "subagent", "flow"}
        }
        history = self.app.state.task_history
        with history.serialized():
            for sid in tuple(sessions):
                sessions.update(history.session_tree_ids(sid))
        spent = 0
        for sid in sessions:
            session = self.app.state.sessions.get(sid)
            if session is not None:
                spent += int(session.tokens_input or 0) + int(session.tokens_output or 0)
        return spent
