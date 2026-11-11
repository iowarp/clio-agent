"""Bounded model-lane projection of complete MCP results.

This bounds the text that enters the MODEL's context, to the room one result has in
the calling model's context window (:mod:`clio_agent.tools.result_room`). It is
deliberately a DIFFERENT bound from ``limits.tool_result_chars``
(:func:`clio_agent.gact.evidence._bounded_tool_call_result`), which bounds the
preview stored in assistant metadata and shipped to the transcript UI: the two
lanes have different consumers and different failure modes (context budget vs
transcript payload size), so each owns its own bound and neither can silently
redirect the other. The raw evidence itself is never rewritten by either.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from pathlib import Path

from clio_agent.runtime import trace
from clio_agent.tools.result_room import ResultRoom, result_room

logger = logging.getLogger(__name__)

#: Typed reason logged when an oversize result could not be written to its file.
MODEL_TOOL_RESULT_SPILL_FAILED_REASON = "model_tool_result_spill_failed"
#: The injection source, and the fixed marker that opens a cut result's note.
RESULT_SPILLED_SOURCE = "result_spilled"
RESULT_SPILLED_MARKER = f"[clio: {RESULT_SPILLED_SOURCE}]"


def _say(piece: str, variables: Mapping[str, object]) -> str:
    """A piece of the note (a ``result_spilled*`` reminder; required, so never off)."""
    from clio_agent import injections  # noqa: PLC0415

    return injections.render(piece, variables) or ""


def transcript_tool_result_chars() -> int:
    """Character bound on the TRANSCRIPT/evidence preview of one tool result.

    Config: ``limits.tool_result_chars`` / ``CLIO_TOOL_RESULT_CHARS`` (default
    12000). Consumed by :func:`clio_agent.gact.evidence._bounded_tool_call_result`;
    resolved here, beside its model-lane sibling, so a tool that must size its own
    result's transcript copy (the shell tool, #887) reads the same single source
    instead of re-declaring the key.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "limits.tool_result_chars",
        env="CLIO_TOOL_RESULT_CHARS",
        default=12_000,
        cast=conf.as_int,
    )


def bounded_model_tool_result(
    text: str,
    *,
    root: str | Path | None = None,
    session_id: str | None = None,
    room: ResultRoom | None = None,
) -> str:
    """The model-facing text of one tool result: whole, or its head plus a file for the rest.

    The harness does not decide what the agent needs from a big result. When it does not
    fit the result's room (``room``, else :func:`~clio_agent.tools.result_room.result_room`),
    the full result is written to the session's tool-output folder (the shell tool's spill
    folder, removed with the session) and the agent is told: how big it is, that the first
    characters follow, and where the rest is for it to explore (the reminder
    ``clio.reminder.result_spilled`` and its ``where`` pieces). The raw evidence itself is
    never rewritten.
    """

    room = room or result_room()
    if room.fits(text):
        return text
    path: Path | None = None
    unsaved = ""
    try:
        path = (
            _spill(text)
            if root is None and session_id is None
            else _spill(text, root=root, session_id=session_id)
        )
    except OSError as exc:
        logger.warning(
            "tool result spill failed reason=%s chars=%d error=%r",
            MODEL_TOOL_RESULT_SPILL_FAILED_REASON,
            len(text),
            exc,
        )
        trace.event(
            "TOOLS",
            "%s chars=%d error=%r",
            MODEL_TOOL_RESULT_SPILL_FAILED_REASON,
            len(text),
            exc,
        )
        unsaved = _say("result_spilled_unsaved", {"error": exc})

    def note(head: str, whole: bool) -> str:
        where = unsaved if path is None else _continuation(path, head, whole)
        frame = {"chars": f"{len(text):,}", "shown": f"{len(head):,}", "where": where}
        return f"{RESULT_SPILLED_MARKER} {_say(RESULT_SPILLED_SOURCE, frame)}"

    # Reserve the longest note (every count at its largest), then cut the head to fit.
    head = text[: max(0, room.chars - len(note(text, True)) - 2)]
    cut = head.rfind("\n")
    whole = cut > len(head) // 2
    if whole:
        head = head[:cut]  # end on a whole line when one is near
    projection = f"{note(head, whole)}\n\n{head}"
    from clio_agent import injections  # noqa: PLC0415

    # Details must include the head promised by the notice, exactly as returned
    # to the model. Recording only `note` silently loses that part of the view.
    injections.note(RESULT_SPILLED_SOURCE, projection)
    return projection


def _continuation(path: Path, head: str, whole: bool) -> str:
    """Where the rest is and how to read on: the exact next read, or a search for JSON."""
    if path.suffix == ".json":
        return _say("result_spilled_json", {"path": path})
    # The line after the head's last whole line (a last line cut short is read again).
    offset = head.count("\n") + (2 if whole else 1)
    return _say("result_spilled_read_on", {"path": path, "offset": offset})


def _json_pages(text: str) -> str | None:
    """``text`` as indented JSON (one value a line, so it can be paged and searched line
    by line), or ``None`` when it is not JSON."""
    if text.lstrip()[:1] not in ("{", "["):
        return None
    try:
        return json.dumps(json.loads(text), indent=1, ensure_ascii=False)
    except ValueError:
        return None


def _spill(text: str, *, root: str | Path | None = None, session_id: str | None = None) -> Path:
    """Write ``text`` to the session's tool-output folder; return the file.

    A JSON result is saved indented (``.json``): as one line, the rest of a long line
    could not be read on with the file tools' line limits. Anything else is saved as
    given (``.txt``).
    """
    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415
    from clio_agent.tools.servers.shell_spill_store import (  # noqa: PLC0415
        active_session_id,
        spill_directory,
    )

    root = root or get_active_tool_workspace_root() or str(Path.cwd())
    folder = spill_directory(root, session_id=session_id or active_session_id())
    folder.mkdir(parents=True, exist_ok=True)
    pages = _json_pages(text)
    path = folder / f"result-{uuid.uuid4().hex[:12]}{'.json' if pages is not None else '.txt'}"
    path.write_text(text if pages is None else pages, encoding="utf-8")
    return path
