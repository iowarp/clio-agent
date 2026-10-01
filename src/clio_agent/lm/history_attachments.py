"""Native image / PDF attachments inside a ``dspy.History`` (every clio adapter).

A ``view_image`` / ``view_pdf`` tool result is stored as a small workspace reference;
the bytes are hydrated into the provider request only when the history is formatted,
and the tool message is promoted to the provider's native attachment shape. One
running byte total spans both kinds per request, so an image-heavy step plus a PDF
cannot together exceed the aggregate ceiling.
"""

from __future__ import annotations

from typing import Any

__all__ = ["HistoryAttachmentsMixin", "hydrate_history_attachments"]


def hydrate_history_attachments(inputs: dict[str, Any], history_field_name: str) -> None:
    """Hydrate the history's image and PDF tool results in place (shared byte budget)."""
    from clio_agent.gact.view_image_tool import hydrate_view_image_results  # noqa: PLC0415
    from clio_agent.gact.view_pdf_tool import hydrate_view_pdf_results  # noqa: PLC0415

    native_attachment_bytes = [0]
    hydrate_view_image_results(
        inputs, history_field_name, running_total_bytes=native_attachment_bytes
    )
    hydrate_view_pdf_results(
        inputs, history_field_name, running_total_bytes=native_attachment_bytes
    )


class HistoryAttachmentsMixin:
    """Hydrate and promote native attachments before the DSPy adapter formats history."""

    def format_conversation_history(
        self, signature: Any, history_field_name: str, inputs: dict[str, Any]
    ) -> Any:
        """Hydrate attachments, format with the DSPy adapter, promote attachment messages."""
        from clio_agent.gact.view_image_tool import (
            promote_view_image_tool_messages,  # noqa: PLC0415
        )
        from clio_agent.gact.view_pdf_tool import promote_view_pdf_tool_messages  # noqa: PLC0415

        hydrate_history_attachments(inputs, history_field_name)
        messages = super().format_conversation_history(  # type: ignore[misc]
            signature, history_field_name, inputs
        )
        return promote_view_pdf_tool_messages(promote_view_image_tool_messages(messages))
