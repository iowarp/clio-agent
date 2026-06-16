"""PromptRecorder: capture the exact messages dspy sends to the LM.

The acceptance contract for the live context plane is *observable at the LM
boundary*: the message list dspy is about to send is the only ground truth for
"what context reached the model". A ``dspy.BaseCallback`` ``on_lm_start`` hook
sees ``inputs["messages"]`` — the literal ``list[dict]`` about to go on the wire —
without subclassing or wrapping the LM, so it works against the real production
LM (live ALCF runs) and a scripted ``DummyLM`` (unit tests) alike.

CRITICAL: the live plane *mutates* the message list between iterations, so the
callback **deep-copies** the captured messages. Without a snapshot every recorded
call would alias the final state and the byte-equality / prefix tests would be
meaningless.
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from dspy.utils.callback import BaseCallback

logger = logging.getLogger(__name__)

_FIELD_MARKER = "[[ ## "  # dspy ChatAdapter field header: "[[ ## name ## ]]"


@dataclass(frozen=True)
class CapturedCall:
    """One snapshot of an outgoing LM call."""

    call_id: str
    model: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    prompt: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        """All message contents joined — for substring assertions over the wire."""
        parts: list[str] = []
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):  # multimodal content blocks
                parts.extend(str(b.get("text", "")) for b in c if isinstance(b, dict))
        if self.prompt:
            parts.append(self.prompt)
        return "\n".join(parts)

    def field_value(self, name: str) -> str | None:
        """Extract a ChatAdapter-rendered field's content from this call's messages.

        dspy renders fields as ``[[ ## name ## ]]\\n<value>`` until the next field
        marker. Used to isolate the ``trajectory`` span for byte-equality without
        the static system/signature framing defeating exact comparison. Returns the
        field value (stripped) or ``None`` if absent.

        Scans messages in REVERSE so the actual rendered *values* (in the last user
        message) win over the field *template* in the system message
        (``[[ ## name ## ]]\\n{name}``).

        NOTE: only reliable for *leaf* fields. The react ``trajectory`` field's value
        is itself a ChatAdapter-formatted string containing nested ``[[ ## ... ## ]]``
        markers, so this truncates it. Use ``text()`` substring checks for
        mutation-propagation and compare ``_format_trajectory`` outputs directly for
        byte-equality of the trajectory.
        """
        header = f"{_FIELD_MARKER}{name} ## ]]"
        for m in reversed(self.messages):
            content = m.get("content")
            if not isinstance(content, str) or header not in content:
                continue
            after = content.rsplit(header, 1)[1]
            after = after.lstrip("\n")
            nxt = after.find(_FIELD_MARKER)
            return (after if nxt < 0 else after[:nxt]).rstrip("\n")
        return None


class PromptRecorder(BaseCallback):
    """Records the exact ``messages`` of every LM call (thread-safe, snapshotting)."""

    def __init__(self) -> None:
        self._calls: list[CapturedCall] = []
        self._lock = threading.Lock()

    def on_lm_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
        msgs = inputs.get("messages")
        captured = CapturedCall(
            call_id=call_id,
            model=str(getattr(instance, "model", "") or ""),
            # Deep-copy: the live plane mutates the list across iterations.
            messages=copy.deepcopy(msgs) if msgs is not None else [],
            prompt=inputs.get("prompt"),
            kwargs={
                k: v
                for k, v in inputs.items()
                if k not in ("messages", "prompt") and not k.startswith("api_")
            },
        )
        with self._lock:
            self._calls.append(captured)
        logger.debug(
            "prompt_recorder: captured call=%s model=%s messages=%d",
            call_id, captured.model, len(captured.messages),
        )

    # ---- accessors -----------------------------------------------------

    def calls(self) -> list[CapturedCall]:
        """A snapshot copy of all captured calls, in send order."""
        with self._lock:
            return list(self._calls)

    def last(self) -> CapturedCall | None:
        """The most recently captured call, or ``None``."""
        with self._lock:
            return self._calls[-1] if self._calls else None

    def reset(self) -> None:
        """Drop all captured calls."""
        with self._lock:
            self._calls.clear()
