"""Shell output capture: spill-to-file instead of dropping (#1487, #887).

The shell tool used to keep the first ``max_output_bytes`` of each stream and
DISCARD the rest, so a wide ``describe()`` or a long column listing lost its
tail with no way to recover it short of re-running the command. Its default cap
(16 KiB) was also larger than the downstream tool-result bounds
(``limits.tool_result_chars`` / ``limits.model_tool_result_chars``, 12,000 chars
each), so an ordinary tabular stdout was re-wrapped by those bounders into a
``{preview, truncated}`` envelope whose ``preview`` was ``json.dumps(result)`` cut
mid-string: malformed JSON-in-JSON that broke CSV content-typing (#887).

This module owns the replacement:

* :class:`StreamCapture` reads one process stream with bounded memory (a head
  buffer and a tail ring, each ``inline_limit`` bytes) and, the moment the stream
  outgrows ``inline_limit``, writes the COMPLETE stream (raw bytes) to
  ``<workspace-state>/tool-output/<session>/<call-id>.<stream>.txt``
  (location + retention: :mod:`clio_agent.tools.servers.shell_spill_store`).
* :func:`compose_output_fields` sizes the result so its encoded JSON fits
  :func:`shell_result_char_budget` (the tighter of the two downstream bounds):
  neither bounder then rewraps it and ``stdout`` stays a real string. A stream
  that does not fit inline is returned as a line-aligned head excerpt in
  ``stdout`` plus a ``stdout_spill`` record (path, totals, tail excerpt).
* Every spill is a typed, logged event (:data:`SPILLED_REASON`); a spill that
  cannot be written returns :data:`SPILL_FAILED_REASON` with the excerpt — never
  a silent drop.

The spill directory sits under the workspace's Agent-managed state root
(:func:`clio_agent.paths.workspace_state_dir`). The model's file tools can read
or grep the returned path through the owning workspace's file policy.
"""

from __future__ import annotations

import codecs
import io
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from fastmcp import Context

from clio_agent.platform_paths import win_extended_path
from clio_agent.runtime import trace
from clio_agent.tools.mcp_result_projection import (
    model_tool_result_chars,
    transcript_tool_result_chars,
)

logger = logging.getLogger(__name__)

#: Typed reason stamped on (and logged for) every stream written to a spill file.
SPILLED_REASON = "shell_output_spilled"
#: Typed reason when the spill file could not be created or written.
SPILL_FAILED_REASON = "shell_output_spill_failed"
#: Typed reason when the command echo is dropped so the output can fit.
COMMAND_ECHO_OMITTED_REASON = "shell_command_echo_oversize"
_READ_CHUNK_BYTES = 4096


def shell_result_char_budget() -> int:
    """Encoded-character budget for one shell result.

    The tighter of the transcript bound (``limits.tool_result_chars``) and the
    model bound (``limits.model_tool_result_chars``), resolved per call exactly
    as those bounders resolve them. A result at or under this size passes both
    unchanged, which is what keeps ``stdout`` a real string (#887).
    """

    return min(transcript_tool_result_chars(), model_tool_result_chars())


def _decode(data: bytes) -> str:
    """Decode captured bytes the way the live stream is decoded (UTF-8, LF)."""

    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _skip_utf8_continuation(data: bytes | bytearray) -> bytes:
    """Drop leading UTF-8 continuation bytes left by cutting mid-character."""

    start = 0
    while start < len(data) and start < 3 and (data[start] & 0xC0) == 0x80:
        start += 1
    return bytes(data[start:])


