"""The compaction prompt: part of CLIO core, next to the compaction code.

The rendered prompt is the last user message of a request that is otherwise the agent's
own context (:mod:`clio_agent.gact.compaction_summarizer`), so the model already sees
the history it replaces; the template (``gact/compaction_prompt.md``) only says what to
write. Placeholders: ``{{focus}}`` (``""`` or ``"\n\nFocus the summary on: <text>"``,
manual compaction) and ``{{files}}`` (``""`` or the attached-file block). The former
``compaction.prompt_file`` key is gone: a value still set for it fails the compaction
typed, so an old override is never ignored silently.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent import conf
from clio_agent.errors import ClioError

if TYPE_CHECKING:
    from clio_agent.runtime.status import IntegrationStatus

__all__ = [
    "PLACEHOLDERS",
    "PROMPT_ID",
    "CompactionPromptError",
    "load_prompt_template",
    "probe_compaction_prompt",
    "render_compaction_prompt",
]

#: The prompt's name in errors and the doctor row.
PROMPT_ID = "clio.compaction"
_TEMPLATE = Path(__file__).parent / "gact" / "compaction_prompt.md"
#: The placeholders the template may use.
PLACEHOLDERS = frozenset({"focus", "files"})
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
#: The removed setting; still set, it is reported, never ignored.
_REMOVED_KEY, _REMOVED_ENV = "compaction.prompt_file", "CLIO_COMPACTION_PROMPT_FILE"


class CompactionPromptError(ClioError):
    """The compaction prompt cannot be used (missing, invalid, or set the removed way)."""

    reason = "compaction_prompt_invalid"

    def __init__(self, scope: str, problem: str) -> None:
        self.scope = scope
        self.problem = problem
        super().__init__(
            f"the compaction prompt {PROMPT_ID} ({scope}) cannot be used: {problem}",
            error_type=self.reason,
            details={"prompt_id": PROMPT_ID, "scope": scope, "problem": problem},
        )


def _removed_setting() -> str:
    value = conf.resolve(_REMOVED_KEY, env=_REMOVED_ENV, default="", cast=conf.as_str)
    return value.strip()


def load_prompt_template(*, app: Any = None, session_id: str = "") -> tuple[str, str]:
    """``("core", text)``: the core template.

    Raises:
        CompactionPromptError: the removed file setting is still set.
    """
    if old := _removed_setting():
        raise CompactionPromptError(
            "settings",
            f"{_REMOVED_KEY} ({_REMOVED_ENV}) was removed; it is set to {old!r}. "
            "Unset it: the compaction prompt is part of CLIO core",
        )
    return "core", _TEMPLATE.read_text(encoding="utf-8")


def render_compaction_prompt(
    *, focus: str = "", files: str = "", app: Any = None, session_id: str = ""
) -> str:
    """The compaction prompt this session sees, filled in.

    Raises:
        CompactionPromptError: the template cannot be used.
    """
    _scope, template = load_prompt_template(app=app, session_id=session_id)
    values = {
        "focus": f"\n\nFocus the summary on: {focus}" if focus else "",
        "files": f"\n\n--- attached session files ---\n{files}\n--- end files ---" if files else "",
    }
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template).strip()


def probe_compaction_prompt() -> "IntegrationStatus":
    """A doctor row: the compaction prompt resolves and validates."""
    from clio_agent.runtime.status import IntegrationState, IntegrationStatus  # noqa: PLC0415

    try:
        scope, _text = load_prompt_template()
    except CompactionPromptError as exc:
        return IntegrationStatus(
            name="compaction_prompt",
            state=IntegrationState.MISCONFIGURED,
            summary=str(exc),
            config_source=exc.scope,
            next_action=f"unset {_REMOVED_KEY}",
            details=dict(exc.details or {}),
            required=False,
        )
    return IntegrationStatus(
        name="compaction_prompt",
        state=IntegrationState.READY,
        summary=f"compaction prompt: {PROMPT_ID} ({scope})",
        config_source=scope,
        next_action="none",
        details={"prompt_id": PROMPT_ID, "scope": scope},
        required=False,
    )
