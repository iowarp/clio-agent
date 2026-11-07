"""An ``lm.call`` record stores only the messages its chain has not recorded yet.

Each model request re-sends the whole conversation, and its ``lm.call`` record used to
store all of it: on H4 (2026-11-03) the six records of one turn were 23, 55, 149, 201,
237 and 300 KB, and a turn near a 1M-token window's compaction threshold would store
about 3 MB per step. A chain is one session's requests with the same first message
(the agent's system prompt; a summarizer or judge call in the same session has its
own). :func:`delta` keeps, per chain, the digests of the messages it recorded last:

* a request that extends them is recorded as its new messages plus
  ``messages_prefix`` ``{"chain", "count", "digest"}`` (how many leading messages the
  previous record of the chain already holds, and a digest of them);
* any other request (the first of a chain, or after a compaction rewrote the head)
  is recorded whole, with no ``messages_prefix``.

:func:`rebuild` restores the full list for records read in order. A record whose
prefix does not match what the reader rebuilt (a missing or reordered record) gets
``None``: the prompt is reported unavailable, never wrong. ``attention_messages`` (the
exact wire copy recorded when attention is declared) is not touched.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable
from typing import Any

__all__ = ["delta", "rebuild"]

#: Chains remembered per process (the oldest is forgotten; its next record is whole).
_MAX_CHAINS = 1024


def _digest(message: Any) -> str:
    return hashlib.sha256(
        json.dumps(message, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _prefix_digest(digests: Iterable[str]) -> str:
    return hashlib.sha256("".join(digests).encode("ascii")).hexdigest()


class _Chains:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str], list[str]] = {}

    def delta(self, session_id: str, messages: list[Any]) -> tuple[list[Any], dict[str, Any]]:
        digests = [_digest(message) for message in messages]
        chain = digests[0] if digests else ""
        key = (session_id, chain)
        with self._lock:
            previous = self._last.pop(key, None)
            self._last[key] = digests
            while len(self._last) > _MAX_CHAINS:
                self._last.pop(next(iter(self._last)))
        if not previous or len(previous) > len(digests) or digests[: len(previous)] != previous:
            return messages, {}
        count = len(previous)
        prefix = {"chain": chain, "count": count, "digest": _prefix_digest(previous)}
        return messages[count:], prefix


_CHAINS = _Chains()


def delta(session_id: str, messages: Any) -> tuple[Any, dict[str, Any]]:
    """``(messages to store, messages_prefix)`` for one request of ``session_id``.

    ``messages_prefix`` is ``{}`` when the request is stored whole.
    """
    if not session_id or not isinstance(messages, list) or not messages:
        return messages, {}
    return _CHAINS.delta(session_id, messages)


def rebuild(payloads: Iterable[dict[str, Any]]) -> list[list[Any] | None]:
    """The full message list of each ``lm.call`` payload of one session, in order.

    ``None`` for a payload whose prefix the earlier payloads do not supply.
    """
    chains: dict[str, tuple[list[Any], list[str]]] = {}
    out: list[list[Any] | None] = []
    for payload in payloads:
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            out.append(None)
            continue
        prefix = payload.get("messages_prefix") if isinstance(payload, dict) else None
        if isinstance(prefix, dict) and prefix:
            chain = str(prefix.get("chain") or "")
            count = int(prefix.get("count") or 0)
            known = chains.get(chain)
            if (
                known is None
                or len(known[0]) < count
                or _prefix_digest(known[1][:count]) != prefix.get("digest")
            ):
                out.append(None)
                continue
            full = [*known[0][:count], *messages]
            digests = [*known[1][:count], *(_digest(message) for message in messages)]
        else:
            full = list(messages)
            digests = [_digest(message) for message in messages]
            chain = digests[0] if digests else ""
        chains[chain] = (full, digests)
        out.append(full)
    return out
