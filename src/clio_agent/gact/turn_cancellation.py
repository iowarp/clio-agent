"""Turn-level cancellation settlement helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from clio_agent.gact.runtime.globals import _cancelled_error_info

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState


def settle_asyncio_cancellation(state: "TurnState") -> None:
    """Consume the cancellation flag and clear partial turn output."""

    state.app.state.cancel_flags.discard(state.sid)
    state.error_info = _cancelled_error_info(
        state.sid,
        execution_cancellation="best_effort",
        executor_work_may_continue=True,
    )
    state.answer_text = ""
    state.tools_called = []
