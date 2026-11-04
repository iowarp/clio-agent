"""L1R's recall rounds, read against the session's own stored replies.

The three questions come after L1P's rounds and its compactions (``explore._RECALL_ROUNDS``):

1. the person's first message, which stays verbatim in context after compaction;
2. what the agent said about the second snapshot in its first reply, which after a
   compaction survives only in clio-core (``recall_context``);
3. the per-dose means it first reported, before the change of plan replaced them.

The failure this looks for is a confident wrong answer: numbers that were never in the
earlier reply, given from the summary's paraphrase. ``numbers_precision`` is the share
of the answer's numbers found in the earlier reply; ``numbers_recall`` the share of the
earlier reply's numbers given back. Whether the agent looked (``recall_context`` calls,
or a ``search_tools`` load of it) is reported, not graded: answering from context is
right for question 1.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["grade_recall"]

#: Distinctive sentences of L1R's first message (``explore._X2_SETUP`` and its round 1).
FIRST_MESSAGE_MARKERS = (
    "This folder holds the export of OPAL experiment 67",
    "Explore this export and tell me what it contains and how it is shaped",
)
_NUMBER = re.compile(r"(?<![\w.])\d+\.\d+|(?<![\w.])\d{2,}(?!\w|\.\d)")
#: Where the rounds sit in L1R's replies: round 1 is 0, X2's peak question is 2, and
#: the three recall rounds are the last three.
_FIRST, _PEAK = 0, 2


#: The dose series' own labels: naming a dose is not a claim about the data.
_DOSES = frozenset({"0", "50", "100", "200", "500", "1000"})


def _numbers(text: str) -> set[str]:
    found = {m.group(0).rstrip("0").rstrip(".") if "." in m.group(0) else m.group(0)
             for m in _NUMBER.finditer(text)}  # fmt: skip
    return found - _DOSES


def _decimals(number: str) -> int:
    return len(number.partition(".")[2])


def _same(a: str, b: str) -> bool:
    """Equal, or the less precise one is the other rounded (0.745 and 0.7451): a table
    given again at more or fewer decimals is the same claim."""
    if a == b:
        return True
    rough, fine = sorted((a, b), key=_decimals)
    places = _decimals(rough)
    return places > 0 and float(rough) == round(float(fine), places)


def _overlap(answer: str, source: str) -> dict[str, Any]:
    said, given = _numbers(source), _numbers(answer)
    found = {g for g in given if any(_same(g, s) for s in said)}
    recalled = {s for s in said if any(_same(g, s) for g in given)}
    return {
        "numbers_in_answer": len(given),
        "numbers_precision": round(len(found) / len(given), 3) if given else None,
        "numbers_recall": round(len(recalled) / len(said), 3) if said else None,
        "numbers_not_said": sorted(given - found)[:12],
    }


def grade_recall(run: dict[str, Any]) -> dict[str, Any]:
    """The three recall answers against the replies they ask about, and the lookups made."""
    replies = [str(r) for r in run.get("replies") or []]
    if len(replies) < 13:
        return {"graded": False, "reason": f"{len(replies)} replies, 13 expected"}
    first_q, snapshot_q, means_q = replies[-3:]
    calls = [c for s in run.get("sessions") or [] for c in s.get("tool_calls") or []]
    recalls = [c for c in calls if str(c.get("tool") or "") == "recall_context"]
    loads = [
        c for c in calls
        if str(c.get("tool") or "") == "search_tools"
        and "recall_context" in str((c.get("args") or {}).get("query") or "")
    ]  # fmt: skip
    quoted = [m for m in FIRST_MESSAGE_MARKERS if m.lower() in first_q.lower()]
    return {
        "graded": True,
        "first_message": {"markers_quoted": len(quoted), "of": len(FIRST_MESSAGE_MARKERS)},
        "first_reply_snapshot": _overlap(snapshot_q, replies[_FIRST]),
        "means_before_change": _overlap(means_q, replies[_PEAK]),
        "recall_context_calls": len(recalls),
        "recall_context_args": [c.get("args") for c in recalls][:6],
        "recall_context_loaded": bool(loads),
    }
