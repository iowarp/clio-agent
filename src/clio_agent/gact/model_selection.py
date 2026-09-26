"""Which models may be SELECTED as the chat model (role: general vs surrogate).

Every model is a first-class catalog row -- embedding, rerank, classification,
image/video generation and speech models are listed with their real
availability and their ``task``/``role`` facts. Only choosing a SURROGATE as
the chat model is refused, with the typed ``surrogate_model_not_chat`` error;
an unknown role is never refused (a chat endpoint offered the model).
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.modality_evidence import catalog_model_rows
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, ModelRef
from clio_agent.providers.capabilities.records import role_for_task

#: The typed error code for choosing a surrogate as the chat model.
SURROGATE_NOT_CHAT = "surrogate_model_not_chat"


def model_role_and_task(app: Any, model: ModelRef) -> tuple[str | None, str | None]:
    """``(role, task)`` for one provider/model selection, or ``(None, None)`` if unknown.

    Reads the catalog row the picker itself shows; with no row, the live
    handshake's effective capabilities for that exact provider.
    """
    for row in catalog_model_rows(app, model):
        task = row.get("task")
        if isinstance(task, str) and task:
            return role_for_task(task), task
    report = getattr(app.state, "lm_handshake_report", None)
    if report is not None and getattr(report, "ok", False) and report.provider_id == model.provider_id:
        discovered = report.model(model.model_id)
        if discovered is not None:
            from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
                get_effective_capabilities,
            )

            effective = get_effective_capabilities(
                report.provider_id, report.api_base, discovered.id
            )
            if effective.task.known and effective.task.value:
                return role_for_task(effective.task.value), effective.task.value
    return None, None


def surrogate_selection_error(app: Any, provider_id: str, model_id: str) -> ErrorEnvelope | None:
    """The typed refusal for selecting a surrogate as the chat model, else None."""
    if not provider_id or not model_id:
        return None
    role, task = model_role_and_task(app, ModelRef(provider_id=provider_id, model_id=model_id))
    if role != "surrogate":
        return None
    return ErrorEnvelope(
        error=ErrorInfo(
            error=SURROGATE_NOT_CHAT,
            message=(
                f"{model_id} is a {task} model, not a chat model; it is listed in the "
                "catalog but cannot run a chat turn. Choose a general (chat) model."
            ),
            details={
                "provider": provider_id,
                "model": model_id,
                "task": task,
                "role": role,
                "recovery_actions": ["choose_chat_model"],
            },
            recoverable=True,
        )
    )


__all__ = ["SURROGATE_NOT_CHAT", "model_role_and_task", "surrogate_selection_error"]
