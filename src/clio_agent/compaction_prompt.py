"""The compaction summarizer prompt: a Markdown template file researchers can swap.

The prompt the summarizer LM gets is read from ``compaction.prompt_file``
(``CLIO_COMPACTION_PROMPT_FILE``) at EVERY compaction, so an edit applies without a
restart. Unset, it is the packaged default ``prompt_packs/builtin/compaction.md``.

The template is formatted with exactly three placeholders:

* ``{transcript}`` (required) -- what the summary replaces, one line per message part;
* ``{focus}`` -- ``""``, or a paragraph ``"\\n\\nFocus the summary on: <text>"`` when the
  user asked for a focus (manual compaction);
* ``{files}`` -- ``""``, or the block ``"\\n\\n--- attached session files ---\\n...\\n---
  end files ---"`` listing the session's attached files.

``{focus}`` and ``{files}`` carry their own leading blank line, so a template that puts
them right after the rules renders no empty section. Any other ``{...}`` is an error;
write a literal brace as ``{{`` or ``}}``. A configured file that is missing,
unreadable or invalid is a typed :class:`CompactionPromptError` naming the path and
the problem -- never a fallback to the packaged default.
"""

from __future__ import annotations

import string
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent import conf
from clio_agent.errors import ClioError

if TYPE_CHECKING:
    from clio_agent.runtime.status import IntegrationStatus

__all__ = [
    "PLACEHOLDERS",
    "CompactionPromptError",
    "configured_prompt_path",
    "load_prompt_template",
    "probe_compaction_prompt",
    "render_compaction_prompt",
]

#: The placeholders a template may use (``transcript`` is required).
PLACEHOLDERS = frozenset({"transcript", "focus", "files"})


class CompactionPromptError(ClioError):
    """The compaction prompt file cannot be used (missing, unreadable or invalid)."""

    reason = "compaction_prompt_invalid"

    def __init__(self, path: Path, problem: str) -> None:
        self.path = path
        self.problem = problem
        super().__init__(
            f"the compaction prompt file {path} cannot be used: {problem}",
            error_type=self.reason,
            details={"path": str(path), "problem": problem},
        )


def _packaged_default() -> Path:
    return Path(str(resources.files("clio_agent.prompt_packs.builtin") / "compaction.md"))


def configured_prompt_path() -> tuple[Path, str]:
    """``(path, source)``: the configured prompt file, or the packaged default."""
    value = conf.resolve(
        "compaction.prompt_file",
        env="CLIO_COMPACTION_PROMPT_FILE",
        default="",
        cast=conf.as_str,
    )
    if value.strip():
        return Path(value.strip()).expanduser(), "compaction.prompt_file"
    return _packaged_default(), "packaged default"


def _validate(path: Path, template: str) -> None:
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:  # unbalanced braces
        raise CompactionPromptError(path, f"invalid template: {exc}") from exc
    names = {name for _text, name, _spec, _conv in parsed if name is not None}
    for _text, name, spec, conversion in parsed:
        if name is not None and (name not in PLACEHOLDERS or spec or conversion):
            raise CompactionPromptError(
                path,
                f"unknown placeholder {{{name}}}; use {{transcript}}, {{focus}}, {{files}} "
                "(write a literal brace as {{ or }})",
            )
    if "transcript" not in names:
        raise CompactionPromptError(path, "the template has no {transcript} placeholder")


def load_prompt_template() -> tuple[Path, str]:
    """Read and validate the configured template (fresh from disk on every call).

    Raises:
        CompactionPromptError: the file is missing, unreadable, not UTF-8, or invalid.
    """
    path, _source = configured_prompt_path()
    try:
        template = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CompactionPromptError(path, "the file does not exist") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise CompactionPromptError(path, f"the file cannot be read: {exc}") from exc
    _validate(path, template)
    return path, template


def render_compaction_prompt(transcript: str, *, focus: str = "", files: str = "") -> str:
    """The summarizer prompt for ``transcript`` from the configured template.

    Args:
        transcript: The rendered lines the summary replaces.
        focus: The user's focus instructions, or ``""``.
        files: The attached-file inventory lines, or ``""``.

    Raises:
        CompactionPromptError: the configured template cannot be used.
    """
    _path, template = load_prompt_template()
    values = {
        "transcript": transcript,
        "focus": f"\n\nFocus the summary on: {focus}" if focus else "",
        "files": f"\n\n--- attached session files ---\n{files}\n--- end files ---" if files else "",
    }
    return template.format_map(values)


def probe_compaction_prompt() -> "IntegrationStatus":
    """A doctor row: the configured compaction prompt file loads and validates."""
    from clio_agent.runtime.status import IntegrationState, IntegrationStatus  # noqa: PLC0415

    path, source = configured_prompt_path()
    try:
        load_prompt_template()
    except CompactionPromptError as exc:
        return IntegrationStatus(
            name="compaction_prompt",
            state=IntegrationState.MISCONFIGURED,
            summary=str(exc),
            config_source=source,
            next_action=(
                "fix the file, or unset compaction.prompt_file / CLIO_COMPACTION_PROMPT_FILE "
                "to use the packaged default"
            ),
            details=dict(exc.details or {}),
            required=False,
        )
    return IntegrationStatus(
        name="compaction_prompt",
        state=IntegrationState.READY,
        summary=f"compaction summarizer prompt: {path}",
        config_source=source,
        next_action="none",
        details={"path": str(path)},
        required=False,
    )
