"""State 8n for the continuity grid: a conversation just below auto-compaction.

The context sits just under the auto-compaction threshold when the transition happens.
After it, one more bulky message crosses the threshold, so the next turn compacts
automatically on the reopened server, and the recall question must still be answered
from the turns before the compaction.

Reaching a real threshold (85 % of a 1M-token window) per cell is not affordable, so
the run lowers it through configuration: start the stack with
``CLIO_AUTOCOMPACT_PCT=0.04`` (40K tokens on a 1M window; the standard agent's first
prompt is about 23K). The two filler messages are sized for that threshold.
"""

from __future__ import annotations

import os
from typing import Any

import continuity_states as cs

#: About 14K and 8K tokens of filler (these number-dense lines run ~1.9 characters a
#: token): the first half leaves the prompt just under 40K, the second crosses it.
_NOTES_CHARS = (27_000, 16_000)


def _notes(part: int, chars: int) -> str:
    lines = []
    station = 0
    while sum(len(line) + 1 for line in lines) < chars:
        station += 1
        lines.append(
            f"Field note {part}.{station}: the tide gauge at post {station} read "
            f"{(station * 37) % 400 / 100:.2f} m at {6 + station % 12:02d}:00, wind "
            f"{station % 9} m/s from {('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')[station % 8]}."
        )
    return "\n".join(lines)


def state_near_compaction(api: cs.Api, label: str) -> cs.Case:
    """State 8n: the context just under the auto-compaction threshold."""
    pct = os.environ.get("CLIO_AUTOCOMPACT_PCT", "")
    case = cs.Case(state="8n-near-compaction", title=f"cc {label} near compaction")
    case.kind = "near_compaction"
    case.sid = api.create(case.title)
    cs._facts_turn(api, case)
    cs._turn(
        api,
        case.sid,
        "Here is the first half of my field notes, for later. Reply with one short "
        "sentence only.\n\n" + _notes(1, _NOTES_CHARS[0]),
        case,
    )
    case.facts["autocompact_pct"] = pct
    case.facts["auto_before"] = str(len(auto_compactions(api, case.sid)))
    case.setup_notes.append(f"CLIO_AUTOCOMPACT_PCT={pct or 'unset (0.85: never reached)'}")
    case.question = "What are the project's codename and lead? One line."
    case.expect = [case.facts["codename"], case.facts["lead"]]
    return case


def cross_threshold(api: cs.Api, case: cs.Case, verdict: cs.Verdict) -> None:
    """After the transition: cross the threshold, so the next turn compacts on its own."""
    status, ack = api.send(
        case.sid,
        "Here is the second half of my field notes. Reply with one short sentence only.\n\n"
        + _notes(2, _NOTES_CHARS[1]),
    )
    settled = cs.wait_settled(api, case.sid, turn=ack.get("message_id") if status < 300 else None)
    verdict.checks["second_half"] = {"http": status, "settled": settled}
    if status >= 300 or settled != "idle":
        verdict.problems.append(f"the second half of the notes failed: {status} {settled}")


def auto_compactions(api: cs.Api, sid: str) -> list[dict[str, Any]]:
    """The compaction records the transcript shows (state 8n requests none by hand).

    A compaction is recorded as an ``injection`` part with source ``summarization``
    (legacy: a ``compaction`` part) on the reply of the turn it ran in.
    """
    rows = []
    for message in api.messages(sid):
        for part in message.get("parts", []):
            if part.get("type") == "compaction" or part.get("source") == "summarization":
                meta = part.get("metadata") or {}
                rows.append(
                    {
                        "message": message["id"],
                        "compacted": meta.get("compacted_message_ids", []),
                    }
                )
    return rows


cs.STATES["8n"] = state_near_compaction
cs.FOLLOWUPS["near_compaction"] = cross_threshold
cs.AFTER_ASK["near_compaction"] = lambda api, case, verdict: _require_auto(api, case, verdict)


def _require_auto(api: cs.Api, case: cs.Case, verdict: cs.Verdict) -> None:
    found = auto_compactions(api, case.sid)
    verdict.checks["auto_compactions"] = found[:3]
    if case.facts.get("auto_before", "0") != "0":
        verdict.problems.append(
            "the context was compacted before the transition (threshold too low)"
        )
    if len(found) <= int(case.facts.get("auto_before", "0")):
        verdict.problems.append("no automatic compaction ran after the threshold was crossed")
