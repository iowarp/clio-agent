"""Per-step next_thought thought-dedup decision (#732 / #883).

Single owner of the "does this step's next_thought survive cleaning as a visible
row, so its redundant tool_call.thought copy must be cleared?" decision, shared by
the live tool-observer gate (streaming, executor thread) and the read-boundary
reload normalizer. Both call the same ``survives_clean`` kernel so the two paths
cannot drift across the ``_clean_text`` boundary -- the exact divergence #883 names.
"""

from __future__ import annotations

import re
from typing import Callable, Literal, NamedTuple

TOOL_THOUGHT_STAGE = "bridge.tool_thought"

# CLEAR: the step streamed a next_thought that survives cleaning as a visible row.
REASON_OWNS_ROW: Literal["next_thought_owns_visible_text_row"] = (
    "next_thought_owns_visible_text_row"
)
# KEEP: the step streamed next_thought chunks that clean to empty (marker-only) -- #883.
REASON_CLEANED_EMPTY: Literal["thought_kept_next_thought_cleaned_empty"] = (
    "thought_kept_next_thought_cleaned_empty"
)
# KEEP: the step streamed nothing on next_thought (SDK/batch gap).
REASON_NO_STREAM: Literal["thought_kept_no_visible_row"] = "thought_kept_no_visible_row"
# KEEP (reload only): a rowless tool_call in a message where the agent DID own a
# surviving row elsewhere -- the over-clear the old set logic would have caused.
REASON_RELOAD_KEEP: Literal["thought_kept_no_surviving_next_thought_row"] = (
    "thought_kept_no_surviving_next_thought_row"
)

# ChatAdapter field markers "[[ ## field ## ]]" -- the only transform that can empty
# an otherwise non-blank persisted row at the read boundary (delegation.py does the
# same substitution inside the live schema-bound clean).
_FIELD_MARKER_RE = re.compile(r"\s*\[\[\s*##\s*[A-Za-z0-9_]+\s*##\s*\]\]\s*")


class ThoughtDecision(NamedTuple):
    """A live thought-dedup verdict plus its structured audit reason."""

    clear: bool
    reason: str


def survives_clean(text: str, clean: Callable[[str], str]) -> bool:
    """True iff ``text`` run through ``clean`` is non-blank.

    The single survival predicate for both dedup paths (#883). Live injects the
    transcript's schema-bound ``_clean_text``; reload injects ``read_boundary_clean``.
    Format-only (does this clean to empty?), never a keyword/prose judgment.
    """

    return bool(clean(text or "").strip())


def read_boundary_clean(text: str) -> str:
    """Strip ChatAdapter field markers from a persisted row (reload survival check).

    A no-op on already-cleaned post-S2 rows (they carry no markers), so reload stays
    a no-op on fresh data; it only bites pre-S2 RAW marker-bearing rows, keeping a
    pre-S2 marker-only next_thought from over-clearing its sibling tool_call.
    """

    return _FIELD_MARKER_RE.sub(" ", text or "").strip()


def classify_live_thought(had_stream: bool, survived: bool) -> ThoughtDecision:
    """Map a per-step tap classification to a live dedup decision + audit reason."""

    if survived:
        return ThoughtDecision(True, REASON_OWNS_ROW)
    if had_stream:
        return ThoughtDecision(False, REASON_CLEANED_EMPTY)
    return ThoughtDecision(False, REASON_NO_STREAM)
