"""CLIO's notes about one tool or model call, as reminder templates.

What the harness says about a call -- a hook changed it, the tool keeps failing -- is
the reminder ``clio.reminder.hook_<piece>`` or ``clio.reminder.circuit_breaker_<piece>``
(``prompt_packs/builtin/reminders/``; :mod:`clio_agent.gact.reminders`), so a person can
reword it on the Prompts page. The code decides when a note is due and supplies its
data; the template is what it says. The fixed markers (``[clio: hook]`` ...) stay in
code: the transcript and other code recognise a note by them.

The hook notes are required reminders (they can be reworded, never switched off); the
failure warning is a fact that can be switched off, and nothing is ever blocked. A leaf module: GACT is imported only when a note is rendered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "CIRCUIT_BREAKER_MARKER",
    "HOOK_MARKER",
    "failure_warning_after",
    "circuit_warning_note",
    "hook_note",
]

#: Heads every note about a hook's effect on a call.
HOOK_MARKER = "[clio: hook] "
#: Heads the circuit breaker's notes.
CIRCUIT_BREAKER_MARKER = "[clio: circuit_breaker] "


def _note(marker: str, source: str, variables: Mapping[str, Any]) -> str:
    """``marker`` and the reminder's text; ``""`` when the person switched it off."""
    from clio_agent.gact import reminders  # noqa: PLC0415 - tools load before gact

    text = reminders.render(source, variables)
    return f"{marker}{text}" if text is not None else ""


def hook_note(piece: str, **variables: Any) -> str:
    """The ``[clio: hook]`` note ``hook_<piece>`` (``model_answered``, ``objected`` ...)."""
    return _note(HOOK_MARKER, f"hook_{piece}", variables)


def failure_warning_after() -> int:
    """Failures in a row before the warning (``x_param_after``; 0 never warns)."""
    from clio_agent.gact import reminders  # noqa: PLC0415 - tools load before gact

    return reminders.param("circuit_breaker_warning", "after", 3, int)


def circuit_warning_note(name: str, count: int) -> str:
    """The fact at ``after`` failures in a row; ``""`` when the person switched it off."""
    return _note(CIRCUIT_BREAKER_MARKER, "circuit_breaker_warning", {"name": name, "count": count})
