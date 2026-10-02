"""DSPy's extract for :class:`~clio_agent.gact.agents.clio_react.ClioReAct`, config-driven.

After a loop longer than ``agents.react_extract.after_steps`` model steps (default 3),
the signature outputs the loop did not produce are filled, from the agent's clio-core
context, by literally DSPy's
``ReAct`` extract: ``ChainOfThought`` over the task inputs, those outputs and the
trajectory. Two endings qualify: ``max_iters`` (no answer at all) and a direct answer
on a signature with further outputs (``question -> answer, summary``). The answer the
model already wrote -- and the user already saw -- is never replaced; ``submit`` (the
model's own typed outputs), a yield and a context overflow never extract. Amendment
to ``docs/design/react-loop-completion-2026-09.md``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import dspy
from dspy.lm15 import Message, TextPart, ThinkingPart, ToolCallPart, ToolResultPart

from clio_agent import conf

EXTRACT_REASONS = frozenset({"direct_response", "max_iters"})


def extract_enabled() -> bool:
    """``agents.react_extract.enabled`` / ``CLIO_REACT_EXTRACT_ENABLED`` (default on)."""
    return conf.resolve(
        "agents.react_extract.enabled",
        env="CLIO_REACT_EXTRACT_ENABLED",
        default=True,
        cast=conf.as_bool,
    )


def extract_after_steps() -> int:
    """``agents.react_extract.after_steps`` / ``CLIO_REACT_EXTRACT_AFTER_STEPS`` (default 3)."""
    return conf.resolve(
        "agents.react_extract.after_steps",
        env="CLIO_REACT_EXTRACT_AFTER_STEPS",
        default=3,
        cast=conf.as_int,
    )


def missing_outputs(signature: Any, outputs: dict[str, Any], reason: str, steps: int) -> list[str]:
    """The output fields the extract should fill, empty when it should not run."""
    if reason not in EXTRACT_REASONS or not extract_enabled() or steps <= extract_after_steps():
        return []
    return [name for name in signature.output_fields if outputs.get(name) in (None, "")]


def extract(
    signature: Any,
    inputs: dict[str, Any],
    steps: Sequence[Message],
    missing: list[str],
    lm: Any,
) -> dict[str, Any]:
    """Run DSPy's extract over the trajectory; return the ``missing`` outputs it produced."""
    fields = {
        **{name: signature.input_fields[name] for name in inputs},
        **{name: signature.output_fields[name] for name in missing},
    }
    fallback = dspy.Signature(fields, signature.instructions).append(
        "trajectory", dspy.InputField(), type_=str
    )
    with dspy.context(lm=lm):
        pred = dspy.ChainOfThought(fallback)(**inputs, trajectory=trajectory_text(steps))
    return {name: getattr(pred, name) for name in missing}


def trajectory_text(steps: Sequence[Message]) -> str:
    """The agent's context -- read from clio-core, exactly what the agent saw, the
    harness's additions included -- as the plain-text trajectory the extract reads."""
    lines: list[str] = []
    step = 0
    for message in steps:
        for part in message.parts:
            if isinstance(part, ThinkingPart):
                continue
            if isinstance(part, TextPart) and part.text:
                label = "thought" if message.role == "assistant" else message.role
                lines.append(f"[{label}_{step}] {part.text}")
            elif isinstance(part, ToolCallPart):
                lines.append(f"[tool_call_{step}] {part.name}({_json(part.input)})")
            elif isinstance(part, ToolResultPart):
                lines.append(f"[observation_{step}] {_result_text(part)}")
        if message.role == "tool":
            step += 1
    return "\n".join(lines)


def _result_text(part: ToolResultPart) -> str:
    return "\n".join(
        c.text if isinstance(c, TextPart) else f"<{type(c).__name__}>" for c in part.content
    )


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
