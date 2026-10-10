"""Bounded model-lane projection of complete MCP results.

This bounds the text that enters the MODEL's context. It is deliberately a
DIFFERENT knob from ``limits.tool_result_chars``
(:func:`clio_agent.gact.evidence._bounded_tool_call_result`), which bounds the
preview stored in assistant metadata and shipped to the transcript UI: the two
lanes have different consumers and different failure modes (context budget vs
transcript payload size), so each owns exactly one key and neither can silently
redirect the other. The raw evidence itself is never rewritten by either.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

#: Typed reason logged when an oversize result could not be written to its file.
MODEL_TOOL_RESULT_SPILL_FAILED_REASON = "model_tool_result_spill_failed"


def model_tool_result_chars() -> int:
    """Characters of one MCP tool result the model is shown before the rest goes to a file.

    Config: ``limits.model_tool_result_chars`` /
    ``CLIO_MODEL_TOOL_RESULT_CHARS`` (default 12000). Lower it to protect a
    small context window from one verbose tool; raise it when a model has room.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "limits.model_tool_result_chars",
        env="CLIO_MODEL_TOOL_RESULT_CHARS",
        default=12_000,
        cast=conf.as_int,
    )


def transcript_tool_result_chars() -> int:
    """Character bound on the TRANSCRIPT/evidence preview of one tool result.

    Config: ``limits.tool_result_chars`` / ``CLIO_TOOL_RESULT_CHARS`` (default
    12000). Consumed by :func:`clio_agent.gact.evidence._bounded_tool_call_result`;
    resolved here, beside its model-lane sibling, so a tool that must size its own
    result to survive BOTH lanes unchanged (the shell tool, #887) reads the same
    single source instead of re-declaring the key.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "limits.tool_result_chars",
        env="CLIO_TOOL_RESULT_CHARS",
        default=12_000,
        cast=conf.as_int,
    )


def bounded_model_tool_result(
    text: str, *, root: str | Path | None = None, session_id: str | None = None
) -> str:
    """The model-facing text of one tool result: whole, or its head plus a file for the rest.

    The harness does not decide what the agent needs from a big result. Over
    :func:`model_tool_result_chars`, the full result is written to the session's
    tool-output folder (the shell tool's spill folder, removed with the session) and
    the agent is told: how big it is, that the first characters follow, and where the
    rest is for it to explore. The raw evidence itself is never rewritten.
    """

    max_chars = model_tool_result_chars()
    if len(text) <= max_chars:
        return text
    try:
        path = (
            _spill(text)
            if root is None and session_id is None
            else _spill(text, root=root, session_id=session_id)
        )
        where = f"the full result is in `{path}` for you to explore (read it in parts or search it)"
    except OSError as exc:
        logger.warning(
            "tool result spill failed reason=%s chars=%d error=%r",
            MODEL_TOOL_RESULT_SPILL_FAILED_REASON,
            len(text),
            exc,
        )
        where = f"the full result could not be saved ({exc}), so the rest is not available"
    note = (
        f"[clio: result_spilled] This result is {len(text):,} characters, more than the "
        f"{max_chars:,} shown to you. The first characters follow; {where}."
    )
    from clio_agent.tools import injections  # noqa: PLC0415

    injections.note("result_spilled", note)
    head = text[: max(0, max_chars - len(note) - 2)]
    cut = head.rfind("\n")
    if cut > len(head) // 2:
        head = head[:cut]  # end on a whole line when one is near
    return f"{note}\n\n{head}"


def _spill(text: str, *, root: str | Path | None = None, session_id: str | None = None) -> Path:
    """Write ``text`` to the session's tool-output folder; return the file."""
    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415
    from clio_agent.tools.servers.shell_spill_store import (  # noqa: PLC0415
        active_session_id,
        spill_directory,
    )

    root = root or get_active_tool_workspace_root() or str(Path.cwd())
    folder = spill_directory(root, session_id=session_id or active_session_id())
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".json" if text.lstrip()[:1] in ("{", "[") else ".txt"
    path = folder / f"result-{uuid.uuid4().hex[:12]}{suffix}"
    path.write_text(text, encoding="utf-8")
    return path
