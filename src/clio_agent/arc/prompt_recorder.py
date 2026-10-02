"""PromptRecorder: capture exactly what each LM call sends to the model.

The acceptance contract for the live context plane is *observable at the LM
boundary*: what an LM call is about to send is the only ground truth for "what
context reached the model". A ``dspy.BaseCallback`` ``on_lm_start`` hook sees the
call's inputs without subclassing or wrapping the LM, so it works against the real
production LM (live runs) and a scripted LM (unit tests) alike.

The agent loop calls ``lm(Request)``: the typed ``dspy.lm15.Request`` is the input,
and it is immutable, so it is kept as is. A DSPy module that still calls with
OpenAI-style ``messages`` (``dspy.Predict`` behind an adapter) is recorded too;
those lists are **deep-copied**, since a caller may mutate them after the call.
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from dspy.lm15 import Request
from dspy.utils.callback import BaseCallback

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapturedCall:
    """One snapshot of an outgoing LM call."""

    call_id: str
    model: str
    request: Request | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    def text(self) -> str:
        """Every text the call sends, joined -- for substring assertions over the wire."""
        parts: list[str] = []
        if self.request is not None:
            if self.request.system:
                parts.append(str(self.request.system))
            for message in self.request.messages:
                parts.extend(_part_texts(message.parts))
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):  # multimodal content blocks
                parts.extend(str(b.get("text", "")) for b in c if isinstance(b, dict))
        return "\n".join(parts)


def _part_texts(parts: Any) -> list[str]:
    texts: list[str] = []
    for part in parts:
        if getattr(part, "type", "") == "tool_result":
            texts.extend(_part_texts(part.content))
        elif isinstance(getattr(part, "text", None), str):
            texts.append(part.text)
    return texts


class PromptRecorder(BaseCallback):
    """Records what every LM call sends (thread-safe, snapshotting)."""

    def __init__(self) -> None:
        self._calls: list[CapturedCall] = []
        self._lock = threading.Lock()

    def on_lm_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
        prompt = inputs.get("prompt")
        msgs = inputs.get("messages")
        captured = CapturedCall(
            call_id=call_id,
            model=str(getattr(instance, "model", "") or ""),
            request=prompt if isinstance(prompt, Request) else None,
            messages=copy.deepcopy(msgs) if msgs is not None else [],
        )
        with self._lock:
            self._calls.append(captured)
        logger.debug(
            "prompt_recorder: captured call=%s model=%s typed=%s",
            call_id,
            captured.model,
            captured.request is not None,
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
