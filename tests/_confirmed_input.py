"""Real confirmed-input segments for compaction and recall tests.

A message the person sent through the mailbox (a typed message, a child's report, an
answer) reaches the agent as a ``recipient_input`` segment whose ``rendered_content``
holds Core references; the text lives in the task history's content store. Tests that
measure or read those messages need that real shape, not a ``{"text": ...}`` stand-in.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.task_history import TaskHistory
from tests.test_arc.test_task_history import input_record, mail


def confirm_input(
    history: TaskHistory, identity: str, text: str, *, turn_id: str = "t1", human: bool = True
) -> Any:
    """Record one confirmed input carrying ``text`` (session ``owner``, scope ``main``)."""
    item = history.record_mail(mail(history, f"mail-{identity}"))
    rendered = history.write_content("owner", {"text": text})
    proof = input_record(item, identity=identity).model_copy(
        update={
            "rendered_content": (rendered,),
            "iteration_id": f"iteration-{identity}",
            "turn_id": turn_id,
            "human_input": human,
        }
    )
    history.record_input(proof)
    return proof
