"""The model text of an MCP tool result that carries A2UI (host semantics HS1).

A plugin server's tool result can carry A2UI resources. The host applies them as
server-owned surfaces and shows the model one line per resource instead of the
raw messages. The tools layer knows nothing about surfaces: the host installs a
resolver (:func:`install_resolver`) that returns the live app's observer, and
:func:`model_result` asks it for the notes. Nothing here names a particular server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from clio_agent.tools.mcp_result_projection import bounded_model_tool_result

logger = logging.getLogger(__name__)

#: ``(tool name, raw CallToolResult, source namespace) -> notes``: applies the A2UI
#: resources in the result and returns the one-line note the model sees for each,
#: in content order (``None``: no A2UI).
A2UIResultObserver = Callable[[str, Any, str], "list[str] | None"]

_resolver: Callable[[], A2UIResultObserver | None] | None = None


def install_resolver(resolver: Callable[[], A2UIResultObserver | None]) -> None:
    """Install how the live call finds its app's observer (idempotent; host-owned)."""

    global _resolver
    _resolver = resolver


def _observer() -> A2UIResultObserver | None:
    if _resolver is None:
        return None
    try:
        return _resolver()
    except Exception:  # noqa: BLE001 - no observer is the same as no A2UI
        logger.exception("A2UI result observer lookup failed reason=a2ui_observer_lookup_failed")
        return None


def model_result(name: str, outcome: Any, *, return_raw: bool) -> Any:
    """The bounded model text of ``outcome``, with A2UI resources shown as notes.

    Runs on the caller's thread, which holds the workspace custody the bounding needs.
    """

    model_text = outcome.model_text
    observer = None if return_raw or not outcome.source_namespace else _observer()
    if observer is not None:
        try:
            notes = observer(name, outcome.raw_result, outcome.source_namespace)
        except Exception as exc:  # noqa: BLE001 - the tool itself succeeded
            logger.exception(
                "A2UI server surface admission failed tool=%r "
                "reason=a2ui_server_surface_failed: %s",
                name,
                exc,
            )
            notes = None
        if notes:
            from clio_agent.tools.mcp_result_text import result_to_text  # noqa: PLC0415

            model_text = result_to_text(outcome.raw_result, a2ui_notes=notes)
    return bounded_model_tool_result(model_text)


__all__ = ["A2UIResultObserver", "install_resolver", "model_result"]
