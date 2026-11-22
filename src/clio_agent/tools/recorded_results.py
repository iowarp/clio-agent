"""What CLIO records of a tool result: the whole result, in clio-core.

Every record CLIO writes of a result goes through this module: the tool observer's copy
(the trace's ``tool.call.completed`` event and the transcript row) and the task ledger's
inline value. Each keeps the result whole, whatever its size: all data that goes to the
agent is stored in clio-core. A full store grows instead of refusing it
(:func:`clio_agent.arc.clio_core_growth.recover_capacity`).

What the model sees stays bounded where it is produced
(:func:`clio_agent.tools.mcp_result_projection.bounded_model_tool_result`), as a preview
of a value clio-core holds whole.

The one exception is a view's call to a tool the model cannot see
(:mod:`clio_agent.tools.view_only_results`): that content goes to the view, never to the
agent, and its record keeps only a size and hash.

A shell stream past its inline room reaches the model as its head and a file; the call's
clio-core event reads the file back whole (:func:`with_whole_streams`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.tools import view_only_results

logger = logging.getLogger(__name__)


def observed(result: Any) -> Any:
    """The tool observer's copy of a result (trace, transcript row): the whole result."""

    return view_only_results.observed_result(result)


def ledger_reply(reply: Any) -> Any:
    """The task ledger's copy of an MCP reply (the call's inline value): the whole reply."""

    return view_only_results.stored_reply(reply)


def with_whole_streams(result: Any) -> Any:
    """``result`` with each spilled shell stream read back whole, for clio-core's copy.

    The shell returns a stream past its inline room as its head plus ``<stream>_spill``
    {status ``spilled``, path} (:mod:`clio_agent.tools.servers.shell_output`); the model
    keeps that head and the file. The call's clio-core event gets the whole stream: this
    returns a copy whose ``<stream>`` is the file's text and ``<stream>_truncated`` false.
    Works on a native result and on an MCP result's ``structuredContent``. A stream whose
    file cannot be read is left as it is, its spill record saying where it was.
    """

    if not isinstance(result, Mapping):
        return result
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        whole = _streams_read_back(structured)
        return result if whole is structured else {**result, "structuredContent": whole}
    return _streams_read_back(result)


def _streams_read_back(value: Mapping[str, Any]) -> Mapping[str, Any]:
    whole: dict[str, Any] | None = None
    for key, record in value.items():
        if not (isinstance(key, str) and key.endswith(_SPILL) and isinstance(record, Mapping)):
            continue
        stream = key[: -len(_SPILL)]
        path = record.get("path")
        if record.get("status") != "spilled" or not isinstance(path, str) or stream not in value:
            continue
        try:
            text = Path(path).read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            logger.warning(
                "tool result stream not read back reason=%s stream=%s path=%s error=%r",
                STREAM_NOT_READ_BACK,
                stream,
                path,
                exc,
            )
            continue
        whole = dict(value) if whole is None else whole
        whole[stream] = text
        whole[f"{stream}_truncated"] = False
    return value if whole is None else whole


_SPILL = "_spill"
#: Logged when a spilled stream's file could not be read into clio-core's copy.
STREAM_NOT_READ_BACK = "tool_result_stream_not_read_back"


__all__ = ["STREAM_NOT_READ_BACK", "ledger_reply", "observed", "with_whole_streams"]
