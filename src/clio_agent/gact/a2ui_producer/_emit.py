"""Transcript emission for producer-tool results (moved from ``a2ui_tools.py``, S4)."""

from __future__ import annotations

from typing import Any


def emit_surface_part(app: Any, session_id: str, part: Any) -> bool:
    """Append a typed A2UI reference in live transcript order."""

    from clio_agent.gact.tool_observer import (  # noqa: PLC0415
        _append_live_assistant_part,
        _mirror_transcript_state,
        _session_turn_transcript,
    )

    transcript = _session_turn_transcript(app, session_id)
    if transcript is None:
        _append_live_assistant_part(app, session_id, part)
        return True
    appended = transcript.append_part(part)
    if appended is None:
        return False
    _mirror_transcript_state(app, session_id, transcript)
    return True


__all__ = ["emit_surface_part"]
