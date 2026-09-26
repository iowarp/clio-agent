"""Make the process's console streams able to carry every character clio emits.

clio writes non-ASCII to the console on ordinary paths: every ``trace`` line is
prefixed ``⚑``, the local-store banner and many log messages carry em dashes,
arrows and section signs. On Windows, when stdout/stderr are redirected (the
desktop sidecar, a launcher log, a pipe), Python opens them in the locale code
page -- typically ``cp1252`` -- and stdout with ``errors="strict"``, so the first
``print("⚑ ...")`` raises ``UnicodeEncodeError``. That took down ARC boot on a
cp1252 console (``arc_boot_failed ... UnicodeEncodeError``).

The fix is at the stream, not at each message: every process entry point calls
:func:`ensure_utf8_console` before doing anything else, which re-opens
``sys.stdout`` / ``sys.stderr`` as UTF-8 with ``errors="replace"``. Every
``print`` and every ``logging.StreamHandler`` (which writes to ``sys.stderr``,
reconfigured in place) then encodes safely, whatever the message contains.
UTF-8 is also what the desktop sidecar decodes; a code-page byte such as
cp1252's ``0x97`` em dash is invalid UTF-8 on that side.
"""

from __future__ import annotations

import codecs
import io
import logging
import sys
from typing import TextIO

logger = logging.getLogger(__name__)

CONSOLE_ENCODING = "utf-8"
CONSOLE_ERRORS = "replace"
_STREAM_NAMES = ("stdout", "stderr")


def _is_utf8(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or ""
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


def _configure_stream(name: str) -> None:
    stream = getattr(sys, name, None)
    if stream is None:
        # pythonw / a detached service: there is no console to write to.
        return
    if _is_utf8(stream) and getattr(stream, "errors", None) in {CONSOLE_ERRORS, "backslashreplace"}:
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        if not _is_utf8(stream):
            logger.warning(
                "console stream not reconfigurable reason=console_encoding_unchanged "
                "stream=%s encoding=%s type=%s",
                name,
                getattr(stream, "encoding", None),
                type(stream).__name__,
            )
        return
    try:
        reconfigure(encoding=CONSOLE_ENCODING, errors=CONSOLE_ERRORS)
    except (ValueError, io.UnsupportedOperation) as exc:
        logger.warning(
            "console stream reconfigure failed reason=console_encoding_unchanged "
            "stream=%s encoding=%s error=%r",
            name,
            getattr(stream, "encoding", None),
            exc,
        )


def ensure_utf8_console() -> None:
    """Re-open ``sys.stdout`` and ``sys.stderr`` as UTF-8 with ``errors="replace"``.

    Idempotent and cheap; call it first thing in every process entry point. A
    stream that cannot be reconfigured is left as-is with a typed
    ``console_encoding_unchanged`` warning, never silently.
    """

    for name in _STREAM_NAMES:
        _configure_stream(name)


__all__ = ["CONSOLE_ENCODING", "CONSOLE_ERRORS", "ensure_utf8_console"]
