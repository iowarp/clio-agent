"""Per-step tap-classification memo for the #883 thought-dedup gate.

Split out of ``transcript.py``'s ``TurnTranscript`` (#1333 ratchet payment). A
model step can issue several tool calls in one parallel batch; every call
shares the same parent span and therefore one next_thought owner. This
memoizes that step's verdict so the FIRST call does not consume the evidence
and leave the remaining calls carrying duplicate thought copies.
"""

from __future__ import annotations

import threading

TapKey = tuple[str, str]
StepVerdictKey = tuple[str, str, str]
Verdict = tuple[bool, bool]


def resolve_tap_step_verdict(
    *,
    lock: threading.RLock,
    tap_streamed: dict[TapKey, list[str]],
    tap_gate_cursor: dict[TapKey, int],
    tap_step_verdicts: dict[StepVerdictKey, Verdict],
    agent_id: str,
    field: str,
    step_id: str,
) -> Verdict:
    """Return ``(had_stream, survives_clean)`` for one tap-gate consuming read.

    Memoized per ``(agent_id, field, step_id)`` so a parallel tool-call batch
    sharing one step reads the SAME verdict instead of each call separately
    consuming (and thereby exhausting) the tap slice. See
    ``TurnTranscript.tap_step_survives_clean`` for the full field/whitespace
    contract this implements.
    """

    key = (agent_id, field)
    verdict_key = (agent_id, field, step_id)
    with lock:
        if step_id and verdict_key in tap_step_verdicts:
            return tap_step_verdicts[verdict_key]
        chunks = tap_streamed.get(key, [])
        start = tap_gate_cursor.get(key, 0)
        tap_gate_cursor[key] = len(chunks)
        tail = "".join(chunks[start:])
    survived = bool(tail.strip())
    verdict = (survived, survived)
    if step_id:
        with lock:
            tap_step_verdicts[verdict_key] = verdict
    return verdict
