"""Per-turn transcript minter: the FIFO that persists a session's transcript atoms off
the loop thread (#1334) and, since #1337, persists each part WHEN IT BECOMES FINAL.

Why: every transcript persist is a synchronous ARC store RPC. Issued from the loop thread
(the user message minted inside ``POST /messages`` before its response, a steer appended
mid-turn) each one froze the server for its duration; and the assistant message used to
be minted as ONE batch at finalize (~30 writes on a 9-tool turn) although every part had
already streamed to the UI as it completed and the event log had recorded each tool call
incrementally. This module owns ONE consumer thread per turn that drains persist jobs in
order, so the loop never waits on the store, the atoms of one session land in append
order (the lane grouping relies on it), and finalize writes only what is not yet sealed
plus the envelope.

Contract:

* :func:`open_turn_minter` / :func:`close_turn_minter` bracket a turn (opened in the
  turn's off-loop prologue, closed when the transcript settles).
* :func:`transcript_sink` is the ``TurnTranscript`` seal hook: non-blocking, it queues
  one sealed part atom (``part_atoms.build_sealed_part_atom``) per part that became
  final; the minted dump is remembered so :meth:`PartAtomMinter.mint_remainder` can tell
  "never sealed" and "sealed then mutated" apart with one comparison.
* :func:`run_transcript_job` is the entry for whole-message deferred persists (the user
  message, a steer, an a2ui part): the open minter takes the job; without one the job is
  dispatched off the loop with a typed audit on failure.
* :func:`persist_finalized_message` is the finalize gate, on the finalize executor: the
  barrier (every queued job landed or its failure re-raised), the remainder (unsealed or
  mutated parts + the envelope atom), then the in-memory ledger + local store append
  through the ``clio_agent.gact.app`` seam (bound at call time so the #714 test
  monkeypatches keep intercepting). The must-succeed contract of
  ``transcript_projection.on_message_appended`` (a failed mint fails the turn, never a
  half-committed transcript) is unchanged, only moved to this boundary.

Deliberately a dedicated thread, not the shared depth-keyed agent-task executor: a slot
parked in ~90 ms RPCs for a whole turn would starve child forwards at the same depth.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from clio_agent.gact.off_loop import schedule_off_loop
from clio_agent.gact.part_atoms import (
    append_part_atom,
    build_envelope_atom,
    build_sealed_part_atom,
    message_stub,
)
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

TRANSCRIPT_JOB_FAILED = "transcript_job_failed"
MINTER_DRAIN_TIMEOUT = "transcript_minter_drain_timeout"
PART_ATOM_SEAL_FAILED = "part_atom_seal_failed"

_REGISTRY_LOCK = threading.Lock()
_STOP = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PartAtomMinter:
    """One FIFO consumer thread that persists transcript jobs for one turn."""

    def __init__(self, *, session_id: str, turn_id: str, arc: Any = None) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.arc = arc
        self.opened_at = _now_iso()
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._failures: list[tuple[str, Callable[[], Any], BaseException]] = []
        self._lock = threading.Lock()
        self._closed = False
        self._pending = 0
        self._idle = threading.Condition(self._lock)
        # #1337: part_id -> the dump that landed on the lane (insertion order = seal order).
        self._minted: dict[str, dict[str, Any]] = {}
        self._minted_index: dict[str, int] = {}
        self._thread = threading.Thread(
            target=self._run, name=f"clio-atom-mint-{session_id}", daemon=True
        )
        self._thread.start()

    # ---- producer side --------------------------------------------------------

    def enqueue(self, label: str, fn: Callable[[], Any]) -> bool:
        """Queue ``fn`` (non-blocking). ``False`` once the minter is closed."""

        with self._lock:
            if self._closed:
                return False
            self._pending += 1
        self._queue.put((label, fn))
        return True

    def seal(
        self, message_id: str, part_dump: dict[str, Any], part_index: int, *, source: str
    ) -> bool:
        """Queue the sealed part atom for a part that just became final (non-blocking).

        ``False`` when nothing was queued (no ARC, or the minter is closed) — the part is
        then minted by :meth:`mint_remainder` at finalize, exactly as before #1337.
        """

        if self.arc is None:
            return False
        part_id = str(part_dump.get("id") or "")
        stub = message_stub(
            message_id=message_id,
            turn_id=self.turn_id,
            session_id=self.session_id,
            created_at=self.opened_at,
        )
        content = build_sealed_part_atom(
            stub, part_dump, part_index, sealed_at=_now_iso(), seal_source=source
        )

        def _mint() -> None:
            append_part_atom(self.arc._segments, self.session_id, content)
            with self._lock:
                self._minted[part_id] = part_dump
                self._minted_index[part_id] = part_index

        return self.enqueue(f"seal:{part_id}", _mint)

    # ---- consumer side --------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            label, fn = item
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - recorded; the barrier re-raises
                stream_audit(
                    "transcript.job_failed",
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    label=label,
                    reason=PART_ATOM_SEAL_FAILED
                    if label.startswith("seal:")
                    else TRANSCRIPT_JOB_FAILED,
                    error=type(exc).__name__,
                    message=str(exc)[:300],
                )
                logger.error(
                    "transcript job %s failed for session=%s; the finalize barrier retries",
                    label,
                    self.session_id,
                    exc_info=True,
                )
                with self._lock:
                    self._failures.append((label, fn, exc))
            finally:
                with self._idle:
                    self._pending -= 1
                    if self._pending == 0:
                        self._idle.notify_all()

    # ---- finalize side --------------------------------------------------------

    def minted(self) -> dict[str, dict[str, Any]]:
        """``part_id -> dump`` of every part atom that landed, in seal order."""

        with self._lock:
            return dict(self._minted)

    def minted_in_order(self) -> list[dict[str, Any]]:
        """The landed part dumps sorted by their ledger index."""

        with self._lock:
            order = self._minted_index
            return [self._minted[pid] for pid in sorted(self._minted, key=lambda p: order[p])]

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait until every queued job ran. ``False`` on timeout (audited)."""

        with self._idle:
            done = self._idle.wait_for(lambda: self._pending == 0, timeout=timeout)
        if not done:
            stream_audit(
                "transcript.minter_drain_timeout",
                session_id=self.session_id,
                turn_id=self.turn_id,
                pending=self._pending,
                reason=MINTER_DRAIN_TIMEOUT,
            )
            logger.error(
                "transcript minter drain timed out session=%s pending=%d (%s)",
                self.session_id,
                self._pending,
                MINTER_DRAIN_TIMEOUT,
            )
        return done

    def barrier(self, *, timeout: float = 5.0) -> None:
        """Drain, then re-run every failed job inline; a job that fails again raises.

        Called on the finalize executor thread, right before the assistant message is
        persisted, so the turn's transcript is complete-or-failed at one boundary. A
        seal that failed is dropped here (not re-run): :meth:`mint_remainder` mints the
        part fresh from the final message, which is the same atom.
        """

        self.drain(timeout=timeout)
        with self._lock:
            failed = list(self._failures)
            self._failures.clear()
        for label, fn, first_exc in failed:
            if label.startswith("seal:"):
                continue  # the remainder mints it from the final message
            logger.warning(
                "transcript barrier: retrying failed job %s (first error: %s)",
                label,
                type(first_exc).__name__,
            )
            fn()  # raises through the finalize envelope when it fails again

    def mint_remainder(self, message: Any) -> int:
        """Mint what the seals did not: unsealed or since-mutated parts, then the envelope.

        Returns the number of atoms written. Runs synchronously on the caller's thread
        (the finalize executor); any failure raises so the turn fails typed.
        """

        if self.arc is None:
            return 0
        self.barrier()
        stub = message_stub(
            message_id=message.id,
            turn_id=self.turn_id,
            session_id=self.session_id,
            created_at=self.opened_at,
        )
        written = 0
        landed = self.minted()
        for index, part in enumerate(message.parts):
            dump = part.model_dump()
            if landed.get(str(part.id or "")) == dump:
                continue
            content = build_sealed_part_atom(
                stub, dump, index, sealed_at=_now_iso(), seal_source="finalize"
            )
            append_part_atom(self.arc._segments, self.session_id, content)
            written += 1
        append_part_atom(self.arc._segments, self.session_id, build_envelope_atom(message))
        return written + 1

    def close(self, *, timeout: float = 5.0) -> None:
        """Stop accepting jobs, drain what is queued, stop the thread."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.drain(timeout=timeout)
        self._queue.put(_STOP)
        self._thread.join(timeout=timeout)
        with self._lock:
            leftover = list(self._failures)
        if leftover:
            logger.error(
                "transcript minter closed with %d unrecovered job(s) session=%s labels=%s",
                len(leftover),
                self.session_id,
                [label for label, _fn, _exc in leftover],
            )


# ---- registry on app.state ----------------------------------------------------------


def _registry(app: Any) -> dict[str, PartAtomMinter]:
    with _REGISTRY_LOCK:
        reg = getattr(app.state, "turn_minters", None)
        if reg is None:
            reg = {}
            app.state.turn_minters = reg
        return reg


def open_turn_minter(app: Any, session_id: str, turn_id: str) -> PartAtomMinter:
    """Open (or replace) the session's minter for this turn."""

    minter = PartAtomMinter(
        session_id=session_id, turn_id=turn_id, arc=getattr(app.state, "arc", None)
    )
    with _REGISTRY_LOCK:
        reg = getattr(app.state, "turn_minters", None)
        if reg is None:
            reg = {}
            app.state.turn_minters = reg
        previous = reg.get(session_id)
        reg[session_id] = minter
    if previous is not None:
        previous.close()
    return minter


