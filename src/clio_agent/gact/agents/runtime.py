"""Trajectory-retaining ReAct runtime for the GACT server (#714).

This module owns the *expert runtime engine* carved out of
``clio_agent.gact.app``: the :class:`dspy.ReAct` subclass that retains its
trajectory across a failed final ``extract`` (so the failure can be captured and
repaired) and drives the ARC live-context plane (writing the working-set
trajectory + reading its prompt back from ARC, with proactive auto-compaction).

The retaining subclass is built lazily and cached per ``dspy.ReAct`` base class
(:func:`_retaining_react_cls`) so test fakes that monkeypatch ``dspy.ReAct`` get
a fresh, correct subclass. ``forward`` mirrors the pinned dspy ReAct loop
verbatim, emitting the per-step / per-expert semantic-event highway records and
publishing the retained trajectory *before* ``extract`` runs.

Imports only the shared runtime base (:mod:`clio_agent.gact.runtime`: the
semantic-event funnel + ``gact.context`` boundary + token/context-window leaves)
and stdlib / lazy ``dspy`` -- never ``gact.app`` -- so the dependency graph stays
acyclic. The expert/blueprint *builders* that instantiate this runtime live in
:mod:`clio_agent.gact.agents.builders`.
"""

from __future__ import annotations

import logging
from typing import Any

# Monkeypatch seam: ReActV2's `_maybe_autocompact` resolves the token/summary
# helpers via THIS module (`_rt._last_prompt_tokens()` in reactv2.py) so tests
# can patch the owner in one place. Re-exported, not defined here.
from clio_agent.gact.runtime.context_tokens import _last_prompt_tokens

logger = logging.getLogger(__name__)

__all__ = [
    "_last_prompt_tokens",
    "_prediction_structured_metadata",
    "_retaining_react_cls",
    "_summarize_segments_llm",
]


def _prediction_structured_metadata(result: Any) -> dict[str, Any]:
    return {
        key: getattr(result, key)
        for key in ("workflow_state", "evidence", "artifacts", "errors", "delegation")
        if getattr(result, key, None) not in (None, "")
    }


def _summarize_segments_llm(segments: list[Any]) -> str:
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

    body = "\n".join(segment_text(s) for s in segments)
    sig = dspy.Signature(
        "prior_context -> summary",
        "Summarize the prior reasoning steps, tool calls, and observations into a "
        "compact summary that preserves every fact, result, and decision needed to "
        "continue the task. Be concise but lose no actionable information.",
    )
    lm = resolve_active_lm(site="agents.runtime._summarize_segments_llm")
    try:
        predict = dspy.Predict(sig)
        result = (
            predict(prior_context=body, lm=lm) if lm is not None else predict(prior_context=body)
        )
        return str(getattr(result, "summary", "") or "").strip()
    except Exception:  # noqa: BLE001
        logger.warning("arc auto-compaction summary LLM call failed", exc_info=True)
        return ""


def _retaining_react_cls() -> Any:
    """Return the production expert-loop class: clio's ReActV2 subclass.

    Single path (#901 shipped the flip; the v0.8.0 cleanup deleted the classic
    ``_RetainingReAct`` and its ``CLIO_REACTV2`` kill-switch): ``_RetainingReActV2``
    (:mod:`clio_agent.gact.agents.reactv2`) — append-only History keeps the
    provider prompt prefix byte-stable across iterations (#891) and ARC ops are
    the sole prefix-reset authors. Constructor shape
    ``Cls(signature, tools=..., max_iters=...)`` at every call site.
    """
    from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls  # noqa: PLC0415

    return retaining_reactv2_cls()
