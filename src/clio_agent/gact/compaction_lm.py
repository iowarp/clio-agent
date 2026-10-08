"""The LM a session's compaction summary runs on: the session's pinned model (F011c).

Compaction summarised every session with the host agent's own LM, so a session
pinned to another provider/model (``turn_forward._apply_turn_model_selection``
runs its turns there) had its context summarised by a different model -- and
failed outright when the host model was not being served. The summary is part
of that session's conversation, so it resolves the pin through the SAME
per-turn resolution its turns use (``builders._dynamic_agent_lm_config``),
credential resolved fresh at call time.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

logger = logging.getLogger(__name__)

__all__ = ["pinned_summary_lm"]


def pinned_summary_lm(app: Any, sid: str) -> Any | None:
    """A fresh LM for the session's pinned model, or ``None`` to use the host LM.

    ``None`` when the session pins nothing or pins the active global LM itself.

    Raises:
        Exception: the pinned model's endpoint/credential cannot be resolved; the
            caller reports it typed (never silently summarises on another model).
    """

    from clio_agent.config import create_lm  # noqa: PLC0415
    from clio_agent.gact.agents.builders import _dynamic_agent_lm_config  # noqa: PLC0415
    from clio_agent.gact.providers.config import (  # noqa: PLC0415
        _model_ref_dict,
        _model_ref_is_empty,
        _model_ref_matches_active,
    )

    session = app.state.sessions.get(sid)
    pinned = getattr(session, "model", None)
    if pinned is None or _model_ref_is_empty(pinned) or _model_ref_matches_active(pinned, app):
        return None
    ref = _model_ref_dict(pinned)
    selection = SimpleNamespace(
        id="compaction", default_provider=ref["provider_id"], default_model=ref["model_id"]
    )
    config = _dynamic_agent_lm_config(app.state.agent, cast(Any, selection)).materialize()
    logger.info(
        "compaction summary lm reason=session_pinned_model session=%s provider=%s model=%s",
        sid,
        ref["provider_id"],
        ref["model_id"],
    )
    return create_lm(config)
