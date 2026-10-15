"""Read actions from folded A2UI surfaces for the interaction projection."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import UserQuestion


def _question_correlation(app: "FastAPI", question: "UserQuestion") -> dict[str, str]:
    """Resolve tool/task correlation, including a forwarded child question."""
    metadata = question.metadata if isinstance(question.metadata, Mapping) else {}
    elicitation = metadata.get("elicitation")
    elicitation = elicitation if isinstance(elicitation, Mapping) else {}
    child = app.state.user_questions.get(str(metadata.get("forwarded_from_question") or ""))
    child_meta = child.metadata if isinstance(getattr(child, "metadata", None), Mapping) else {}
    child_elicitation = child_meta.get("elicitation")
    child_elicitation = child_elicitation if isinstance(child_elicitation, Mapping) else {}
    return {
        "tool_name": str(
            metadata.get("tool_name")
            or elicitation.get("tool_name")
            or child_meta.get("tool_name")
            or child_elicitation.get("tool_name")
            or ""
        ),
        "invocation_id": str(
            metadata.get("invocation_id")
            or elicitation.get("invocation_id")
            or child_meta.get("invocation_id")
            or child_elicitation.get("invocation_id")
            or ""
        ),
        "task_id": str(
            metadata.get("task_id")
            or elicitation.get("task_id")
            or child_meta.get("task_id")
            or child_elicitation.get("task_id")
            or ""
        ),
        "input_key": str(elicitation.get("input_key") or child_elicitation.get("input_key") or ""),
    }


def _task_id_for_owner(app: "FastAPI", owner: str) -> str:
    """Return the newest registered agent task belonging to this session."""
    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return ""
    tasks = [task for task in registry.snapshot() if task.child_session_id == owner]
    tasks.sort(key=lambda task: task.created_at, reverse=True)
    return tasks[0].task_id if tasks else ""


def _surface_actions(surface: Mapping[str, Any]) -> list[str]:
    """Return every action name a folded surface's messages declare.

    Pre-S2 this filtered against a closed ``SERVER_ACTIONS`` Literal; the
    catalog file is now the allowlist (docs/design/a2ui-compat-campaign-
    2026-09.md S2) and ANY non-empty event name is a legal action, so this
    just reports what the surface actually offers rather than re-deriving a
    trust decision the server already made at ``updateComponents`` time.
    """

    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            event = value.get("event")
            if isinstance(event, Mapping):
                name = str(event.get("name") or "")
                if name:
                    found.add(name)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(surface.get("messages") or [])
    return sorted(found)


def _latest_action_record(surface: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return this surface's most recent ``a2ui_action`` record, if any (S5).

    ``A2UIStore``'s S5 fold (``gact/a2ui_actions/record.py::
    fold_action_records``) already orders ``surface["actions"]`` oldest first
    and keeps only each record's LATEST snapshot -- this just takes the last
    entry, the durable, idempotent, correlated record replacing the deleted
    ``/lastAction`` data-model ack this projection used to read.
    """

    actions = surface.get("actions") or []
    return dict(actions[-1]) if actions else None
