"""Recover explicit Codex file-preparation refusals without claiming a file was read."""

from __future__ import annotations

import dataclasses
import hashlib
import threading
from collections import OrderedDict
from typing import Any

from dspy.lm15 import DocumentPart, Request, TextPart, ToolResultPart

FILE_PREPARATION_REFUSAL = (
    "this input file format requires preparation that is not supported by rustponsesapi"
)
#: The injection source of the note that stands in for a refused document.
MEDIA_UNAVAILABLE_SOURCE = "media_unavailable"


def file_recovery_note() -> str:
    """The note that replaces a refused document: the reminder
    ``clio.reminder.media_unavailable`` (required, so never switched off)."""
    from clio_agent.gact import reminders  # noqa: PLC0415

    return reminders.render(MEDIA_UNAVAILABLE_SOURCE, {}) or ""


def __getattr__(name: str) -> str:
    """``FILE_RECOVERY_NOTE``: the note as this session words it (:func:`file_recovery_note`)."""
    if name == "FILE_RECOVERY_NOTE":
        return file_recovery_note()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class FilePreparationUnsupportedError(Exception):
    """Internal signal for a file refusal received before any model output."""


def _fingerprint(part: DocumentPart) -> str:
    for kind in ("data", "url", "file_id", "path"):
        value = getattr(part, kind)
        if value is not None:
            content = value if isinstance(value, bytes) else str(value).encode()
            return hashlib.sha256(
                part.media_type.encode() + b"\0" + kind.encode() + b"\0" + content
            ).hexdigest()
    raise ValueError("document has no input source")


def _documents(request: Request) -> list[DocumentPart]:
    return [
        item
        for message in request.messages
        for part in message.parts
        for item in (part, *getattr(part, "content", ()))
        if isinstance(item, DocumentPart)
    ]


def is_file_preparation_refusal(request: Request, message: str) -> bool:
    """Recognize the specific backend refusal only when a document was sent."""
    return FILE_PREPARATION_REFUSAL in message.casefold() and bool(_documents(request))


class DocumentInputRecovery:
    """Keep bounded, content-specific refusals for one model's engine.

    Recorded context and source files remain intact. Only ephemeral provider input
    changes, with an explicit note in place of each rejected document. Other files,
    images, tool results and models keep their normal delivery path.
    """

    def __init__(self, capacity: int = 256) -> None:
        """Create a bounded refusal cache; never retain attachment bytes here."""
        if capacity < 1:
            raise ValueError("document refusal capacity must be positive")
        self._capacity = capacity
        self._rejected: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def reject(self, request: Request) -> None:
        """Remember documents in the refused request, without generalizing to others."""
        keys = [_fingerprint(part) for part in _documents(request)]
        with self._lock:
            for key in keys:
                self._rejected[key] = None
                self._rejected.move_to_end(key)
            while len(self._rejected) > self._capacity:
                self._rejected.popitem(last=False)

    def prepare(self, request: Request) -> Request:
        """Replace known-refused documents with honest, actionable observations."""
        with self._lock:
            rejected = frozenset(self._rejected)
        if not rejected:
            return request
        note = file_recovery_note()

        def replace_document(part: Any) -> Any:
            if isinstance(part, DocumentPart) and _fingerprint(part) in rejected:
                return TextPart(text=note)
            if isinstance(part, ToolResultPart):
                content = tuple(replace_document(item) for item in part.content)
                return dataclasses.replace(part, content=content)
            return part

        messages = tuple(
            dataclasses.replace(message, parts=tuple(replace_document(p) for p in message.parts))
            for message in request.messages
        )
        return dataclasses.replace(request, messages=messages)


def report_file_recovery() -> None:
    """Expose the same recovery note in the live transcript when a session exists."""
    from clio_agent.gact.injection_parts import emit_injection  # noqa: PLC0415

    emit_injection(MEDIA_UNAVAILABLE_SOURCE, file_recovery_note())