def turn_minter(app: Any, session_id: str) -> Optional[PartAtomMinter]:
    """The session's open minter, or ``None``."""

    return _registry(app).get(session_id)


def close_turn_minter(app: Any, session_id: str) -> None:
    """Close and drop the session's minter (no-op when none is open)."""

    with _REGISTRY_LOCK:
        reg = getattr(app.state, "turn_minters", None)
        minter = reg.pop(session_id, None) if reg is not None else None
    if minter is not None:
        minter.close()


def transcript_sink(app: Any, session_id: str) -> Callable[[str, dict[str, Any], int, str], None]:
    """The ``TurnTranscript`` seal hook for ``session_id`` (resolves the minter per call)."""

    def _sink(message_id: str, part_dump: dict[str, Any], part_index: int, source: str) -> None:
        minter = turn_minter(app, session_id)
        if minter is not None:
            minter.seal(message_id, part_dump, part_index, source=source)

    return _sink


def run_transcript_job(app: Any, session_id: str, label: str, fn: Callable[[], Any]) -> None:
    """Persist a transcript job off the loop thread.

    Order of preference: the session's open minter (FIFO, covered by the finalize
    barrier); else :func:`schedule_off_loop` (inline without a loop on this thread,
    otherwise dispatched with a typed audit on failure).
    """

    minter = turn_minter(app, session_id)
    if minter is not None and minter.enqueue(label, fn):
        return
    schedule_off_loop(fn, label=label)


