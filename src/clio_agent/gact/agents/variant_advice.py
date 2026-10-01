"""Refine's advice, owned by clio (Phase 9).

DSPy's ``Refine`` writes per-predictor advice and delivers it as a ``hint_`` input through
an adapter wrapper. ``ClioReAct`` has no named predictors and no adapter (each step is one
``lm(Request)``), so that advice never reached the next try. clio writes it instead: one
LM call reads the previous try from clio-core -- exactly what that try saw and did, its
answer and its score -- and the advice is recorded on the next try's own scope as a CLIO
addition (``variant_advice``): told to the model, shown in the UI like every harness
addition.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from dspy.lm15 import Message, Request, TextPart

from clio_agent.errors import ClioError
from clio_agent.gact.agents.clio_react_extract import trajectory_text

ADVICE_SOURCE = "variant_advice"

_SYSTEM = (
    "You review one attempt at a task that fell short of its target score. Write brief, "
    "concrete advice for the next attempt: what to change and what to keep. Reply with the "
    "advice only."
)


class VariantAdviceError(ClioError):
    """The advice for the next Refine try could not be written."""

    reason = "variant_advice_failed"

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"Refine could not write advice for the next try: {detail}",
            error_type=self.reason,
            details={"detail": detail},
        )


def advise(
    lm: Any,
    *,
    task: Mapping[str, Any],
    attempt: Sequence[Message],
    answer: str,
    score: float,
    threshold: float | None,
) -> str:
    """Advice for the next try, from the previous try as clio-core recorded it."""
    target = "none" if threshold is None else f"{threshold:.2f}"
    prompt = (
        f"Task inputs:\n{json.dumps(dict(task), ensure_ascii=False, default=str)}\n\n"
        f"The attempt, step by step:\n{trajectory_text(attempt) or '(no steps)'}\n\n"
        f"Its answer:\n{answer or '(empty)'}\n\n"
        f"Its score: {score:.2f} (target {target})"
    )
    response = lm(Request(model=lm.model, system=_SYSTEM, messages=(Message.user(prompt),)))
    text = "".join(p.text for p in response.message.parts if isinstance(p, TextPart)).strip()
    if not text:
        raise VariantAdviceError("the model returned no advice")
    return text