class StreamCapture:
    """Bounded-memory capture of one process stream with lazy spill-to-file.

    Memory holds at most ``2 * inline_limit`` bytes: the first ``inline_limit``
    (the head) and the last ``inline_limit`` after it (the tail ring). Totals are
    exact. Once the stream outgrows ``inline_limit`` every byte, head included,
    is written to ``spill_path``; :meth:`spill` does the same after the fact for a
    stream that fit the per-stream cap but not the result budget.
    """

    def __init__(self, stream: str, *, inline_limit: int, spill_path: Path) -> None:
        """Create a capture for ``stream`` ("stdout"/"stderr")."""

        self.stream = stream
        self.inline_limit = inline_limit
        self.spill_path = spill_path
        self.total_bytes = 0
        self.spill_status: str | None = None
        self.spill_error: str | None = None
        self._head = bytearray()
        self._tail = bytearray()
        self._tail_prev_newline = True
        self._newlines = 0
        self._ends_with_newline = True
        self._file: IO[bytes] | None = None

    # -- feeding ----------------------------------------------------------- #

    def feed(self, chunk: bytes) -> bytes:
        """Account for ``chunk``; return the part inside the inline window.

        The returned bytes are what the live terminal display should show; the
        rest is still counted, kept in the tail ring, and written to the spill
        file.
        """

        if not chunk:
            return b""
        self.total_bytes += len(chunk)
        self._newlines += chunk.count(b"\n")
        self._ends_with_newline = chunk.endswith(b"\n")
        room = max(0, self.inline_limit - len(self._head))
        shown = chunk[:room]
        overflow = chunk[room:]
        self._head.extend(shown)
        if overflow:
            if not self._tail:
                self._tail_prev_newline = self._head.endswith(b"\n") if self._head else True
            self._tail.extend(overflow)
            excess = len(self._tail) - self.inline_limit
            if excess > 0:
                self._tail_prev_newline = self._tail[excess - 1 : excess] == b"\n"
                del self._tail[:excess]
            if self.spill_status is None:
                self._open_spill()
            self._write(overflow)
        return shown

    def finish(self) -> None:
        """Close the spill file (if any) and log the typed spill event."""

        if self._file is not None:
            try:
                self._file.close()
            except OSError as exc:
                self._fail(exc)
            self._file = None
        if self.spill_status == "spilled":
            self._log_spilled()

    def spill(self) -> None:
        """Write a stream that stayed inline to its spill file (post-hoc).

        Used when the per-stream cap admitted the stream but the whole result
        would breach :func:`shell_result_char_budget`. The head buffer holds the
        entire stream in that case, so the file is still complete.
        """

        if self.spill_status is not None:
            return
        self._open_spill()
        self.finish()

    # -- spill file -------------------------------------------------------- #

    def _open_spill(self) -> None:
        try:
            self.spill_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(win_extended_path(self.spill_path), "wb")  # noqa: SIM115
            self._file.write(self._head)
        except OSError as exc:
            self._fail(exc)
            return
        self.spill_status = "spilled"

    def _write(self, data: bytes) -> None:
        if self._file is None:
            return
        try:
            self._file.write(data)
        except OSError as exc:
            self._fail(exc)

    def _fail(self, exc: OSError) -> None:
        """Record a typed spill failure; remove a partial file so none misleads."""

        if self._file is not None:
            try:
                self._file.close()
            except OSError as close_exc:
                logger.warning(
                    "shell spill close failed reason=%s stream=%s error=%r",
                    SPILL_FAILED_REASON,
                    self.stream,
                    close_exc,
                )
            self._file = None
        try:
            Path(win_extended_path(self.spill_path)).unlink(missing_ok=True)
        except OSError as unlink_exc:
            logger.warning(
                "shell spill partial-file cleanup failed reason=%s path=%s error=%r",
                SPILL_FAILED_REASON,
                self.spill_path,
                unlink_exc,
            )
        self.spill_status = "spill_failed"
        self.spill_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "shell output spill failed reason=%s stream=%s path=%s error=%s",
            SPILL_FAILED_REASON,
            self.stream,
            self.spill_path,
            self.spill_error,
        )
        trace.event(
            "TOOLS",
            "shell output spill failed reason=%s stream=%s path=%s error=%s",
            SPILL_FAILED_REASON,
            self.stream,
            self.spill_path,
            self.spill_error,
        )

    def _log_spilled(self) -> None:
        logger.info(
            "shell output spilled reason=%s stream=%s path=%s total_bytes=%d total_lines=%d",
            SPILLED_REASON,
            self.stream,
            self.spill_path,
            self.total_bytes,
            self.total_lines,
        )
        trace.event(
            "TOOLS",
            "shell output spilled reason=%s stream=%s path=%s total_bytes=%d total_lines=%d",
            SPILLED_REASON,
            self.stream,
            self.spill_path,
            self.total_bytes,
            self.total_lines,
        )

    # -- views ------------------------------------------------------------- #

    @property
    def total_lines(self) -> int:
        """Exact line count (a final unterminated line counts)."""

        if self.total_bytes == 0:
            return 0
        return self._newlines + (0 if self._ends_with_newline else 1)

    @property
    def overflowed(self) -> bool:
        """True when the stream outgrew ``inline_limit`` (the head is partial)."""

        return self.total_bytes > self.inline_limit

    @property
    def buffered_bytes(self) -> int:
        """Bytes held in memory (bounded by ``2 * inline_limit``)."""

        return len(self._head) + len(self._tail)

    def full_text(self) -> str:
        """The whole stream as text; only valid when not :attr:`overflowed`."""

        return _decode(bytes(self._head))

    def head_excerpt(self, max_chars: int) -> str:
        """Leading excerpt of at most ``max_chars``, cut on a line boundary."""

        text = _decode(bytes(self._head))
        if len(text) <= max_chars and not self.overflowed:
            return text
        cut = text[:max_chars]
        newline = cut.rfind("\n")
        return cut[: newline + 1] if newline >= 0 else cut

    def tail_excerpt(self, max_chars: int, *, head: str = "") -> str:
        """Trailing excerpt of at most ``max_chars``, starting on a line boundary.

        For a stream that overflowed, the excerpt comes from the tail ring. For a
        stream spilled post-hoc (it fit the per-stream cap, so the head buffer
        holds all of it) it comes from the text after ``head`` — never
        overlapping the head excerpt already returned.
        """

        if max_chars <= 0:
            return ""
        if self._tail:
            trimmed = _skip_utf8_continuation(self._tail)
            text = _decode(trimmed)
            starts_line = self._tail_prev_newline and len(trimmed) == len(self._tail)
        else:
            text = self.full_text()[len(head) :]
            starts_line = not head or head.endswith("\n")
        if len(text) > max_chars:
            starts_line = text[-max_chars - 1] == "\n"
            text = text[-max_chars:]
        if not starts_line:
            newline = text.find("\n")
            if 0 <= newline < len(text) - 1:
                text = text[newline + 1 :]
        return text