def persist_finalized_message(app: Any, session_id: str, message: Any) -> None:
    """Finalize's persist (on the finalize executor): barrier, remainder, then the append.

    With an open minter that has an ARC, the atoms are minted here (the eager profile:
    sealed parts already landed, the remainder + envelope now) and the append skips its
    own mint; without one the append mints the inline profile as before.
    """

    from clio_agent.gact.app import _append_session_message  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415
        record_state_merge_best_effort,
    )

    minter = turn_minter(app, session_id)
    if minter is None or minter.arc is None:
        if minter is not None:
            minter.barrier()
        _append_session_message(app, session_id, message)
        return
    minter.mint_remainder(message)
    _append_session_message(app, session_id, message, atoms_minted=True)
    record_state_merge_best_effort(minter.arc, session_id, message)


def failed_finalize_identity(app: Any, session_id: str) -> tuple[str, list[Any]]:
    """The identity the failed-finalize envelope reuses: the open transcript's message
    id and its FULL current parts — sealed AND unsealed — so a crashed turn's streamed
    parts survive reload under a typed error envelope instead of an empty message.

    Not just the sealed subset: a crash can land after a part was appended to the
    ledger but before it was ever eligible to seal (the FieldStream batch fallback
    text is never sealed live — only ``mint_remainder`` at a clean finalize mints it),
    so ``minter.minted_in_order()`` alone would drop it. ``transcript.snapshot()`` is
    the full ledger instead; its parts are sequence-stamped here exactly as
    ``finalize()`` would (idempotent for the already-sealed ones — their index never
    shifts). Reads only: never calls ``transcript.finalize()`` itself, the failed
    envelope must not publish, and the caller abandons the ledger right after this
    returns.

    ``("", [])`` when no assistant message was ever minted for this turn (the envelope
    mints a fresh id).
    """

    registry = getattr(app.state, "turn_transcripts", None)
    transcript = registry.get(session_id) if registry is not None else None
    if transcript is None or not transcript.message_id:
        return "", []
    parts = transcript.snapshot()
    for index, part in enumerate(parts, start=1):
        part.sequence = index  # idempotent re-stamp, mirrors TurnTranscript.finalize()
    return transcript.message_id, parts


__all__ = [
    "MINTER_DRAIN_TIMEOUT",
    "PART_ATOM_SEAL_FAILED",
    "TRANSCRIPT_JOB_FAILED",
    "PartAtomMinter",
    "close_turn_minter",
    "failed_finalize_identity",
    "open_turn_minter",
    "persist_finalized_message",
    "run_transcript_job",
    "transcript_sink",
    "turn_minter",
]
