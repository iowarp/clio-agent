"""Disclosure and notification collection for native task-tool observations."""

from typing import Any

from clio_agent.gact.task_access import TaskAccess
from clio_agent.gact.task_publication import publish_task_notice
from clio_agent.gact.tool_observation import CollectedTaskResult


def collected(app: Any, caller: Any, value: dict[str, Any]) -> CollectedTaskResult:
    """Carry authorized completion identities to the existing input-commit guard."""
    if value.get("result_kind") == "shared_evidence":
        return CollectedTaskResult(value, ())
    handles = {
        row["handle"]
        for row in value.get("outcomes", [])
        if row.get("handle")
        and row.get("pending") is False
        and not row.get("error")
        and row.get("status") != "task_state_uncertain"
    }
    if value.get("handle") and value.get("pending") is False:
        handles.add(value["handle"])
    read_handles = handles | ({value["handle"]} if value.get("handle") else set())
    observe_handles: tuple[str, ...] = ()
    if isinstance(value, CollectedTaskResult):
        read_handles.update(value.read_handles)
        observe_handles = value.observe_handles
    with app.state.task_history.serialized():
        for handle in handles:
            current = TaskAccess(app.state.task_history, caller).require(handle, "read")
            publish_task_notice(app.state.task_history, current)
        identities = tuple(
            item.id
            for item in app.state.task_history.pending_mail(caller.session_id)
            if item.handle in handles and item.kind in {"task_result", "question_answer"}
        )
    return CollectedTaskResult(
        value, identities, tuple(sorted(read_handles)), observe_handles=observe_handles
    )
