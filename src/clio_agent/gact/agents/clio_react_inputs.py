"""Bind a ClioReAct forward's kwargs to the signature's declared input fields.

The runtime hands every expert its task under fixed kwarg names (``question``, plus
``system_prompt`` and the ``images``/``files`` media). ``ClioReAct`` keeps only the
kwargs its signature declares. An expert whose declared task input is named
something else (``request:`` instead of ``question:``) used to keep NONE of them and
run with an empty task, which the model then correctly refused to invent -- a config
typo that presented as a semantic non-answer (#1267). :func:`bind_task_inputs`
refuses that case with a typed error naming both sides.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.errors import ClioError

__all__ = ["MEDIA_INPUTS", "SYSTEM_INPUT", "ExpertInputsUnmappableError", "bind_task_inputs"]

SYSTEM_INPUT = "system_prompt"
MEDIA_INPUTS = ("images", "files")


class ExpertInputsUnmappableError(ClioError):
    """None of an expert's declared task inputs is among the kwargs it was called with."""

    reason = "expert_inputs_unmappable"

    def __init__(self, declared: list[str], offered: list[str]) -> None:
        super().__init__(
            f"the expert declares task input(s) {declared} but was called with {offered}; "
            "none match, so it would run with no task. Rename the expert's input field "
            "to one the runtime supplies (usually 'question').",
            error_type=self.reason,
            # details["reason"] is what the turn copies into the child's error
            # envelope (gact/turn.py ClioError branch), so the parent sees the cause.
            details={
                "reason": self.reason,
                "declared_inputs": declared,
                "offered_inputs": offered,
            },
        )


def bind_task_inputs(signature: Any, input_args: Mapping[str, Any]) -> dict[str, Any]:
    """Return the kwargs ``signature`` declares, refusing a task-less binding.

    Args:
        signature: The expert's DSPy signature (its ``input_fields`` are declared).
        input_args: The forward's kwargs, after loop controls are removed.

    Returns:
        ``{name: value}`` for every declared input present in ``input_args``.

    Raises:
        ExpertInputsUnmappableError: The signature declares at least one task input
            (anything but the system prompt and media) and none was supplied.
    """

    inputs = {name: input_args[name] for name in signature.input_fields if name in input_args}
    declared = [
        name for name in signature.input_fields if name not in (SYSTEM_INPUT, *MEDIA_INPUTS)
    ]
    if declared and not any(name in inputs for name in declared):
        raise ExpertInputsUnmappableError(declared, sorted(input_args))
    return inputs
