"""Where a viewed image or PDF reaches the model, and whether it did.

``view_image`` / ``view_pdf`` (and ``capture_a2ui_surface``) return a small descriptor;
the agent loop hydrates it into a native media part on its next request. A descriptor
returned as a direct call's result is that call's tool result. A descriptor returned
inside a ``run_code`` script only reaches the script: the script's result is text, so
before this module the model saw the printed metadata dict and never the pixels.

The loop's direct call (:meth:`clio_react._Loop._call`) opens a :data:`CALL_MEDIA` slot.
:func:`clio_agent.gact.code_mode.tool.prepare_script` hands that slot to the script's
nested calls as :data:`SCRIPT_MEDIA`, and :func:`deliver` puts each viewed descriptor in
it; the call's observation records the slot (``media``) and every later fold attaches
the media after the script's text. A background script has no model step to deliver to:
its slot is :data:`DETACHED` and a view there is refused with a sentence saying the
model did not see it, never a descriptor that reads as success.

The descriptor carries the delivery facts the transcript shows per row:
``attached_to_model`` (true), ``delivery`` (``tool_result`` or ``script_result``),
``delivered_step`` (the model step that sees it) and, for a script, ``parent_call_id``.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Final

__all__ = [
    "CALL_MEDIA",
    "DETACHED",
    "SCRIPT_MEDIA",
    "MediaNotShownError",
    "deliver",
    "script_media_max_items",
]

#: The direct model call's attachments: media its nested calls viewed (``None`` outside
#: a model step).
CALL_MEDIA: ContextVar[list[dict[str, Any]] | None] = ContextVar("clio_call_media", default=None)

#: A script's own slot: ``None`` outside a script, the calling ``run_code`` call's
#: :data:`CALL_MEDIA` list in a foreground script, :data:`DETACHED` in a background one.
DETACHED: Final = "detached"
SCRIPT_MEDIA: ContextVar[list[dict[str, Any]] | str | None] = ContextVar(
    "clio_script_media", default=None
)

_DEFAULT_SCRIPT_MEDIA_MAX_ITEMS = 8


class MediaNotShownError(ValueError):
    """A view whose media cannot reach the model; ``reason`` is the typed code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def script_media_max_items() -> int:
    """How many images or PDFs one script may attach to its result (``tools.script_media_max``)."""
    from clio_agent import conf  # noqa: PLC0415

    return max(
        1,
        conf.resolve(
            "tools.script_media_max",
            env="CLIO_TOOLS_SCRIPT_MEDIA_MAX",
            default=_DEFAULT_SCRIPT_MEDIA_MAX_ITEMS,
            cast=conf.as_int,
        ),
    )


def _next_step() -> dict[str, Any]:
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415

    loop = active_loop()
    step = getattr(loop, "step", None)
    return {"delivered_step": step + 1} if isinstance(step, int) and step >= 0 else {}


def deliver(descriptor: dict[str, Any], *, tool: str) -> dict[str, Any]:
    """``descriptor`` with where it reaches the model; a script's view joins its slot.

    Raises:
        MediaNotShownError: in a background script (``<tool>_not_shown``), or past the
            per-script limit (``<tool>_script_limit``); the message says the model did
            not see it and what to do instead.
    """
    from clio_agent.gact.tool_call_nesting import PARENT_CALL_ID  # noqa: PLC0415

    kind = "image" if "image" in str(descriptor.get("media_type") or "") else "PDF"
    script = SCRIPT_MEDIA.get()
    if script is None:
        if CALL_MEDIA.get() is None:
            return descriptor  # no model step: the caller (a test, a host) gets the facts
        return {**descriptor, "attached_to_model": True, "delivery": "tool_result", **_next_step()}
    if not isinstance(script, list):
        raise MediaNotShownError(
            f"{tool}_not_shown",
            f"{tool} ran in a background script, which has no model step to attach to: the "
            f"{kind} was NOT shown to the model. Call {tool} directly, or from a foreground "
            "run_code script, to look at it.",
        )
    limit = script_media_max_items()
    if len(script) >= limit:
        raise MediaNotShownError(
            f"{tool}_script_limit",
            f"This script already attached {limit} images or PDFs to its result: the {kind} "
            f"{descriptor.get('path')!s} was NOT shown to the model. View the rest in a later "
            "step.",
        )
    script.append(dict(descriptor))
    parent = PARENT_CALL_ID.get()
    return {
        **descriptor,
        "attached_to_model": True,
        "delivery": "script_result",
        **({"parent_call_id": parent} if parent else {}),
        **_next_step(),
        "note": (
            f"Not visible inside the script: the {kind} is attached to this run_code "
            "call's result, so you see it on your next step."
        ),
    }
