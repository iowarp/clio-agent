"""Where a composed CLIO addition's pieces sit in its text, carried to the context view.

The session facts reach the model as one user message, but one of their pieces, the
list of plugins and connectors the agent can offer (``clio.reminder.session_facts_plugins``),
has its own category in the context view: "Plugin list" (OL-D8, OL-D12). The piece is
identified structurally, where the facts are composed:

1. :func:`join` composes the facts from their rendered pieces and returns a
   :class:`Composed` text that carries each attributed piece's span and items
   (one per listed row, sized on the template by :func:`clio_agent.injections.item_chars`).
2. The step recorder stores those spans with the recorded ``user`` segment
   (``content["pieces"]``).
3. When the segment is folded back into a message, :func:`note` keeps the spans for that
   exact message text (the codec's ``[clio: <source>]`` heading shifts them), so
   :func:`clio_agent.gact.prompt_breakdown.request_breakdown` can split the message it
   finds in the actual request without reading its words.

The kept spans are a bounded in-memory map (a cache of the recorded spans, not a store):
after a restart the folds of the recorded segments fill it again.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: The reminder whose piece is the offer list.
OFFER_PIECE = "clio.reminder.session_facts_plugins"
#: The context-view category of the offer list.
OFFER_CATEGORY = "offerable"
#: The item id of the list's own lines (its heading and closing line), not a row.
FRAME_ITEM = "offer_list_frame"
#: How many folded texts' spans are kept.
_KEPT = 512

_LOCK = threading.Lock()
_SPANS: OrderedDict[bytes, tuple[dict[str, Any], ...]] = OrderedDict()


class Piece(str):
    """A rendered piece that the context view attributes on its own.

    ``piece_id`` names its reminder; ``items`` are ``(label, characters)`` per row.
    """

    piece_id: str
    items: tuple[tuple[str, int], ...]

    def __new__(cls, text: str, piece_id: str, items: Iterable[tuple[str, int]] = ()) -> "Piece":
        made = super().__new__(cls, text)
        made.piece_id = piece_id
        made.items = tuple(items)
        return made


class Composed(str):
    """A composed text with the spans of its attributed pieces (``pieces``)."""

    pieces: tuple[dict[str, Any], ...]

    def __new__(cls, text: str, pieces: Iterable[Mapping[str, Any]] = ()) -> "Composed":
        made = super().__new__(cls, text)
        made.pieces = tuple(dict(piece) for piece in pieces)
        return made


def join(lines: Sequence[str | None], separator: str = "\n") -> Composed:
    """``separator.join`` of the non-empty lines, with the span of each :class:`Piece`."""

    parts = [line for line in lines if line]
    pieces: list[dict[str, Any]] = []
    offset = 0
    for index, part in enumerate(parts):
        if isinstance(part, Piece):
            pieces.append(
                {
                    "id": part.piece_id,
                    "start": offset,
                    "end": offset + len(part),
                    "items": [{"label": label, "chars": chars} for label, chars in part.items],
                }
            )
        offset += len(part) + (len(separator) if index < len(parts) - 1 else 0)
    return Composed(separator.join(parts), pieces)


def recorded(raw: Any) -> list[dict[str, Any]]:
    """The well-formed spans of a recorded segment's ``pieces`` (anything else dropped)."""

    out: list[dict[str, Any]] = []
    for piece in raw if isinstance(raw, list) else ():
        if not isinstance(piece, Mapping):
            continue
        start, end = piece.get("start"), piece.get("end")
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end):
            continue
        items = [
            {"label": str(item.get("label") or ""), "chars": int(item.get("chars") or 0)}
            for item in piece.get("items") or ()
            if isinstance(item, Mapping)
        ]
        out.append({"id": str(piece.get("id") or ""), "start": start, "end": end, "items": items})
    return out


def _key(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest()


def note(text: str, pieces: Sequence[Mapping[str, Any]], offset: int = 0) -> None:
    """Keep ``pieces`` (spans in the recorded text) for the folded message ``text``, whose
    recorded text starts ``offset`` characters in."""

    spans = tuple(
        {**piece, "start": piece["start"] + offset, "end": piece["end"] + offset}
        for piece in pieces
        if piece["end"] + offset <= len(text)
    )
    if not spans:
        return
    key = _key(text)
    with _LOCK:
        _SPANS[key] = spans
        _SPANS.move_to_end(key)
        while len(_SPANS) > _KEPT:
            _SPANS.popitem(last=False)


def pieces_for(text: str) -> tuple[dict[str, Any], ...]:
    """The kept spans of a folded message's text (``()`` for any other text)."""

    with _LOCK:
        return _SPANS.get(_key(text), ())


def piece_tokens(segments: Iterable[Any], piece_id: str = OFFER_PIECE) -> int:
    """Estimated tokens (characters / 4) of ``piece_id``'s spans in recorded segments,
    for the context view's per-kind fallback."""

    chars = 0
    for segment in segments:
        content = getattr(segment, "content", None)
        if not isinstance(content, Mapping) or getattr(segment, "kind", "") != "user":
            continue
        chars += sum(
            piece["end"] - piece["start"]
            for piece in recorded(content.get("pieces"))
            if piece["id"] == piece_id
        )
    return (chars + 3) // 4 if chars else 0


def with_offer_category(categories: Mapping[str, int], segments: Iterable[Any]) -> dict[str, int]:
    """The per-kind fallback categories with the offer list moved out of ``messages`` into
    :data:`OFFER_CATEGORY`, by its recorded spans."""

    out = dict(categories)
    tokens = min(piece_tokens(segments), out.get("messages", 0))
    if tokens:
        out["messages"] -= tokens
        out[OFFER_CATEGORY] = tokens
    return {key: value for key, value in out.items() if value}
