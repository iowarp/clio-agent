"""After "Stop everything", the conversation stays stopped until the person writes.

Found live (FQ12, a conversation resumed after a restart): each "Stop everything" cancelled
the running shell command, and the cancellation's own notices (the task's outcome and the
person's intervention) woke the agent, which started a new turn ("the build command was
cancelled when you stopped the conversation, so I'll check what state it left") and a new
command. The person had stopped everything; the conversation went on by itself.

``stop`` marks the session. While the mark stands, mail other than the person's own message
does not start a turn; it waits and is delivered with the person's next message, so the
agent still learns what the stop ended. The person's message clears the mark, and so does
the person restarting or resuming work in the conversation (``resume``): the work they start
again reports back. "Stop reply only" does not mark: the work it leaves running reports back
as it always did.
"""

from __future__ import annotations

from typing import Any

from clio_agent.arc.task_types import MailItem
from clio_agent.gact.native_work import now

#: Session metadata key holding when the person stopped everything ("" when not stopped).
STOPPED_KEY = "stopped_everything_at"


def stop(sessions: Any, sid: str) -> None:
    """Mark ``sid`` stopped by the person ("Stop everything")."""
    sessions.update(sid, metadata_patch={STOPPED_KEY: now()})


def resume(sessions: Any, sid: str) -> None:
    """End the stop of ``sid``: the person restarted or resumed work, so its results wake it."""
    session = sessions.get(sid)
    if session is not None and session.metadata.get(STOPPED_KEY):
        sessions.update(sid, metadata_patch={STOPPED_KEY: ""})


def holds(sessions: Any, history: Any, sid: str, item: MailItem) -> bool:
    """Whether ``item`` waits for the person: the session is stopped, the item is not the
    person's message, and no message of the person's is pending to carry it.

    The person's own message clears the stop (it is delivered, with what waited)."""
    session = sessions.get(sid)
    if session is None or not session.metadata.get(STOPPED_KEY):
        return False
    if item.kind == "human":
        sessions.update(sid, metadata_patch={STOPPED_KEY: ""})
        return False
    return not any(pending.kind == "human" for pending in history.pending_mail(sid))
