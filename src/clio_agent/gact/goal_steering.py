"""Recorded human delivery boundaries used by goal evaluation."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any


def pending_steer(app: Any, session_id: str) -> bool:
    """Return whether an accepted human message still needs model delivery."""
    intents = getattr(app.state, "message_intents", None)
    return bool(intents is not None and intents.list_pending(session_id))


def _part_groups(message: Any) -> list[tuple[str, list[Any]]]:
    """Group call/result pairs at their canonical public block position."""
    groups: list[tuple[str, list[Any]]] = []
    tools: dict[str, list[Any]] = {}
    for part in getattr(message, "parts", []) or []:
        part_id = str(getattr(part, "id", "") or "")
        if getattr(part, "type", "") in {"tool_call", "tool_result"}:
            call_id = str(getattr(part, "call_id", "") or part_id)
            if call_id and call_id in tools:
                tools[call_id].append(part)
                continue
            if call_id:
                tools[call_id] = group = [part]
                groups.append((part_id, group))
                continue
        groups.append((part_id, [part]))
    return groups


def ordered_evidence(messages: Sequence[Any]) -> Iterator[tuple[Any, list[Any]]]:
    """Interleave consumed steers using receipts, retaining unmatched messages.

    A message creation timestamp cannot order parts generated on both sides of
    a human steer. Only the durable delivery receipt supplies that boundary.
    """
    owners = {
        getattr(message, "id", ""): message
        for message in messages
        if getattr(message, "role", "") == "assistant" and getattr(message, "id", "")
    }
    groups = {key: _part_groups(message) for key, message in owners.items()}
    placements: dict[str, dict[str, list[Any]]] = {}
    placed: set[str] = set()
    for message in messages:
        metadata = getattr(message, "metadata", None)
        if getattr(message, "role", "") != "user" or not isinstance(metadata, Mapping):
            continue
        receipt = metadata.get("steer_delivery")
        if metadata.get("mid_turn_steer") is not True or not isinstance(receipt, Mapping):
            continue
        owner_id, anchor = receipt.get("assistant_message_id"), receipt.get("after_part_id")
        message_id = getattr(message, "id", "")
        if not isinstance(owner_id, str) or not isinstance(anchor, str) or not message_id:
            continue
        owner = owners.get(owner_id)
        if owner is None or getattr(owner, "session_id", "") != getattr(message, "session_id", ""):
            continue
        if anchor and not any(key == anchor for key, _ in groups[owner_id]):
            continue
        placements.setdefault(owner_id, {}).setdefault(anchor, []).append(message)
        placed.add(message_id)

    for message in messages:
        message_id = getattr(message, "id", "")
        if message_id in placed:
            continue
        boundaries = placements.get(message_id)
        if boundaries is None:
            yield message, list(getattr(message, "parts", []) or [])
            continue
        for steer in boundaries.get("", []):
            yield steer, list(getattr(steer, "parts", []) or [])
        pending: list[Any] = []
        for anchor, parts in groups[message_id]:
            pending.extend(parts)
            if anchor and anchor in boundaries:
                yield message, pending
                pending = []
                for steer in boundaries[anchor]:
                    yield steer, list(getattr(steer, "parts", []) or [])
        if pending:
            yield message, pending
