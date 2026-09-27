"""Transcript blocks for spilled shell output (#1487).

When the shell tool spills a stream (:mod:`clio_agent.tools.servers.shell_output`),
the result's ``stdout``/``stderr`` hold only a head excerpt and a
``<stream>_spill`` record says where the rest went. The human needs the same
facts the model gets, in plain words: that the full output was saved, how big it
is, how it ends, and a way to open it.

Each spilled stream becomes ONE ``link`` block, placed right after the terminal
block so clients render it inside the shell card:

* ``label`` — "Full output saved (20,000 lines, 391 KB)" (or "Full error output
  ..."; "could not be saved" when the spill failed),
* ``text`` — the tail excerpt, trimmed to one transport page on a line boundary,
* ``detail`` — the caption for that tail ("Last 12 lines"),
* ``target="file"`` + ``uri`` — the saved file, with ``action_label`` "Open full
  output"; both empty when nothing was saved.

The server writes every word; clients render verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Keep in step with ``clio_agent.gact.tool_result_presentation.PAGE_CHARS``: a
#: block body above one page is paged from its HEAD, but a tail must show its end.
_TAIL_PAGE_CHARS = 2048
_STREAM_NOUN = {"stdout": "output", "stderr": "error output"}


def format_size(total_bytes: int) -> str:
    """Human size for a label: whole KB under 1 MiB, one-decimal MB above."""

    if total_bytes < 1024 * 1024:
        return f"{max(1, round(total_bytes / 1024))} KB"
    return f"{total_bytes / (1024 * 1024):.1f} MB"


def _page_tail(tail: str) -> str:
    """Trim ``tail`` to at most one page, keeping whole trailing lines."""

    if len(tail) <= _TAIL_PAGE_CHARS:
        return tail
    cut = tail[-_TAIL_PAGE_CHARS:]
    newline = cut.find("\n")
    return cut[newline + 1 :] if 0 <= newline < len(cut) - 1 else cut


def _caption(tail: str) -> str:
    count = len(tail.splitlines())
    if count == 0:
        return ""
    return "Last line" if count == 1 else f"Last {count:,} lines"


def saved_output_blocks(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one ``link`` block per spilled stream in a shell result ``row``."""

    blocks: list[dict[str, Any]] = []
    for stream, noun in _STREAM_NOUN.items():
        spill = row.get(f"{stream}_spill")
        if not isinstance(spill, Mapping):
            continue
        lines = int(spill.get("total_lines") or 0)
        size = format_size(int(spill.get("total_bytes") or 0))
        saved = spill.get("status") == "spilled" and bool(spill.get("path"))
        verb = "saved" if saved else "could not be saved"
        tail = _page_tail(str(spill.get("tail") or ""))
        blocks.append(
            {
                "id": f"saved-output-{stream}",
                "type": "link",
                "target": "file",
                "uri": str(spill.get("path") or "") if saved else "",
                "label": f"Full {noun} {verb} ({lines:,} lines, {size})",
                "action_label": "Open full output" if saved else "",
                "text": tail,
                "detail": _caption(tail),
            }
        )
    return blocks


__all__ = ["format_size", "saved_output_blocks"]
