"""The compaction prompt: the registry prompt ``clio.compaction``, editable like a reminder.

The rendered prompt is the last user message of a request that is otherwise the agent's
own context (:mod:`clio_agent.gact.compaction_summarizer`), so the model already sees the
history it replaces; the template only says what to write. Changing it never costs the
cached prefix.

The template ships in ``prompt_packs/builtin/templates/clio.compaction.md`` and resolves
through the prompt registry like the reminders: a person edits or overrides it per
workspace or session on the Prompts page, and the session's own copy wins. It is read
at every compaction. Its placeholders (``x_variables``):

* ``{{focus}}`` -- ``""``, or ``"\\n\\nFocus the summary on: <text>"`` when the user asked
  for a focus (manual compaction);
* ``{{files}}`` -- ``""``, or the block ``"\\n\\n--- attached session files ---\\n...\\n---
  end files ---"`` listing the session's attached files.

Both carry their own leading blank line. Any other ``{{...}}`` is an error. A template
that cannot be used is a typed :class:`CompactionPromptError` naming the prompt, its
scope and the problem -- never a fallback to the shipped text. The former
``compaction.prompt_file`` key is gone: a value still set for it is reported the same
way, so an old override is never ignored silently.
"""

from __future__ import annotations

import re
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

#: The registry prompt the compaction request ends with.
PROMPT_ID = "clio.compaction"
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
    """``(scope, text)`` of the template this session sees, validated.

    Raises:
        CompactionPromptError: the removed file setting is still set, the prompt is
            missing, or it uses an unknown placeholder.
    """
    if old := _removed_setting():
        raise CompactionPromptError(
            "settings",
            f"{_REMOVED_KEY} ({_REMOVED_ENV}) was removed; it is set to {old!r}. Edit "
            f"{PROMPT_ID} on the Prompts page instead, then unset it",
        )
    from clio_agent.gact.reminders import registry_for  # noqa: PLC0415

    resolved = registry_for(app, session_id).resolve(PROMPT_ID)
    if resolved is None:
        raise CompactionPromptError("registry", "no such prompt")
    if resolved.validation_errors:  # an invalid copy: the registry kept it off the prompt
        invalid = ", ".join(str(p) for p in resolved.metadata.get("invalid_sources") or [])
        raise CompactionPromptError(
            "override" if invalid else resolved.scope,
            "; ".join(resolved.validation_errors) + (f" ({invalid})" if invalid else ""),
        )
    names = {m.group(1) for m in _PLACEHOLDER.finditer(resolved.text)}
    unknown = sorted(names - set(PLACEHOLDERS))
    if unknown:
        raise CompactionPromptError(
            resolved.scope,
            f"unknown placeholder {{{{{unknown[0]}}}}}; use {{{{focus}}}} or {{{{files}}}} "
            "(the model reads the history itself)",
        )
    return resolved.scope, resolved.text


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
            next_action=f"fix {PROMPT_ID} on the Prompts page (or unset {_REMOVED_KEY})",
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
