"""Expert-runtime helpers shared by the GACT agent builders (#714).

The loop itself is :class:`clio_agent.gact.agents.clio_react.ClioReAct`; this module
keeps the compaction summarizer (``_summarize_segments_llm``). Imports only the shared runtime base and
stdlib / lazy ``dspy`` -- never ``gact.app`` -- so the dependency graph stays acyclic.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "_summarize_segments_llm",
]


def _summarize_segments_llm(
    segments: list[Any], *, owning_lm: Any = None, owning_adapter: Any = None
) -> str:
    """Summarize live segments into a compact text that preserves what's needed to
    continue the task.

    Resolves the LM through :func:`resolve_active_lm` and passes it to
    ``dspy.Predict`` *explicitly* so the summarisation runs on the active profile's
    bound LM. When invoked outside any ``dspy.context`` (e.g. the ``/context``
    compaction route rather than an expert ``forward``) it falls through to the
    process boot-default LM and records a structured ``ambient_lm_default`` reason,
    so the miss is queryable and never silent (per the per-expert-provider sweep).
    Returns '' on failure (caller then skips compaction and keeps the reactive
    backstop).
    """
    import dspy  # noqa: PLC0415

    from clio_agent.arc.schema import segment_text  # noqa: PLC0415
    from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415
    from clio_agent.lm.secondary import resolve_secondary_lm  # noqa: PLC0415

    body = "\n".join(segment_text(s) for s in segments)
    sig = dspy.Signature(
        "prior_context -> summary",
        "Summarize the prior reasoning steps, tool calls, and observations into a "
        "compact summary that preserves every fact, result, and decision needed to "
        "continue the task. Be concise but lose no actionable information.",
    )
    caller_lm = resolve_active_lm(site="agents.runtime._summarize_segments_llm", explicit=owning_lm)
    adapter = owning_adapter or getattr(dspy.settings, "adapter", None)
    try:
        route = resolve_secondary_lm("summarizer", caller_lm=caller_lm, caller_adapter=adapter)
        predict = dspy.Predict(sig)
        with dspy.context(lm=route.lm, adapter=route.adapter):
            result = predict(prior_context=body, lm=route.lm)
        return str(getattr(result, "summary", "") or "").strip()
    except Exception:  # noqa: BLE001
        logger.warning("arc auto-compaction summary LLM call failed", exc_info=True)
        return ""