async def read_process_stream(
    reader: Any,
    *,
    capture: StreamCapture,
    ctx: Context,
) -> None:
    """Drain one process stream into ``capture``, forwarding live terminal chunks.

    Bytes inside the inline window are forwarded as typed ``clio.terminal.chunk``
    progress messages for the live display; bytes past it are still captured in
    full by ``capture`` (and its spill file), just not streamed to the display.
    """

    decoder = io.IncrementalNewlineDecoder(
        codecs.getincrementaldecoder("utf-8")(errors="replace"),
        translate=True,
    )
    try:
        while chunk := await reader.read(_READ_CHUNK_BYTES):
            shown = capture.feed(chunk)
            if shown:
                await _forward(ctx, capture, decoder.decode(shown, final=False))
        await _forward(ctx, capture, decoder.decode(b"", final=True))
    finally:
        capture.finish()


async def _forward(ctx: Context, capture: StreamCapture, text: str) -> None:
    """Send one live terminal chunk; display delivery can never fail the command."""

    if not text:
        return
    message = json.dumps(
        {"type": "clio.terminal.chunk", "stream": capture.stream, "text": text},
        ensure_ascii=False,
    )
    try:
        await ctx.report_progress(progress=capture.total_bytes, total=None, message=message)
    except Exception as exc:  # noqa: BLE001 - display progress cannot fail the command
        logger.warning(
            "shell progress delivery failed stream=%s reason=progress_delivery_failed error=%r",
            capture.stream,
            exc,
        )


def _spill_record(capture: StreamCapture, root: Path, excerpt_chars: int) -> dict[str, Any]:
    head_chars = (excerpt_chars * 3) // 4
    head = capture.head_excerpt(head_chars)
    tail = capture.tail_excerpt(excerpt_chars - head_chars, head=head)
    record: dict[str, Any] = {
        "status": capture.spill_status,
        "reason": SPILLED_REASON if capture.spill_status == "spilled" else SPILL_FAILED_REASON,
    }
    if capture.spill_status == "spilled":
        path = capture.spill_path.resolve()
        record["path"] = str(path)
        try:
            record["relative_path"] = path.relative_to(root.resolve()).as_posix()
        except ValueError:
            record["relative_path"] = str(path)
    else:
        record["error"] = capture.spill_error
    record.update(
        total_bytes=capture.total_bytes,
        total_lines=capture.total_lines,
        head_lines=len(head.splitlines()),
        tail=tail,
        tail_lines=len(tail.splitlines()),
    )
    return {"head": head, "record": record}


