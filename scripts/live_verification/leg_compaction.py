"""Live leg: compaction is VISIBLE and LOSSLESS (Phase 11b, #1571).

Verifies the compaction contract as landed on ``rework_agent`` -- read from the code,
not a plan:

* ``POST /v1/sessions/{sid}/compact`` (``gact/routes/sessions.py`` ->
  ``gact/compaction.py::compact_session_context``, ``trigger="manual"``) answers
  ``{session_id, compacted, compactions: [{session_id, compaction_id, scope, trigger,
  turn_id, message_id, part_id, replaced_count, summary}]}``, one entry per compacted
  agent scope (no ``?scope=``: every agent scope; this single-agent session has one,
  ``main``).
* The record (``gact/summarization_record.py`` / ``gact/compaction_record.py``): an
  ``injection`` part, ``source: "summarization"``, carrying ``text`` (the summary the
  model gets, ending with the ``recall_context(ids=["<compaction_id>"])`` line),
  ``trigger``, ``compaction_id`` and ``metadata.derived_from`` (the clio-core ids the
  summary replaced) / ``metadata.compacted_message_ids`` (the transcript rows it
  stands in for). Between turns -- both compacts here -- it is its OWN assistant row
  ``msg_summary_*`` (``turn_id ""``), so the ledger grows by exactly one row.
* The events (``compaction._event`` -> highway semantic events, typed on the v3 wire by
  ``gact/protocol/v3/event.py::_semantic_event``): ``compaction.started`` then
  ``compaction.completed``, same ``compaction_id``, ``trigger: "manual"``; the completed
  payload equals the response entry minus ``summary``. The SSE probe subscribes with
  ``x-gact-version: 0.3`` and keeps every ``compaction.*`` frame.
* What is replaced (``gact/compaction_policy.py``, defaults ``compaction.keep.head=true``,
  ``keep.last_turns=0``, ``keep.last_steps=0`` -- pinned in the app env below so a
  stray operator setting cannot change the expectation): ``keep_head`` keeps the OPEN
  turn's question, and between turns there is none, so the policy summarizes the
  scope's WHOLE live working set. The leg reads that working set from
  ``GET /v1/sessions/{sid}/context/state?scope=main`` (segments whose kind is in
  ``arc/schema.py::WORKING_SET_KINDS``) right before each compact and expects
  ``replaced_count == len(it)`` and ``derived_from == its ids, in order``. After the
  fold the working set is exactly one ``summary`` segment naming the compaction.
  The second compact therefore replaces summary #1 plus the segments turn 5 added --
  never the first compaction's originals again.
* Rows (delta-based, never a hardcoded literal): 4 turns x 2 rows = 8, +1 record = 9,
  turn 5 adds its rows, +1 record. The old leg's "13 after compact2" was wrong: the
  delta arithmetic gives 12 for this scenario (recorded as ``rows_trace``).
* Restart: ``GET /messages`` is byte-equal across an app restart (kept from #1339).
* Lossless: after the restart, one realistic short user turn asks for the originals
  back; the agent's ``recall_context`` call by a compaction id must return exactly
  that compaction's ``derived_from`` atoms, marked compacted, with content byte-equal
  to the segments the leg read before the compaction. There is no HTTP route for
  ``recall_context`` (an agent tool only), so the leg drives the agent.

Kept from the #1339 leg (not part of the compaction contract): the loop-liveness
probe, ``store.write_on_loop_thread`` count, the message-part lane chunk-family
evidence, the SSE gap probe, the context-frame view after turn 5.

Every judgement is a pure function over saved evidence (``messages_after_*.json``,
``context_state_*.json``, ``compact*_response.json``, ``compaction_events.json``,
``context_frames.json``, ``sse.log``), so a run's verdict can be replayed offline.

Run under the private real-CTE daemon (a gate never holds ARC-local)::

    uv run --no-sync python scripts/live_verification/run_with_private_cte.py \\
        scripts/live_verification/leg_compaction.py --provider codex
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import socket
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    OUT_ROOT,
    allow_all,
    bind_provider,
    boot_server,
    client,
    create_session,
    create_workspace,
    dump_json,
    expanding_wait,
    post_message,
    session_messages,
    terminate_server,
    wait_health,
    wait_turn,
    write_verdict,
)

#: Small chunks on the message-part atom family so the lane-rollover evidence is
#: exercised within 5 turns (``gact/part_atoms.py::_MESSAGE_PART_CHUNK_ENV``).
CHUNK_SEGMENTS_ENV = "CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS"

#: Message-part lane record-name infix (``"<sid>___events~m"`` / ``"..._events~m~N"``),
#: see ``tests/test_arc/test_storage_companion.py``.
MESSAGE_PART_LANE_INFIX = "_events~m"

#: The single agent scope of this session (``gact/agents`` main agent).
AGENT_SCOPE = "main"

#: ``arc/schema.py::WORKING_SET_KINDS`` -- the kinds compaction's policy reads.
WORKING_SET_KINDS = frozenset(
    {"system", "user", "tool_def", "thought", "tool_call", "observation", "summary"}
)

#: ``compaction_policy.keep_policy`` defaults, pinned so the expectation is hermetic.
KEEP_POLICY_ENV = {
    "CLIO_COMPACTION_KEEP_HEAD": "true",
    "CLIO_COMPACTION_KEEP_LAST_TURNS": "0",
    "CLIO_COMPACTION_KEEP_LAST_STEPS": "0",
}

TURN_PROMPT = "Reply with exactly the single word ack-{n} and nothing else."

#: A realistic, short human ask for the compacted originals (owner rule: no scripted
#: tool steps in live prompts).
RECALL_PROMPT = "Can you pull up the original messages that summary replaced, word for word?"

RECALL_TOOL = "recall_context"
COMPACTION_EVENT_TYPES = ("compaction.started", "compaction.completed", "compaction.failed")
RESPONSE_ENTRY_KEYS = {
    "session_id",
    "compaction_id",
    "scope",
    "trigger",
    "turn_id",
    "message_id",
    "part_id",
    "replaced_count",
    "summary",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _HealthProbe(threading.Thread):
    """Poll one GET every 250 ms and keep every latency, wallclock-stamped."""

    def __init__(self, base: str, path: str = "/v1/health", name: str = "health") -> None:
        super().__init__(name=f"{name}-probe", daemon=True)
        self._base = base
        self._path = path
        self.name_tag = name
        self.stop = threading.Event()
        self.max_latency_s = 0.0
        self.samples = 0
        self.failures = 0
        self.rows: list[tuple[float, float]] = []

    def run(self) -> None:
        import requests

        while not self.stop.is_set():
            wall = time.time()
            t0 = time.monotonic()
            try:
                requests.get(f"{self._base}{self._path}", timeout=10)
            except Exception:  # noqa: BLE001 - a failed probe is itself the finding
                self.failures += 1
            latency = time.monotonic() - t0
            self.rows.append((wall, latency))
            self.max_latency_s = max(self.max_latency_s, latency)
            self.samples += 1
            self.stop.wait(0.25)

    def slow(self, threshold_s: float = 0.5) -> list[dict[str, Any]]:
        return [
            {
                "at": _dt.datetime.fromtimestamp(wall, tz=_dt.UTC).isoformat(),
                "latency_s": round(lat, 3),
            }
            for wall, lat in self.rows
            if lat >= threshold_s
        ]


class _SseProbe(threading.Thread):
    """Read the session's v3 SSE stream: the max gap between events, plus every
    ``compaction.*`` frame (``{"event", "payload", "wall"}``) in arrival order."""

    def __init__(self, base: str, sid: str) -> None:
        super().__init__(name="sse-probe", daemon=True)
        self._url = f"{base}/v1/sessions/{sid}/events"
        self.stop = threading.Event()
        self.max_gap_s = 0.0
        self.events = 0
        self.error = ""
        self.compaction_frames: list[dict[str, Any]] = []

    def run(self) -> None:
        import requests

        last = time.monotonic()
        event_type = ""
        data_lines: list[str] = []
        try:
            with requests.get(
                self._url,
                stream=True,
                timeout=(10, 60),
                headers={"Accept": "text/event-stream", "x-gact-version": "0.3"},
            ) as r:
                for line in r.iter_lines(decode_unicode=True):
                    if self.stop.is_set():
                        break
                    if line is None:
                        continue
                    if line == "":
                        self._frame(event_type, data_lines)
                        event_type, data_lines = "", []
                        continue
                    if line.startswith("event:"):
                        now = time.monotonic()
                        self.max_gap_s = max(self.max_gap_s, now - last)
                        last = now
                        self.events += 1
                        event_type = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:") :].strip())
        except Exception as exc:  # noqa: BLE001 - recorded, not asserted
            self.error = f"{type(exc).__name__}: {exc}"

    def _frame(self, event_type: str, data_lines: list[str]) -> None:
        if event_type not in COMPACTION_EVENT_TYPES or not data_lines:
            return
        try:
            envelope = json.loads("\n".join(data_lines))
        except ValueError:
            envelope = {}
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        self.compaction_frames.append(
            {"event": event_type, "payload": payload or {}, "wall": time.time()}
        )


# --------------------------------------------------------------------------- #
# Evidence readers.
# --------------------------------------------------------------------------- #
def _store_writes_on_loop(sse_log: Path) -> int:
    """Count refused server-loop store writes (#1334)."""

    if not sse_log.exists():
        return 0
    count = 0
    for line in sse_log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("stage") == "store.write_on_loop_thread":
            count += 1
    return count


def _sse_rows_since(sse_log: Path, after_ts: float, event_type: str) -> list[dict[str, Any]]:
    """``sse.write`` audit rows (``gact/routes/misc.py``) for ``event_type`` at/after ``after_ts``."""

    if not sse_log.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in sse_log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("stage") != "sse.write" or row.get("event_type") != event_type:
            continue
        if float(row.get("ts") or 0.0) >= after_ts:
            rows.append(row)
    return rows


def _read_ledger_file(state_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """The per-session ledger file ``gact/messages.py::MessageStore`` persists (chronological)."""

    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in session_id)
    path = state_dir / "messages" / f"{safe}.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return raw if isinstance(raw, list) else []


def _messages_chronological(call: Any, sid: str) -> list[dict[str, Any]]:
    """``GET /messages`` oldest-first (the wire is newest-first by design)."""

    return list(reversed(session_messages(call, sid)))


def _context_state(call: Any, sid: str, scope: str) -> dict[str, Any]:
    """``GET /v1/sessions/{sid}/context/state?scope=`` (the live segments of ``scope``)."""

    return dict(call("GET", f"/v1/sessions/{sid}/context/state", params={"scope": scope}))


def _lane_chunk_family_evidence(
    sse_log: Path,
    sid: str,
    last_turn_start: float,
    last_turn_end: float,
) -> dict[str, Any]:
    """The #1339 lane-rollover / no-sibling-amplification evidence (``store.put`` rows).

    Over the whole run: more than one chunk record of this session's message-part lane
    was put, and the chunk index of the chronological put sequence never decreases (a
    sealed chunk is never re-encoded). ``last_turn_*`` is descriptive only.
    """

    prefix = f"{sid}__"
    result: dict[str, Any] = {
        "evidence_source": "arc/storage.py store.put audit rows, kind=='segments'",
        "audit_rows_found": 0,
        "whole_run_distinct_chunk_names": [],
        "highest_chunk_index": None,
        "highest_chunk_names": [],
        "chronological_chunk_index_sequence": [],
        "last_turn_rows_found": 0,
        "last_turn_distinct_chunk_names": [],
        "verified_whole_run_multiple_chunks": None,
        "verified_no_sealed_chunk_reamplified": None,
    }
    if not sse_log.exists():
        return result

    def _chunk_index(name: str) -> int | None:
        if not name.startswith(prefix):
            return None
        tail = name[len(prefix) :]
        if tail == MESSAGE_PART_LANE_INFIX:
            return 1
        infix_chunk_prefix = MESSAGE_PART_LANE_INFIX + "~"
        if tail.startswith(infix_chunk_prefix):
            suffix = tail[len(infix_chunk_prefix) :]
            if suffix.isdigit():
                return int(suffix)
        return None

    whole_run: dict[str, int] = {}
    last_turn_names: set[str] = set()
    sequence: list[tuple[float, int]] = []
    last_turn_rows = 0
    for line in sse_log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("stage") != "store.put" or row.get("kind") != "segments":
            continue
        name = str(row.get("name") or "")
        idx = _chunk_index(name)
        if idx is None:
            continue
        whole_run[name] = idx
        ts = float(row.get("ts") or 0.0)
        sequence.append((ts, idx))
        if last_turn_start <= ts < last_turn_end:
            last_turn_names.add(name)
            last_turn_rows += 1

    sequence.sort(key=lambda pair: pair[0])
    result["audit_rows_found"] = len(sequence)
    result["whole_run_distinct_chunk_names"] = sorted(whole_run)
    result["chronological_chunk_index_sequence"] = [idx for _ts, idx in sequence]
    result["last_turn_rows_found"] = last_turn_rows
    result["last_turn_distinct_chunk_names"] = sorted(last_turn_names)
    if whole_run:
        highest_index = max(whole_run.values())
        result["highest_chunk_index"] = highest_index
        result["highest_chunk_names"] = sorted(
            n for n, i in whole_run.items() if i == highest_index
        )
        result["verified_whole_run_multiple_chunks"] = len(whole_run) > 1
        indices = [idx for _ts, idx in sequence]
        result["verified_no_sealed_chunk_reamplified"] = all(
            a <= b for a, b in zip(indices, indices[1:], strict=False)
        )
    return result


# --------------------------------------------------------------------------- #
# Pure judgements (replayable over saved evidence).
# --------------------------------------------------------------------------- #
def _ids(rows: Sequence[dict[str, Any]]) -> list[str]:
    return [str(r.get("id") or "") for r in rows]


def working_set(context_state: dict[str, Any]) -> list[dict[str, Any]]:
    """The scope's live working-set segments, in render order (what compaction reads)."""

    return [
        seg
        for seg in context_state.get("segments") or []
        if seg.get("kind") in WORKING_SET_KINDS and seg.get("status", "live") == "live"
    ]


def expected_replaced_ids(context_state_before: dict[str, Any]) -> list[str]:
    """What the default keep policy summarizes BETWEEN turns: the whole working set.

    ``keep_head`` keeps only the OPEN turn's question (none between turns);
    ``keep_last_turns`` / ``keep_last_steps`` are 0. So every live working-set id is
    replaced, in live order (``compaction_policy.post_compaction_context``).
    """

    return _ids(working_set(context_state_before))


def check_compact_response(
    resp: dict[str, Any], sid: str, *, scope: str, expected_ids: Sequence[str]
) -> dict[str, bool]:
    """The ``POST /compact`` answer: one manual compaction of ``scope``."""

    entries = resp.get("compactions") or []
    entry = entries[0] if len(entries) == 1 else {}
    return {
        "response_compacted_true": resp.get("compacted") is True,
        "response_session_id": resp.get("session_id") == sid,
        "response_one_compaction_for_the_scope": len(entries) == 1 and entry.get("scope") == scope,
        "response_entry_keys": RESPONSE_ENTRY_KEYS <= set(entry),
        "response_entry_ids_present": bool(
            str(entry.get("compaction_id") or "").startswith("cmp_")
            and entry.get("message_id")
            and entry.get("part_id")
        ),
        "response_trigger_manual_between_turns": entry.get("trigger") == "manual"
        and entry.get("turn_id") == "",
        "response_replaced_count_matches_policy": bool(expected_ids)
        and entry.get("replaced_count") == len(expected_ids),
        "response_summary_names_recall_by_id": bool(entry.get("summary"))
        and f'{RECALL_TOOL}(ids=["{entry.get("compaction_id")}"])' in str(entry.get("summary")),
    }


def record_row(messages_after: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The newest row (between-turns compaction record) in a chronological transcript."""

    return dict(messages_after[-1]) if messages_after else {}


def check_record_row(
    messages_before: Sequence[dict[str, Any]],
    messages_after: Sequence[dict[str, Any]],
    *,
    expected_covered_rows: Sequence[str],
) -> dict[str, bool]:
    """The transcript side of one between-turns compaction (no response needed)."""

    row = record_row(messages_after)
    parts = row.get("parts") or []
    part = parts[0] if len(parts) == 1 else {}
    meta = part.get("metadata") or {}
    compaction_id = str(part.get("compaction_id") or "")
    derived = meta.get("derived_from") or []
    return {
        "ledger_grows_by_exactly_one": len(messages_after) == len(messages_before) + 1,
        "prior_rows_unchanged_in_order": _ids(messages_after[: len(messages_before)])
        == _ids(messages_before),
        "record_is_own_summary_row": str(row.get("id") or "").startswith("msg_summary_")
        and row.get("role") == "assistant"
        and row.get("turn_id", "") == ""
        and len(parts) == 1,
        "record_part_injection_summarization": part.get("type") == "injection"
        and part.get("source") == "summarization",
        "record_trigger_manual": part.get("trigger") == "manual",
        "record_has_compaction_id": compaction_id.startswith("cmp_")
        and (row.get("metadata") or {}).get("compaction_id") == compaction_id,
        "record_text_ends_with_recall_line": bool(part.get("text"))
        and f'{RECALL_TOOL}(ids=["{compaction_id}"])' in str(part.get("text")).splitlines()[-1],
        "record_derived_from_nonempty_unique": bool(derived) and len(set(derived)) == len(derived),
        "record_covers_expected_rows": [str(x) for x in meta.get("compacted_message_ids") or []]
        == list(expected_covered_rows),
    }


def check_record_matches_response(
    messages_after: Sequence[dict[str, Any]],
    entry: dict[str, Any],
    *,
    expected_ids: Sequence[str],
) -> dict[str, bool]:
    """The record row is the one the response names, and replaced what the policy chose."""

    row = record_row(messages_after)
    part = (row.get("parts") or [{}])[0]
    derived = [str(x) for x in (part.get("metadata") or {}).get("derived_from") or []]
    return {
        "record_is_response_message_and_part": row.get("id") == entry.get("message_id")
        and part.get("id") == entry.get("part_id"),
        "record_compaction_id_matches_response": part.get("compaction_id")
        == entry.get("compaction_id"),
        "record_text_is_response_summary": part.get("text") == entry.get("summary"),
        "record_derived_from_is_policy_selection": derived == list(expected_ids),
        "record_derived_count_is_replaced_count": len(derived) == entry.get("replaced_count"),
    }


def check_compaction_events(
    frames: Sequence[dict[str, Any]], entry: dict[str, Any]
) -> dict[str, bool]:
    """``compaction.started`` then ``compaction.completed`` for this compaction, paired."""

    cid = entry.get("compaction_id")
    mine = [f for f in frames if (f.get("payload") or {}).get("compaction_id") == cid]
    kinds = [f.get("event") for f in mine]
    empty: dict[str, Any] = {}
    started: dict[str, Any] = next(
        (f["payload"] for f in mine if f.get("event") == "compaction.started"), empty
    )
    completed: dict[str, Any] = next(
        (f["payload"] for f in mine if f.get("event") == "compaction.completed"), empty
    )
    return {
        "events_started_then_completed": kinds == ["compaction.started", "compaction.completed"],
        "events_trigger_manual": started.get("trigger") == "manual"
        and completed.get("trigger") == "manual",
        "events_started_is_prefix_of_completed": bool(started)
        and all(completed.get(k) == v for k, v in started.items()),
        "events_completed_equals_response": bool(completed)
        and completed == {k: v for k, v in entry.items() if k != "summary"},
    }


def check_folded_state(
    context_state_after: dict[str, Any], entry: dict[str, Any], *, expected_ids: Sequence[str]
) -> dict[str, bool]:
    """After the fold the scope's working set is exactly one summary of this compaction."""

    live = working_set(context_state_after)
    seg = live[0] if len(live) == 1 else {}
    return {
        "fold_leaves_one_summary_segment": seg.get("kind") == "summary",
        "fold_summary_names_compaction": (seg.get("content") or {}).get("compaction_id")
        == entry.get("compaction_id"),
        "fold_summary_derived_from_replaced": list(seg.get("derived_from") or [])
        == list(expected_ids),
    }


def check_second_selection(
    state_after_compact1: dict[str, Any], state_before_compact2: dict[str, Any]
) -> dict[str, bool]:
    """Compaction 2 replaces summary #1 plus what turn 5 added -- never #1's originals."""

    after1 = _ids(working_set(state_after_compact1))
    before2 = _ids(working_set(state_before_compact2))
    return {
        "compact2_selection_starts_with_summary1": len(after1) == 1 and before2[:1] == after1,
        "compact2_selection_adds_turn5_steps": len(before2) > len(after1),
    }


def check_context_frame(
    frames: Sequence[dict[str, Any]], *, covered_ids: Sequence[str], record_id: str
) -> dict[str, bool]:
    """Turn 5's context frame: rows compaction 1 covers excluded, its record visible."""

    items = (frames[-1].get("items") if frames else None) or []
    covered = set(covered_ids)
    compacted = [it for it in items if it.get("source_id") in covered]
    record: dict[str, Any] = next((it for it in items if it.get("source_id") == record_id), {})
    return {
        "context_frame_found_after_turn_five": bool(frames),
        "context_frame_marks_every_covered_row": bool(covered) and len(compacted) == len(covered),
        "context_frame_covered_rows_excluded_as_compacted": bool(compacted)
        and all(
            it.get("included") is False and it.get("reason") == "compacted" for it in compacted
        ),
        "context_frame_record_visible": record.get("included") is True
        and record.get("reason") == "visible_transcript",
    }


def check_restart(
    before: Sequence[dict[str, Any]], after: Sequence[dict[str, Any]]
) -> dict[str, bool]:
    """``GET /messages`` survives an app restart unchanged, byte for byte."""

    return {
        "messages_survive_restart_same_count": len(after) == len(before),
        "messages_survive_restart_same_order": _ids(before) == _ids(after),
        "messages_survive_restart_byte_equal": json.dumps(list(before), sort_keys=True)
        == json.dumps(list(after), sort_keys=True),
    }


def recall_calls(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ``recall_context`` call in ``messages``: ``{input, is_error, text}``."""

    out: list[dict[str, Any]] = []
    for message in messages:
        parts = message.get("parts") or []
        inputs = {
            p.get("call_id"): p.get("input") or {}
            for p in parts
            if p.get("type") == "tool_call" and p.get("tool_name") == RECALL_TOOL
        }
        for p in parts:
            if p.get("type") != "tool_result" or p.get("call_id") not in inputs:
                continue
            text = "".join(str(c.get("text") or "") for c in p.get("content") or [])
            out.append(
                {"input": inputs[p["call_id"]], "is_error": bool(p.get("is_error")), "text": text}
            )
    return out


def check_recall(
    messages: Sequence[dict[str, Any]],
    entries: Sequence[dict[str, Any]],
    derived_by_compaction: dict[str, list[str]],
    segments_before: dict[str, dict[str, Any]],
) -> tuple[dict[str, bool], dict[str, Any]]:
    """The agent's ``recall_context`` by a compaction id returns its originals byte-exact.

    ``derived_by_compaction`` maps each compaction id to the ids it replaced;
    ``segments_before`` maps a segment id to the segment the leg read from
    ``/context/state`` before it was compacted.
    """

    calls = recall_calls(messages)
    known = {str(e.get("compaction_id")) for e in entries}
    by_id: list[dict[str, Any]] = []
    for c in calls:
        asked = [str(x) for x in c["input"].get("ids") or []]
        if c["is_error"] or not asked or not set(asked) & known:
            continue
        try:
            rows = json.loads(c["text"])
        except ValueError:
            rows = None
        by_id.append({"asked": asked, "rows": rows})
    exact = [b for b in by_id if isinstance(b["rows"], list)]

    def _expected(asked: list[str]) -> list[str]:
        return [i for a in asked for i in derived_by_compaction.get(a, [a])]

    def _byte_exact(rows: list[dict[str, Any]]) -> bool:
        return bool(rows) and all(
            r.get("id") in segments_before
            and json.dumps(r.get("content"), sort_keys=True)
            == json.dumps(segments_before[r["id"]].get("content"), sort_keys=True)
            for r in rows
        )

    checks = {
        "recall_context_called_ok": any(not c["is_error"] for c in calls),
        "recall_by_compaction_id_called": bool(by_id),
        "recall_by_compaction_id_returns_its_replaced_ids": bool(exact)
        and all(_ids(b["rows"]) == _expected(b["asked"]) for b in exact),
        "recall_rows_marked_compacted": bool(exact)
        and all(r.get("compacted") is True for b in exact for r in b["rows"]),
        "recall_rows_byte_exact": bool(exact) and all(_byte_exact(b["rows"]) for b in exact),
    }
    evidence = {
        "calls": [{"input": c["input"], "is_error": c["is_error"]} for c in calls],
        "by_compaction_id": [
            {
                "asked": b["asked"],
                "returned_ids": _ids(b["rows"]) if isinstance(b["rows"], list) else None,
            }
            for b in by_id
        ],
    }
    return checks, evidence


def _prefixed(prefix: str, checks: dict[str, bool]) -> dict[str, bool]:
    return {f"{prefix}_{k}": v for k, v in checks.items()}


# --------------------------------------------------------------------------- #
# The live run.
# --------------------------------------------------------------------------- #
def _compact(
    call: Callable[..., Any],
    sid: str,
    out: Path,
    n: int,
    sse: _SseProbe,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str], float]:
    """Read the working set, compact, read it again; wait for the paired events."""

    state_before = _context_state(call, sid, AGENT_SCOPE)
    dump_json(out / f"context_state_before_compact{n}.json", state_before)
    expected = expected_replaced_ids(state_before)
    wall = time.time()
    resp = dict(call("POST", f"/v1/sessions/{sid}/compact", {}))
    dump_json(out / f"compact{n}_response.json", resp)
    entries = resp.get("compactions") or [{}]
    cid = entries[0].get("compaction_id")
    expanding_wait(
        lambda: any(
            f["event"] == "compaction.completed" and f["payload"].get("compaction_id") == cid
            for f in sse.compaction_frames
        ),
        what=f"compaction.completed for {cid} on the v3 stream",
        initial=0.25,
        max_elapsed=60.0,
    )
    state_after = _context_state(call, sid, AGENT_SCOPE)
    dump_json(out / f"context_state_after_compact{n}.json", state_after)
    return resp, state_before, state_after, expected, wall


def run_leg(
    provider: str,
    model: str,
    out: Path,
    *,
    max_health_latency_s: float = 1.0,
    chunk_segments: int = 8,
) -> dict[str, Any]:
    """Boot an isolated server, drive the compaction scenario, judge the evidence."""

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    state = out / "state"
    ws_root = out / "workspace"
    state.mkdir(parents=True, exist_ok=True)
    sse_log = out / "sse.log"
    extra_env = {
        "CLIO_USER_DIR": str(state),
        "CLIO_SESSIONS_PATH": str(state / "sessions.json"),
        "CLIO_ALLOWED_ROOTS": str(ws_root),
        CHUNK_SEGMENTS_ENV: str(chunk_segments),
        # Pin the boot-config provider so the first bind never touches LM Studio.
        "CLIO_LM_PROVIDER": provider,
        **KEEP_POLICY_ENV,
    }
    proc = boot_server(port, cwd=ws_root, sse_log=sse_log, extra_env=extra_env)
    call = client(base)
    verdict: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "port": port,
        "chunk_segments_env": chunk_segments,
        "keep_policy_env": KEEP_POLICY_ENV,
        "turn_statuses": [],
    }
    checks: dict[str, bool] = {}
    evidence_gaps: list[str] = []
    health = _HealthProbe(base)
    loop_probe: _HealthProbe | None = None
    sse: _SseProbe | None = None
    try:
        if not wait_health(call):
            raise RuntimeError("server never became healthy")
        verdict["provider_ready"] = bind_provider(call, provider=provider, model=model)
        allow_all(call)  # MUST run before the first turn (house rule, #1274/#1275)
        wsid = create_workspace(call, "compaction-11b", ws_root)
        sid = create_session(call, wsid, "compaction-visible-11b")
        verdict["session_id"] = sid

        sse = _SseProbe(base, sid)
        sse.start()
        health.start()
        loop_probe = _HealthProbe(base, f"/v1/sessions/{sid}", "loop")
        loop_probe.start()

        # 1. Four short turns, then the FIRST manual compact.
        for n in range(1, 5):
            post_message(call, sid, TURN_PROMPT.format(n=n))
            verdict["turn_statuses"].append(wait_turn(call, wsid, sid, max_elapsed=180.0))
        checks["turns_1_4_completed"] = all(
            s in ("idle", "completed") for s in verdict["turn_statuses"][:4]
        )
        messages_after_turns = _messages_chronological(call, sid)

        resp1, before1, after1, expected1, compact1_wall = _compact(call, sid, out, 1, sse)
        entry1 = (resp1.get("compactions") or [{}])[0]
        messages_after_compact1 = _messages_chronological(call, sid)
        dump_json(out / "messages_after_compact1.json", {"chronological": messages_after_compact1})
        checks.update(
            _prefixed(
                "compact1",
                {
                    **check_compact_response(resp1, sid, scope=AGENT_SCOPE, expected_ids=expected1),
                    **check_record_row(
                        messages_after_turns,
                        messages_after_compact1,
                        expected_covered_rows=_ids(messages_after_turns),
                    ),
                    **check_record_matches_response(
                        messages_after_compact1, entry1, expected_ids=expected1
                    ),
                    **check_compaction_events(sse.compaction_frames, entry1),
                    **check_folded_state(after1, entry1, expected_ids=expected1),
                },
            )
        )
        record1_id = str(record_row(messages_after_compact1).get("id") or "")
        checks["ledger_file_row_count_matches_api_after_compact1"] = len(
            _read_ledger_file(state, sid)
        ) == len(messages_after_compact1)
        checks["sse_message_created_seen_after_compact1"] = bool(
            _sse_rows_since(sse_log, compact1_wall, "message.created")
        )

        # 2. One more turn; its context frame; the SECOND manual compact.
        turn5_wall = time.time()
        post_message(call, sid, TURN_PROMPT.format(n=5))
        status5 = wait_turn(call, wsid, sid, max_elapsed=180.0)
        verdict["turn_statuses"].append(status5)
        checks["turn_five_completed"] = status5 in ("idle", "completed")

        frames = call("GET", f"/v1/sessions/{sid}/context/frames").get("frames", [])
        dump_json(out / "context_frames.json", frames)
        checks.update(
            check_context_frame(
                frames, covered_ids=_ids(messages_after_turns), record_id=record1_id
            )
        )

        messages_after_turn5 = _messages_chronological(call, sid)
        new_since_compact1 = _ids(messages_after_turn5[len(messages_after_compact1) :])

        resp2, before2, after2, expected2, compact2_wall = _compact(call, sid, out, 2, sse)
        entry2 = (resp2.get("compactions") or [{}])[0]
        messages_after_compact2 = _messages_chronological(call, sid)
        dump_json(out / "messages_after_compact2.json", {"chronological": messages_after_compact2})
        checks.update(
            _prefixed(
                "compact2",
                {
                    **check_compact_response(resp2, sid, scope=AGENT_SCOPE, expected_ids=expected2),
                    **check_record_row(
                        messages_after_turn5,
                        messages_after_compact2,
                        expected_covered_rows=[record1_id, *new_since_compact1],
                    ),
                    **check_record_matches_response(
                        messages_after_compact2, entry2, expected_ids=expected2
                    ),
                    **check_compaction_events(sse.compaction_frames, entry2),
                    **check_folded_state(after2, entry2, expected_ids=expected2),
                },
            )
        )
        checks.update(check_second_selection(after1, before2))
        checks["sse_message_created_seen_after_compact2"] = bool(
            _sse_rows_since(sse_log, compact2_wall, "message.created")
        )
        checks["no_compaction_failed_event"] = not any(
            f["event"] == "compaction.failed" for f in sse.compaction_frames
        )
        dump_json(out / "compaction_events.json", sse.compaction_frames)
        verdict["rows_trace"] = {
            "after_turns_1_4": len(messages_after_turns),
            "after_compact1": len(messages_after_compact1),
            "after_turn5": len(messages_after_turn5),
            "after_compact2": len(messages_after_compact2),
            "expected_after_compact2": len(messages_after_turn5) + 1,
        }
        verdict["replaced_counts"] = {
            "compact1": {"expected": len(expected1), "response": entry1.get("replaced_count")},
            "compact2": {"expected": len(expected2), "response": entry2.get("replaced_count")},
        }

        # Lane evidence -- the whole run through compact2.
        lane = _lane_chunk_family_evidence(sse_log, sid, turn5_wall, compact2_wall)
        dump_json(out / "lane_evidence.json", lane)
        verdict["lane_chunk_family_evidence"] = lane
        if lane["audit_rows_found"] == 0:
            evidence_gaps.append(
                "lane_chunk_family_evidence: zero store.put/segments audit rows matched "
                f"'{sid}__{MESSAGE_PART_LANE_INFIX}' in sse.log over the whole run."
            )
        else:
            checks["lane_whole_run_multiple_chunks"] = bool(
                lane["verified_whole_run_multiple_chunks"]
            )
            checks["lane_puts_never_reamplify_a_sealed_chunk"] = bool(
                lane["verified_no_sealed_chunk_reamplified"]
            )

        # Stop the liveness apparatus BEFORE the restart (dropped connections are not stalls).
        health.stop.set()
        health.join(timeout=15)
        loop_probe.stop.set()
        loop_probe.join(timeout=15)
        sse.stop.set()
        checks["loop_live_whole_run_pre_restart"] = (
            loop_probe.samples > 0
            and loop_probe.failures == 0
            and loop_probe.max_latency_s < max_health_latency_s
        )
        verdict["loop_probe"] = {
            "route": loop_probe._path,
            "samples": loop_probe.samples,
            "failures": loop_probe.failures,
            "max_latency_overall_s": round(loop_probe.max_latency_s, 3),
            "slow_samples": loop_probe.slow(),
        }
        store_writes = _store_writes_on_loop(sse_log)
        verdict["store_writes_on_loop"] = store_writes
        checks["no_store_writes_on_loop"] = store_writes == 0
        verdict["sse"] = {
            "events": sse.events,
            "max_gap_s": round(sse.max_gap_s, 1),
            "error": sse.error,
            "compaction_frames": len(sse.compaction_frames),
        }

        # 3. Restart the app process (the private CTE daemon stays); re-read.
        terminate_server(proc)
        proc = boot_server(port, cwd=ws_root, sse_log=sse_log, extra_env=extra_env)
        restarted_ok = bool(wait_health(call))
        checks["app_restarted_and_became_healthy"] = restarted_ok
        if not restarted_ok:
            raise RuntimeError("server never became healthy after the restart")
        messages_after_restart = _messages_chronological(call, sid)
        dump_json(out / "messages_after_restart.json", {"chronological": messages_after_restart})
        checks.update(check_restart(messages_after_compact2, messages_after_restart))

        # 4. Lossless: the user asks for the originals back.
        verdict["provider_ready_after_restart"] = bind_provider(
            call, provider=provider, model=model
        )
        allow_all(call)
        post_message(call, sid, RECALL_PROMPT)
        status_recall = wait_turn(call, wsid, sid, max_elapsed=300.0)
        verdict["turn_statuses"].append(status_recall)
        checks["recall_turn_completed"] = status_recall in ("idle", "completed")
        messages_after_recall = _messages_chronological(call, sid)
        dump_json(out / "messages_after_recall.json", {"chronological": messages_after_recall})
        segments_before = {
            str(seg.get("id")): seg for st in (before1, before2) for seg in working_set(st)
        }
        recall_checks, recall_evidence = check_recall(
            messages_after_recall[len(messages_after_restart) :],
            [entry1, entry2],
            {
                str(entry1.get("compaction_id")): expected1,
                str(entry2.get("compaction_id")): expected2,
            },
            segments_before,
        )
        checks.update(recall_checks)
        verdict["recall"] = recall_evidence

        verdict["checks"] = checks
        verdict["evidence_gaps"] = evidence_gaps
        verdict["pass"] = all(checks.values())
    except Exception as exc:  # noqa: BLE001 - the verdict carries the failure
        verdict["error"] = f"{type(exc).__name__}: {exc}"
        verdict["checks"] = checks
        verdict["evidence_gaps"] = evidence_gaps
        verdict["pass"] = False
    finally:
        health.stop.set()
        if loop_probe is not None:
            loop_probe.stop.set()
        if sse is not None:
            sse.stop.set()
        terminate_server(proc)
    return verdict


def main() -> int:
    """CLI entry: run one provider/model leg and write the verdict."""

    ap = argparse.ArgumentParser(description="Compaction visible + lossless live leg (11b)")
    ap.add_argument("--provider", default="codex")
    ap.add_argument("--model", default="gpt-5.6-luna")
    ap.add_argument("--max-health-latency", type=float, default=1.0)
    ap.add_argument("--chunk-segments", type=int, default=8)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or (OUT_ROOT / f"compaction-{args.provider}-{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    verdict = run_leg(
        args.provider,
        args.model,
        out,
        max_health_latency_s=args.max_health_latency,
        chunk_segments=args.chunk_segments,
    )
    write_verdict(out / "verdict.json", verdict)
    print(json.dumps(verdict, indent=2, default=str))
    print(f"[leg_compaction] {'PASS' if verdict.get('pass') else 'FAIL'} evidence={out}")
    return 0 if verdict.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
