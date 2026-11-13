"""A visible service notice in the live assistant message, from the model-call path.

Model retries and stalls are shown to the person as they happen, so a long wait is
never silent and they can stop the turn. A notice is never sent to the model.
"""

from __future__ import annotations

import uuid
from typing import Any


def post_model_notice(source: str, text: str, **metadata: Any) -> bool:
    """Append a ``notice`` part to the active turn's live assistant message.

    Returns whether a turn was active to show it (off-turn callers have none).
    """
    from clio_agent.gact import context as ctx  # noqa: PLC0415
    from clio_agent.gact.parts import Part  # noqa: PLC0415
    from clio_agent.gact.tool_observer import (  # noqa: PLC0415
        _append_live_assistant_part,
    )
    from clio_agent.runtime.lm_activity import after_live_chunks  # noqa: PLC0415

    app, sid = ctx.active_app(), ctx.active_session_id()
    if app is None or not sid:
        return False
    part = Part(
        id=f"notice_{uuid.uuid4().hex[:12]}",
        type="notice",
        source=source,
        agent_id=ctx.active_react_scope() or "",
        text=text,
        metadata=ctx.stamp_active_try({"actor": "algorithm", **metadata}),
    )
    # Emissions were scheduled cross-thread. Append after those callbacks, so an
    # earlier streamed passage cannot land after the notice.
    after_live_chunks(lambda: _append_live_assistant_part(app, sid, part))
    return True


__all__ = ["post_model_notice"]
