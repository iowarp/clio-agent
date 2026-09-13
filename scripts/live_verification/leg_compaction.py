"""Live leg: the compaction CHECKPOINT lands as an APPEND, not a ledger replace (#1339).

Reconciled against the CODE AS LANDED on ``fix/1339-compaction-checkpoint`` (HEAD
``911b0aac``) -- not the plan, not the earlier ``fix/1333`` draft this file started
from. Every assumption below was checked by reading the landed modules; three of the
earlier draft's assumptions turned out to be materially wrong and are fixed here:

1. **Response / event shapes are landed, not speculative.** ``compact_session_context``
   (``gact/compaction.py``) returns ``{session_id, compacted, event_id, archived_count,
   summary, checkpoint_placement}`` on success; ``checkpoint_placement`` is
   ``"appended"`` for every compact this leg drives (no turn minter is open when either
   ``POST /compact`` call lands -- both fire strictly between turns). The
   ``session.compacted`` SSE payload is ``{event_id, archived_count, summary_chars,
   summary_message_id, version, trigger}`` (``append_checkpoint``) -- ``trigger`` is
   real, landed surface now, so it is folded into the required-keys superset check
   below rather than recorded-but-not-gated.

2. **``GET /v1/sessions/{sid}/messages`` is NEWEST-FIRST, not chronological.** Read
   ``gact/routes/messages.py::list_messages``: "We store chronologically so reverse at
   read time" -- the wire response is newest-first by design (SPEC / TUI contract).
   The earlier draft indexed ``messages_after_compact1[-1]`` for the checkpoint and
   compared ``[m.id for m in messages_after_turns]`` directly against
   ``compacted_message_ids`` (which is chronological, built from
   ``model_context_messages`` in original ledger order) -- both would have been wrong
   against the real API. Every call site here goes through
   :func:`_messages_chronological`, which reverses the wire response back to
   chronological order before any indexing/slicing; the per-session ledger FILE read
   by :func:`_read_ledger_file` was already chronological (``MessageStore.append``
   loads-appends-flushes in order) and needed no fix.

3. **``Part.to_wire()`` uses ``exclude_defaults``, not just ``exclude_none``.** A
   manual checkpoint's ``auto`` field is ``False`` -- the field's own default -- so it
   is DROPPED from the wire dict entirely (``gact/parts.py::Part.to_wire``), not sent
   as ``"auto": false``. ``checkpoint_part.get("auto") is False`` would fail on a
   correctly-landed manual checkpoint. Fixed to ``checkpoint_part.get("auto", False)
   is False``, which is correct whether the key is omitted (manual, expected) or
   present-and-false.

4. **The amplification-bound / lane-chunking claim now has a REAL evidence source**,
   landed in THIS SAME branch, commit ``911b0aac`` ("test(arc): audit segment puts for
   the live lane proof (#1339)"): ``LocalFSStore.put`` / ``ClioCoreStore.put``
   (``arc/storage.py``) emit ``stream_audit("store.put", kind="segments", name=...,
   size=...)`` for every ``kind == "segments"`` put. The earlier draft grepped for a
   ``scope`` field and an ``_events/m``-prefixed audit row that does not exist; the
   real row carries ``name`` (a record name, not a scope), already proven by
   ``tests/test_arc/test_storage_companion.py::
   test_one_append_on_a_full_three_chunk_lane_puts_only_the_active_chunk``. That test
   also pins the exact naming grammar this leg relies on:
   ``SegmentStore._record_name(session_id, scope) ==
   f"{session_id}{'__'}{scope.replace('/', '~')}"`` (``arc/segments.py`` +
   ``arc/companion_policy.py::SEGMENT_NAME_SEP == "__"``), and chunk scopes are
   ``"_events/m"`` (chunk 1, bare) / ``"_events/m/2"``, ``"_events/m/3"``, ...
   (``arc/lane_chunking.py::chunk_scope``) -- so on the wire a chunk-1 record name
   contains the literal substring ``"_events~m"`` and a chunk-N (N>=2) record name
   contains ``"_events~m~N"``. :func:`_lane_chunk_family_evidence` matches on this
   ``name`` substring (scoped to this session's own ``"<sid>__"`` prefix) instead of
   guessing at a ``scope`` field, and asserts two real, gated claims instead of a
   ``verified: None`` gap:

   * over the WHOLE RUN, more than one distinct chunk record name was ``store.put``
     (the lane actually rolled over -- proves ``CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS``
     took effect);
   * over the WHOLE RUN's chronological ``store.put`` sequence for this lane, the
     touched chunk index is monotonically non-decreasing -- i.e. once a put for
     chunk N+1 is observed, no LATER put ever re-targets chunk N or earlier (the
     real no-sibling-amplification claim the storage-companion test proves at the
     store layer for one append; this leg proves it end-to-end across a live run).

   An EARLIER version of this check instead asserted "the LAST turn's puts touch
   only ONE record, the highest chunk" -- a live codex run disproved that: turn 5's
   own atoms landed 3 puts in chunk 3 then rolled into chunk 4 (a turn can
   legitimately straddle a chunk boundary mid-turn), which is NOT a violation of
   the real invariant above. That version was a leg bug (an over-strict, wrong
   claim), not a backend defect -- fixed to the monotonic-sequence check; the
   per-turn chunk names are still recorded as descriptive (non-gating) evidence.

   With ``CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS=8``, a live turn mints more than the
   naive "one atom per part" count (real provider turns carry tool-call/thinking
   parts too, and the eager per-turn minter's sealed-part-atom profile is
   independent of the bulk ``build_message_part_atoms`` path) -- a live codex run
   produced 27 lane puts across 4 chunks in ~5 turns, confirming the rollover is
   comfortably exercised, not marginal.

Assumptions that were already correct and are carried over unchanged:

* Row-count checks stay DELTA-based (``before_count + known_delta == after_count``),
  never hardcoded literals -- the brief's literal 8/9/13 are recorded as
  ``verdict["brief_literal_*"]`` evidence only, never a gate, because per-turn row
  count is a real unknown this leg does not hardcode.
* No harness primitive exists to restart the app mid-run; reused ``boot_server`` /
  ``terminate_server`` a second time, private CTE daemon untouched (owned by the outer
  ``run_with_private_cte.py`` process).
* No distinct "exit code quirk" mechanism exists in this package beyond
  ``return 0 if verdict["pass"] else 1``; ``main()`` uses that plain pattern.
* Auto-trigger compaction stays out of scope (owner ruling): only the manual
  ``POST /compact`` route is driven.

Run under the private real-CTE daemon (a gate never holds ARC-local)::

    uv run --no-sync python scripts/live_verification/run_with_private_cte.py \\
        scripts/live_verification/leg_compaction.py --provider codex

    uv run --no-sync python scripts/live_verification/run_with_private_cte.py \\
        scripts/live_verification/leg_compaction.py --provider claude_code --model sonnet
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import socket
import sys
import threading
import time
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
    post_message,
    session_messages,
    terminate_server,
    wait_health,
    wait_turn,
    write_verdict,
)

#: The env var #1339 adds to force small chunks on the message-part atom family for
#: the whole run, so the lane-rollover evidence is exercisable within 5 turns instead
#: of needing hundreds (``gact/part_atoms.py::_MESSAGE_PART_CHUNK_ENV``, resolved
#: fresh on every append -- never cached -- so setting it in the app's env before boot
#: is sufficient; no reconfiguration call is needed).
CHUNK_SEGMENTS_ENV = "CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS"

#: The literal substring a message-part-lane record name carries after the '/' -> '~'
#: substitution ``SegmentStore._record_name`` applies to ``gact/part_atoms.py::
#: MESSAGE_PART_SCOPE`` ("_events/m") and its chunk children ("_events/m/2", ...,
#: via ``arc/lane_chunking.py::chunk_scope``). A record name is
#: ``f"{session_id}__{scope-with-tildes}"``; chunk 1 is "<sid>___events~m" (bare
#: scope), chunk N>=2 is "<sid>___events~m~N". Verified against
#: tests/test_arc/test_storage_companion.py::
#: test_one_append_on_a_full_three_chunk_lane_puts_only_the_active_chunk.
MESSAGE_PART_LANE_INFIX = "_events~m"

TURN_PROMPT = "Reply with exactly the single word ack-{n} and nothing else."

#: ``session.compacted`` payload keys (``gact/compaction.py::append_checkpoint``) --
#: ``trigger`` is landed surface on this branch (#1339), not new/unverified.
SESSION_COMPACTED_EXPECTED_KEYS = {
    "event_id",
    "archived_count",
    "summary_chars",
    "summary_message_id",
    "version",
    "trigger",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _HealthProbe(threading.Thread):
    """Poll one GET every 250 ms and keep every latency, wallclock-stamped.

    Copied unchanged (in mechanism) from ``leg_goal_judge.py`` (#1333/#1334) per the
    brief's "carry over ... unchanged" instruction -- this package has no shared home
    for it in ``_common.py``, and no other leg imports it cross-file either.
    """

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


class _SseGapProbe(threading.Thread):
    """Read the session SSE stream; record the max gap between any two events.

    Copied unchanged (in mechanism) from ``leg_goal_judge.py`` -- see :class:`_HealthProbe`.
    """

    def __init__(self, base: str, sid: str) -> None:
        super().__init__(name="sse-probe", daemon=True)
        self._url = f"{base}/v1/sessions/{sid}/events"
        self.stop = threading.Event()
        self.max_gap_s = 0.0
        self.events = 0
        self.error = ""

    def run(self) -> None:
        import requests

        last = time.monotonic()
        try:
            with requests.get(self._url, stream=True, timeout=(10, 60)) as r:
                for line in r.iter_lines():
                    if self.stop.is_set():
                        break
                    if not line or not line.startswith(b"event:"):
                        continue
                    now = time.monotonic()
                    self.max_gap_s = max(self.max_gap_s, now - last)
                    last = now
                    self.events += 1
        except Exception as exc:  # noqa: BLE001 - recorded, not asserted
            self.error = f"{type(exc).__name__}: {exc}"


def _store_writes_on_loop(sse_log: Path) -> int:
    """Count refused server-loop store writes (#1334), unchanged from ``leg_goal_judge.py``."""

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
    """``sse.write`` audit rows (``gact/routes/misc.py``) for ``event_type`` at/after ``after_ts``.

    The audit row carries ``payload_keys`` (``sorted(event.payload.keys())``), not the
    full payload -- enough to assert an event of the right type/shape rode the bus, not
    to assert the values inside it (those come from the HTTP responses instead).
    """

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
        ts = float(row.get("ts") or 0.0)
        if ts >= after_ts:
            rows.append(row)
    return rows


def _read_ledger_file(state_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """The per-session ledger file ``gact/messages.py::MessageStore`` persists to disk.

    Chronological (oldest-first): ``MessageStore.append`` loads the existing file,
    appends, and flushes -- no reversal, unlike the ``GET /messages`` wire response.
    Mirrors ``MessageStore._session_file``'s sanitizing exactly (alnum + ``._-``); a
    ``sess_*`` id never needs it, but matching the real function avoids a silent path
    mismatch if that ever changes.
    """

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
    """``GET /messages`` in chronological (oldest-first) order.

    The wire response is newest-first by design (``gact/routes/messages.py::
    list_messages``: "We store chronologically so reverse at read time" -- the SPEC
    §6.4 / TUI contract). Every internal check in this leg (checkpoint = last row,
    ``compacted_message_ids`` order, "rows appended since compact1" slicing) is
    written against chronological order to match how the ledger and
    ``model_context_messages`` both order things, so every call site reverses here
    rather than re-deriving the fix ad hoc.
    """

    return list(reversed(session_messages(call, sid)))


def _lane_chunk_family_evidence(
    sse_log: Path,
    sid: str,
    last_turn_start: float,
    last_turn_end: float,
) -> dict[str, Any]:
    """Real evidence for the #1339 lane-rollover / no-sibling-amplification claim.

    Reads ``store.put`` audit rows (``arc/storage.py``, landed on this branch,
    commit ``911b0aac``) with ``kind == "segments"`` whose ``name`` carries this
    session's message-part lane infix (:data:`MESSAGE_PART_LANE_INFIX`, scoped to
    ``"<sid>__"`` so a sibling session's rows in a shared log can never leak in).

    The real claim the storage-companion test proves at the store layer
    (``test_one_append_on_a_full_three_chunk_lane_puts_only_the_active_chunk``) is
    narrower than "one turn only ever touches one chunk record": a live turn can
    legitimately straddle a chunk rollover mid-turn (its own atoms fill out the
    tail of chunk N and start chunk N+1) -- an earlier version of this leg's
    ``last_turn``-scoped check asserted the stronger, wrong claim and failed on a
    real codex run for exactly that reason (turn 5's atoms landed 3 puts in chunk 3
    then rolled into chunk 4, evidence at
    ``out/live-verification/compaction-codex-20260911T094203Z/sse.log``). The claim
    that actually holds, globally, is: ``chunk_for_append`` only ever advances its
    cursor forward (``arc/lane_chunking.py``), so once a put for chunk N+1 is
    observed, NO LATER put may ever re-target chunk N or earlier (that would mean a
    sealed sibling got re-encoded/amplified). This is checked over the WHOLE RUN's
    chronological put sequence, not scoped to one turn.

    Returns a dict with ``verified_whole_run_multiple_chunks`` /
    ``verified_no_sealed_chunk_reamplified`` set to ``True``/``False`` when
    evidence exists, or ``None`` when the run produced literally zero matching rows
    (an anomaly worth flagging, not silently passing). ``last_turn_*`` fields are
    recorded as descriptive evidence only (which chunk(s) turn 5's own atoms
    touched) -- never gated on being a single chunk.
    """

    prefix = f"{sid}__"
    result: dict[str, Any] = {
        "evidence_source": "arc/storage.py store.put audit rows, kind=='segments' (#1339, commit 911b0aac)",
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

    whole_run: dict[str, int] = {}  # name -> chunk index
    last_turn_names: set[str] = set()
    sequence: list[tuple[float, int]] = []  # (ts, chunk index), append order
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


def run_leg(
    provider: str,
    model: str,
    out: Path,
    *,
    max_health_latency_s: float = 1.0,
    chunk_segments: int = 8,
) -> dict[str, Any]:
    """Boot an isolated server, drive the checkpoint scenario, judge the evidence."""

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
        # Pin the ambient boot-config default to THIS leg's provider (config.py::
        # load_config_from_env defaults CLIO_LM_PROVIDER to "lm_studio" when unset).
        # The server's own deferred agent-init AND the first PUT /v1/providers/lm's
        # throwaway construct_agent_with_relay() scaffold (immediately overwritten by
        # rebind_lms(cfg), gact/routes/providers.py::_apply_lm_provider) both read
        # this ambient default, not our explicit bind -- so an operator box without a
        # live LM Studio instance at 127.0.0.1:1234 fails the FIRST bind of ANY
        # provider with an unrelated LMStudioDiscoveryError. Pinning it here makes
        # the leg hermetic (matches the "config over env vars" / no-ambient-shell-
        # dependency house rule) instead of silently depending on whatever the
        # operator's shell happens to have loaded.
        "CLIO_LM_PROVIDER": provider,
    }
    proc = boot_server(port, cwd=ws_root, sse_log=sse_log, extra_env=extra_env)
    call = client(base)
    verdict: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "port": port,
        "chunk_segments_env": chunk_segments,
        "turn_statuses": [],
    }
    checks: dict[str, bool] = {}
    evidence_gaps: list[str] = []
    health = _HealthProbe(base)
    loop_probe: _HealthProbe | None = None
    sse: _SseGapProbe | None = None
    try:
        if not wait_health(call):
            raise RuntimeError("server never became healthy")
        verdict["provider_ready"] = bind_provider(call, provider=provider, model=model)
        allow_all(call)  # MUST run before the first turn (house rule, #1274/#1275)
        wsid = create_workspace(call, "compaction-1339", ws_root)
        sid = create_session(call, wsid, "compaction-checkpoint-1339")
        verdict["session_id"] = sid

        sse = _SseGapProbe(base, sid)
        sse.start()
        health.start()
        loop_probe = _HealthProbe(base, f"/v1/sessions/{sid}", "loop")
        loop_probe.start()

        # ------------------------------------------------------------------ #
        # 1. Four short turns, then the FIRST manual compact.
        # ------------------------------------------------------------------ #
        for n in range(1, 5):
            post_message(call, sid, TURN_PROMPT.format(n=n))
            status = wait_turn(call, wsid, sid, max_elapsed=180.0)
            verdict["turn_statuses"].append(status)
        checks["turns_1_4_completed"] = all(
            s in ("idle", "completed") for s in verdict["turn_statuses"][:4]
        )

        messages_after_turns = _messages_chronological(call, sid)
        rows_after_turns = len(messages_after_turns)
        pre_compact1_ids = [str(m.get("id") or "") for m in messages_after_turns]
        verdict["brief_literal_8_rows_before_compact1"] = rows_after_turns == 8

        compact1_wall = time.time()
        compact1_resp = call("POST", f"/v1/sessions/{sid}/compact", {})
        checks["compact1_response_compacted_true"] = bool(compact1_resp.get("compacted"))
        checks["compact1_archived_count_matches_pre_rows"] = (
            int(compact1_resp.get("archived_count", -1)) == rows_after_turns
        )
        checks["compact1_has_event_id"] = bool(compact1_resp.get("event_id"))
        # Landed surface (#1339, gact/compaction.py::PLACEMENT_APPENDED): both compacts
        # this leg drives happen strictly between turns (no open turn minter), so the
        # checkpoint is always landed via append_checkpoint, never staged.
        verdict["compact1_checkpoint_placement"] = compact1_resp.get("checkpoint_placement")
        checks["compact1_checkpoint_placement_appended"] = (
            compact1_resp.get("checkpoint_placement") == "appended"
        )

        messages_after_compact1 = _messages_chronological(call, sid)
        dump_json(
            out / "messages_after_compact1.json",
            {
                "chronological": messages_after_compact1,
                "wire_newest_first": session_messages(call, sid),
            },
        )
        rows_after_compact1 = len(messages_after_compact1)
        checks["compact1_grows_ledger_by_exactly_one"] = rows_after_compact1 == rows_after_turns + 1
        verdict["brief_literal_9_rows_after_compact1"] = rows_after_compact1 == 9
        checks["compact1_all_prior_rows_still_present"] = set(pre_compact1_ids) <= {
            str(m.get("id") or "") for m in messages_after_compact1
        }

        checkpoint1 = messages_after_compact1[-1] if messages_after_compact1 else {}
        checkpoint1_id = str(checkpoint1.get("id") or "")
        checkpoint1_parts = checkpoint1.get("parts") or []
        checkpoint1_part = checkpoint1_parts[0] if checkpoint1_parts else {}
        checks["checkpoint1_single_part"] = len(checkpoint1_parts) == 1
        checks["checkpoint1_part_type_compaction"] = checkpoint1_part.get("type") == "compaction"
        # Part.to_wire() uses exclude_defaults (gact/parts.py): auto's default IS
        # False, so a manual checkpoint's "auto" key is DROPPED from the wire dict,
        # not sent as false. .get("auto", False) is correct whether the key is
        # omitted (expected here) or present-and-false.
        checks["checkpoint1_auto_false"] = checkpoint1_part.get("auto", False) is False
        checks["checkpoint1_has_summary"] = bool(str(checkpoint1_part.get("summary") or ""))
        checkpoint1_ids = [str(x) for x in (checkpoint1_part.get("compacted_message_ids") or [])]
        checks["checkpoint1_compacted_message_ids_match_pre_ids"] = (
            checkpoint1_ids == pre_compact1_ids
        )

        ledger_file_1 = _read_ledger_file(state, sid)
        checks["ledger_file_row_count_matches_api_after_compact1"] = (
            len(ledger_file_1) == rows_after_compact1
        )

        sse_message_created_1 = _sse_rows_since(sse_log, compact1_wall, "message.created")
        sse_session_compacted_1 = _sse_rows_since(sse_log, compact1_wall, "session.compacted")
        checks["sse_message_created_seen_after_compact1"] = len(sse_message_created_1) > 0
        checks["sse_session_compacted_seen_after_compact1"] = len(sse_session_compacted_1) > 0
        if sse_session_compacted_1:
            keys1 = set(sse_session_compacted_1[-1].get("payload_keys") or [])
            checks["session_compacted1_payload_keys_superset"] = (
                SESSION_COMPACTED_EXPECTED_KEYS <= keys1
            )
        else:
            checks["session_compacted1_payload_keys_superset"] = False

        # ------------------------------------------------------------------ #
        # 2. One more turn; context-frame evidence; the SECOND manual compact.
        # ------------------------------------------------------------------ #
        turn5_wall = time.time()
        post_message(call, sid, TURN_PROMPT.format(n=5))
        status5 = wait_turn(call, wsid, sid, max_elapsed=180.0)
        verdict["turn_statuses"].append(status5)
        checks["turn_five_completed"] = status5 in ("idle", "completed")

        frames = call("GET", f"/v1/sessions/{sid}/context/frames").get("frames", [])
        dump_json(out / "context_frames.json", frames)
        latest_frame = frames[-1] if frames else {}
        frame_items = latest_frame.get("items") or []
        checks["context_frame_found_after_turn_five"] = bool(latest_frame)

        pre_ids_set = set(pre_compact1_ids)
        compacted_items = [it for it in frame_items if it.get("source_id") in pre_ids_set]
        checks["context_frame_marks_all_eight_archived_rows"] = len(compacted_items) == len(
            pre_compact1_ids
        )
        checks["context_frame_archived_rows_excluded_with_reason"] = bool(compacted_items) and all(
            it.get("included") is False and it.get("reason") == "compacted"
            for it in compacted_items
        )
        checkpoint_item = next(
            (it for it in frame_items if it.get("source_id") == checkpoint1_id), {}
        )
        checks["context_frame_marks_checkpoint_visible"] = (
            bool(checkpoint_item)
            and checkpoint_item.get("included") is True
            and checkpoint_item.get("reason") == "visible_transcript"
        )

        messages_after_turn5 = _messages_chronological(call, sid)
        rows_after_turn5 = len(messages_after_turn5)
        new_since_compact1 = [
            str(m.get("id") or "") for m in messages_after_turn5[rows_after_compact1:]
        ]
        expected_checkpoint2_ids = [checkpoint1_id, *new_since_compact1]

        compact2_wall = time.time()
        compact2_resp = call("POST", f"/v1/sessions/{sid}/compact", {})
        checks["compact2_response_compacted_true"] = bool(compact2_resp.get("compacted"))
        checks["compact2_has_event_id"] = bool(compact2_resp.get("event_id"))
        checks["compact2_checkpoint_placement_appended"] = (
            compact2_resp.get("checkpoint_placement") == "appended"
        )
        checks["compact2_archived_count_matches_expected"] = int(
            compact2_resp.get("archived_count", -1)
        ) == len(expected_checkpoint2_ids)

        messages_after_compact2 = _messages_chronological(call, sid)
        dump_json(
            out / "messages_after_compact2.json",
            {
                "chronological": messages_after_compact2,
                "wire_newest_first": session_messages(call, sid),
            },
        )
        rows_after_compact2 = len(messages_after_compact2)
        checks["compact2_grows_ledger_by_exactly_one"] = rows_after_compact2 == rows_after_turn5 + 1
        # Brief literal check (see module docstring -- the arithmetic gap): recorded
        # as evidence, never gates ``pass`` on its own (per-turn row count is not
        # hardcoded anywhere else in this leg either).
        verdict["brief_literal_13_rows_after_compact2"] = rows_after_compact2 == 13
        verdict["delta_based_expected_rows_after_compact2"] = rows_after_turn5 + 1

        checkpoint2 = messages_after_compact2[-1] if messages_after_compact2 else {}
        checkpoint2_part = (checkpoint2.get("parts") or [{}])[0]
        checks["checkpoint2_auto_false"] = checkpoint2_part.get("auto", False) is False
        checkpoint2_ids = [str(x) for x in (checkpoint2_part.get("compacted_message_ids") or [])]
        checks["checkpoint2_covers_checkpoint1_user_assistant_only"] = (
            checkpoint2_ids == expected_checkpoint2_ids
        )
        checks["checkpoint2_does_not_recover_first_eight"] = not (
            set(checkpoint2_ids) & pre_ids_set
        )
        checks["checkpoint2_all_prior_rows_still_present"] = {
            str(m.get("id") or "") for m in messages_after_turn5
        } <= {str(m.get("id") or "") for m in messages_after_compact2}

        sse_message_created_2 = _sse_rows_since(sse_log, compact2_wall, "message.created")
        sse_session_compacted_2 = _sse_rows_since(sse_log, compact2_wall, "session.compacted")
        checks["sse_message_created_seen_after_compact2"] = len(sse_message_created_2) > 0
        checks["sse_session_compacted_seen_after_compact2"] = len(sse_session_compacted_2) > 0
        if sse_session_compacted_2:
            keys2 = set(sse_session_compacted_2[-1].get("payload_keys") or [])
            checks["session_compacted2_payload_keys_superset"] = (
                SESSION_COMPACTED_EXPECTED_KEYS <= keys2
            )
        else:
            checks["session_compacted2_payload_keys_superset"] = False

        pre_restart_snapshot = list(messages_after_compact2)

        # ------------------------------------------------------------------ #
        # Lane evidence -- computed from the whole run through the end of
        # compact2 (the last atom-minting operation before the restart).
        # ------------------------------------------------------------------ #
        lane = _lane_chunk_family_evidence(sse_log, sid, turn5_wall, compact2_wall)
        dump_json(out / "lane_evidence.json", lane)
        verdict["lane_chunk_family_evidence"] = lane
        if lane["audit_rows_found"] == 0:
            evidence_gaps.append(
                "lane_chunk_family_evidence: zero store.put/segments audit rows matched "
                f"the '{sid}__{MESSAGE_PART_LANE_INFIX}' naming in sse.log over the whole "
                "run -- either atom minting produced no audit rows this run, or the "
                "naming grammar assumption (see module docstring) no longer holds."
            )
        else:
            checks["lane_whole_run_multiple_chunks"] = bool(
                lane["verified_whole_run_multiple_chunks"]
            )
            checks["lane_puts_never_reamplify_a_sealed_chunk"] = bool(
                lane["verified_no_sealed_chunk_reamplified"]
            )
            if lane["last_turn_rows_found"] == 0:
                evidence_gaps.append(
                    "lane_last_turn_evidence: no store.put/segments rows for this lane fell "
                    "inside the turn-5 window [turn5_wall, compact2_wall) -- turn 5's atoms "
                    "landed entirely outside the sampled window (descriptive evidence only, "
                    "never gated -- see module docstring)."
                )

        # ------------------------------------------------------------------ #
        # Stop the loop-liveness apparatus BEFORE the restart -- a restart
        # necessarily drops the probes' HTTP connections, which would otherwise
        # read as spurious failures rather than a measured stall.
        # ------------------------------------------------------------------ #
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
        if sse is not None:
            verdict["sse"] = {
                "events": sse.events,
                "max_gap_s": round(sse.max_gap_s, 1),
                "error": sse.error,
            }

        # ------------------------------------------------------------------ #
        # 3. Restart the app process (keep the private CTE daemon); re-read.
        # ------------------------------------------------------------------ #
        terminate_server(proc)
        proc = boot_server(port, cwd=ws_root, sse_log=sse_log, extra_env=extra_env)
        restarted_ok = wait_health(call)
        checks["app_restarted_and_became_healthy"] = restarted_ok
        if restarted_ok:
            messages_after_restart = _messages_chronological(call, sid)
            dump_json(
                out / "messages_after_restart.json",
                {
                    "chronological": messages_after_restart,
                    "wire_newest_first": session_messages(call, sid),
                },
            )
            checks["messages_survive_restart_same_count"] = (
                len(messages_after_restart) == rows_after_compact2
            )
            ids_before = [m.get("id") for m in pre_restart_snapshot]
            ids_after = [m.get("id") for m in messages_after_restart]
            checks["messages_survive_restart_same_order"] = ids_before == ids_after
            checks["messages_survive_restart_byte_equal_parts"] = len(pre_restart_snapshot) == len(
                messages_after_restart
            ) and all(
                json.dumps(before.get("parts"), sort_keys=True, default=str)
                == json.dumps(after.get("parts"), sort_keys=True, default=str)
                for before, after in zip(pre_restart_snapshot, messages_after_restart, strict=True)
            )
        else:
            checks["messages_survive_restart_same_count"] = False
            checks["messages_survive_restart_same_order"] = False
            checks["messages_survive_restart_byte_equal_parts"] = False

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
    """CLI entry: run one provider/model leg and write the verdict.

    ``return 0 if pass else 1`` -- the plain pattern every leg in this package's
    ``main()`` already uses; no distinct "exit code quirk" beyond it exists in this
    package.
    """

    ap = argparse.ArgumentParser(description="Compaction CHECKPOINT live leg (#1339)")
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