def _assemble(
    base: Mapping[str, Any],
    captures: list[StreamCapture],
    excerpted: set[str],
    excerpt_chars: int,
    root: Path,
) -> dict[str, Any]:
    result = dict(base)
    for capture in captures:
        name = capture.stream
        if name in excerpted:
            spilled = _spill_record(capture, root, excerpt_chars)
            result[name] = spilled["head"]
            result[f"{name}_truncated"] = True
            result[f"{name}_spill"] = spilled["record"]
        else:
            result[name] = capture.full_text()
            result[f"{name}_truncated"] = False
    return result


def _fits(result: Mapping[str, Any], budget: int) -> bool:
    # ensure_ascii=True (json.dumps' default, what the model lane encodes with)
    # is never shorter than the transcript lane's ensure_ascii=False encoding.
    return len(json.dumps(result)) <= budget


def _largest_fitting(
    base: Mapping[str, Any],
    captures: list[StreamCapture],
    excerpted: set[str],
    root: Path,
    budget: int,
) -> dict[str, Any] | None:
    """Binary-search the largest excerpt size whose result fits ``budget``."""

    ceiling = max(capture.inline_limit for capture in captures)
    best = _assemble(base, captures, excerpted, 0, root)
    if not _fits(best, budget):
        return None
    low, high = 1, ceiling
    while low <= high:
        size = (low + high) // 2
        candidate = _assemble(base, captures, excerpted, size, root)
        if _fits(candidate, budget):
            best, low = candidate, size + 1
        else:
            high = size - 1
    return best


def compose_output_fields(
    base: Mapping[str, Any],
    captures: list[StreamCapture],
    *,
    root: Path,
    budget: int,
) -> dict[str, Any]:
    """Build the shell result so its encoded JSON fits ``budget``.

    Streams that fit inline are returned verbatim. A stream that overflowed its
    per-stream cap is always excerpted (its spill file already holds all of it).
    If the result still exceeds ``budget``, the largest remaining inline stream is
    spilled post-hoc and excerpted, and so on. Only if even empty excerpts cannot
    fit is the (redundant — the call's arguments carry it) command echo dropped,
    with a typed ``command_omitted`` marker.

    Args:
        base: The non-output result fields (command, cwd, exit code, ...).
        captures: The finished stdout/stderr captures.
        root: The workspace root the spill paths are made relative to.
        budget: Encoded-character budget (:func:`shell_result_char_budget`).

    Returns:
        The complete result dict.
    """

    excerpted = {capture.stream for capture in captures if capture.overflowed}
    if not excerpted:
        inline = _assemble(base, captures, excerpted, 0, root)
        if _fits(inline, budget):
            return inline
    pending = sorted(
        (c for c in captures if c.stream not in excerpted and c.total_bytes),
        key=lambda capture: capture.total_bytes,
        reverse=True,
    )
    while True:
        if excerpted:
            fitted = _largest_fitting(base, captures, excerpted, root, budget)
            if fitted is not None:
                return fitted
        if not pending:
            break
        capture = pending.pop(0)
        capture.spill()
        excerpted.add(capture.stream)
    command = str(base.get("command") or "")
    reduced = {key: value for key, value in base.items() if key != "command"}
    reduced["command_omitted"] = {"reason": COMMAND_ECHO_OMITTED_REASON, "chars": len(command)}
    logger.info(
        "shell result command echo omitted reason=%s chars=%d",
        COMMAND_ECHO_OMITTED_REASON,
        len(command),
    )
    fitted = _largest_fitting(reduced, captures, excerpted, root, budget)
    return fitted if fitted is not None else _assemble(reduced, captures, excerpted, 0, root)


__all__ = [
    "COMMAND_ECHO_OMITTED_REASON",
    "SPILLED_REASON",
    "SPILL_FAILED_REASON",
    "StreamCapture",
    "compose_output_fields",
    "read_process_stream",
    "shell_result_char_budget",
]
