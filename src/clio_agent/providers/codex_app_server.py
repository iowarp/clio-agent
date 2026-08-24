"""Native ``codex app-server`` transport for the Codex provider (JSON-RPC/stdio, #896).

Owner module for the *streaming* half of the Codex bridge, carved out of
:mod:`clio_agent.providers.codex_litellm` (which must stay under its file-size
ratchet — #775 no-accretion). The legacy ``codex exec`` batch path and the
opt-in ``openai_codex`` Python-SDK path stay in that module; this one drives the
real programmatic surface.

**Why app-server (not ``codex exec``).** ``codex exec`` is the batch print mode:
it returns the whole answer at once, surfaces no usage on the ``-o`` file, and
never streams token deltas. ``codex app-server`` is the bidirectional JSON-RPC
2.0 service that powers every Codex surface (including the TUI's visible token
streaming). Verified live against ``codex-cli 0.144.1``:

* framing is newline-delimited JSON over stdio (``--listen stdio://`` default);
* lifecycle is ``initialize`` → ``initialized`` (notification) → ``thread/start``
  → ``turn/start``, with server notifications ``thread/started`` → ``turn/started``
  → ``item/agentMessage/delta`` (true incremental text deltas) → ``item/completed``
  → ``thread/tokenUsage/updated`` (live usage) → ``turn/completed``;
* usage arrives as ``{inputTokens, cachedInputTokens, outputTokens,
  reasoningOutputTokens, totalTokens}`` (``cachedInputTokens`` is the automatic
  prefix-cache read — mapped to the cache-read audit column by
  :mod:`clio_agent.providers.codex_audit`);
* reasoning summaries *may* arrive as ``item/reasoning/*Delta`` notifications —
  empirically absent on the subscription backend, so the thinking lane is typed
  absent (never synthesized).

**The warm-process shape (#891) — honest accounting.** One ``codex app-server``
subprocess is kept alive per ``(model, cwd)`` key by :class:`CodexAppServerPool`;
the spawn + ``initialize`` handshake is paid once, not per call — **that
spawn-amortization is the measured win**. In the flag-OFF (default) path each LM
call runs a FRESH ephemeral thread (:meth:`CodexAppServerProcess.run_turn` —
``thread/start`` ... ``ephemeral=True``) so there is no cross-call / cross-expert
context bleed, but codex re-ingests the whole prompt every call (measured ~33%
cache, all spawn-amortization) because the automatic prefix cache is limited by the
~20K prefix codex injects (base instructions + ``AGENTS.md`` + plugins) ahead of
clio's prompt.

**The stateful-delta path (#891 codex slice).** When
:mod:`clio_agent.providers.codex_stateful` engages (flag ON + a ReActV2 scope), a
single PERSISTENT thread (:meth:`start_thread` ``ephemeral=False``) is kept per
expert-forward scope and continued with :meth:`run_turn_on_thread`, whose ``prompt``
is only the NEW appended content (the delta). The persistent thread RETAINS the
conversation server-side, so codex never re-ingests the ~20K injected prefix OR the
prior turns — sidestepping the shared-prefix ceiling entirely. ``effort`` is re-sent
per turn (the schema documents it as "for this turn and subsequent turns"), so the
#895 reasoning knob keeps applying. A per-process turn lock serialises the
thread/turn cycle (bounded wait, typed timeout) so concurrent experts sharing a
process never interleave streams.

**No silent fallback (#775).** A mid-turn transport death or an ``error`` notification surfaces as a typed
:class:`CodexAppServerError`, marks the process dead, and the pool **evicts the
corpse and respawns on the next call** (one respawn attempt per call; a failed
respawn is a typed error, never a hang — the self-healing parity with ``exec``).
Late notifications from an abandoned/completed turn are dropped with a typed
audit reason and can never interleave into the next turn's stream (the drain
filters by ``threadId``). The pool tears every subprocess down on clean shutdown
(``atexit`` + the #900 teardown hook) so no ``codex`` child is orphaned.

v0.8.0 cleanup: the ``app_server_enabled`` kill-switch (CLIO_CODEX_APP_SERVER)
and its ``transport_fallback_payload`` downgrade catalog were deleted along
with the legacy ``codex exec`` batch path they degraded to — app-server is the
only transport, and a broken one is a typed hard error, not a downgrade.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any

from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled

logger = logging.getLogger(__name__)

__all__ = [
    "CodexAppServerError",
    "CodexAppServerPool",
    "CodexAppServerProcess",
    "TurnEvent",
    "normalize_usage",
    "_APP_SERVER_POOL",
    "_reset_app_server_for_tests",
]

#: Codex sandbox for the bare-model transport: read-only keeps the model's
#: shell/fs tools inert so the turn produces only an answer (clio drives tools).
DEFAULT_SANDBOX = "read-only"

#: Reasoning-summary request. ``detailed`` asks codex to surface a reasoning
#: summary when the backend exposes one (it is typed-absent otherwise).
DEFAULT_SUMMARY = "detailed"


class CodexAppServerError(RuntimeError):
    """Raised on an app-server transport failure (spawn, protocol, or turn error)."""


def normalize_usage(last: dict[str, Any] | None) -> dict[str, int]:
    """Normalize a codex ``tokenUsage.last`` breakdown to the audit key names.

    Codex reports ``{inputTokens, cachedInputTokens, outputTokens,
    reasoningOutputTokens, totalTokens}``; ``inputTokens`` already includes the
    cached subset and ``outputTokens`` already includes reasoning. We remap to
    the snake-case keys the waterfall analyzer joins on
    (``scripts/analyze_turn_waterfall.py``): ``cachedInputTokens`` →
    ``cache_read_input_tokens`` (the cache-read column), ``reasoningOutputTokens``
    → ``reasoning_output_tokens``. ``cache_creation_input_tokens`` is always 0 —
    OpenAI prefix caching has no explicit creation step.
    """
    last = last or {}

    def _int(key: str) -> int:
        try:
            return int(last.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "input_tokens": _int("inputTokens"),
        "cache_read_input_tokens": _int("cachedInputTokens"),
        "cache_creation_input_tokens": 0,
        "output_tokens": _int("outputTokens"),
        "reasoning_output_tokens": _int("reasoningOutputTokens"),
        "total_tokens": _int("totalTokens"),
    }


class TurnEvent:
    """One normalized event yielded by :meth:`CodexAppServerProcess.run_turn`.

    ``kind`` is one of ``"text"`` (agent-message delta), ``"reasoning"``
    (provider reasoning-summary delta → thinking lane only), ``"usage"`` (a live
    ``thread/tokenUsage/updated`` snapshot, normalized), or ``"final"`` (terminal:
    carries the accumulated ``text``, final normalized ``usage``, and ``reason``).
    """

    __slots__ = ("kind", "text", "usage", "reason")

    def __init__(
        self,
        kind: str,
        *,
        text: str = "",
        usage: dict[str, int] | None = None,
        reason: str = "stop",
    ) -> None:
        self.kind = kind
        self.text = text
        self.usage = usage or {}
        self.reason = reason


# --------------------------------------------------------------------------- #
# One warm app-server subprocess.
# --------------------------------------------------------------------------- #
class CodexAppServerProcess:
    """A single warm ``codex app-server`` subprocess driven over stdio JSON-RPC.

    Owns the subprocess, a daemon reader thread parsing newline-delimited JSON,
    a request/response correlation table, and a per-process turn lock. Requests
    (``initialize`` / ``thread/start`` / ``turn/start``) block for their result;
    server notifications are routed to the active turn's queue. The ``initialize``
    handshake runs lazily on first :meth:`run_turn`.

    Death is a first-class state: a reader exit (stdout closed), a stdin write
    failure, or a spawn failure marks the process dead with a typed reason.
    :meth:`CodexAppServerPool.process_for` evicts dead entries so the next call
    gets a fresh process (the self-healing parity with the per-call ``exec`` path).
    """

    def __init__(self, *, binary: str, model: str, cwd: str | None) -> None:
        self._binary = binary
        self._model = model
        self._cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()  # guards spawn/teardown + write serialization
        self._turn_lock = threading.Lock()  # serialises thread/turn cycles (bounded wait)
        self._next_id = 0
        self._pending: dict[int, queue.SimpleQueue[dict[str, Any]]] = {}
        self._sink: queue.SimpleQueue[dict[str, Any]] | None = None
        self._initialized = False
        self._dead_reason: str | None = None

    # -- death tracking ------------------------------------------------------ #
    @property
    def dead_reason(self) -> str | None:
        """The typed reason this process died, or ``None`` while healthy."""
        return self._dead_reason

    @property
    def is_dead(self) -> bool:
        """Whether this process can no longer serve turns (pool must evict)."""
        return self._dead_reason is not None

    def _mark_dead(self, reason: str) -> None:
        """Record a typed death reason (first one wins) so the pool evicts us."""
        if self._dead_reason is None:
            self._dead_reason = reason
            log = logger.info if reason == "closed" else logger.warning
            log("codex app-server process marked dead reason=%s model=%s", reason, self._model)

    # -- lifecycle --------------------------------------------------------- #
    def _spawn(self) -> None:
        """Spawn the app-server subprocess + start the reader thread (once)."""
        argv = [self._binary, "app-server", "--stdio"]
        creationflags = 0
        if os.name == "nt":
            # Keep the console window hidden on Windows (parity with the clio-core spawn
            # fix #870); the child still inherits the server's Job Object (#900).
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
                creationflags=creationflags,
            )
        except OSError as exc:
            # Typed + dead: the pool evicts this entry so the NEXT call retries a
            # fresh spawn (one respawn attempt per call, never a poisoned cache).
            self._mark_dead("spawn_failed")
            raise CodexAppServerError(f"failed to spawn codex app-server: {exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name="codex-app-server-reader", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        """Parse newline-delimited JSON, route responses vs notifications."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("codex app-server: non-JSON line dropped: %s", line[:200])
                continue
            self._dispatch(msg)
        # stdout closed → the process is gone. Mark dead FIRST (so the pool evicts
        # this entry on the next call), then fail any waiters so nothing hangs.
        self._mark_dead("stdout_closed")
        with self._lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for w in waiters:
            w.put({"error": {"message": "codex app-server stdout closed"}})
        sink = self._sink
        if sink is not None:
            sink.put({"method": "__closed__"})

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route one parsed message to a response waiter or the active sink."""
        mid = msg.get("id")
        if mid is not None and ("result" in msg or "error" in msg):
            with self._lock:
                waiter = self._pending.pop(int(mid), None)
            if waiter is not None:
                waiter.put(msg)
            return
        if mid is not None and "method" in msg:
            # Server→client request (approval / input). Read-only single-turn
            # Q&A must never hit this; deny explicitly so codex never blocks on us.
            self._reply_error(mid, msg.get("method", ""))
            return
        # Notification → the active turn sink. With no turn active (completed or
        # abandoned), the notification is stray: drop it with a typed audit reason
        # so it can never interleave into a later turn's stream.
        sink = self._sink
        if sink is not None:
            sink.put(msg)
        else:
            self._drop_stray(str(msg.get("method") or ""), "no_active_turn")

    def _drop_stray(self, method: str, reason: str) -> None:
        """Drop a stray/foreign notification with a typed, audited reason."""
        logger.debug(
            "codex app-server: dropped stray notification method=%s reason=%s", method, reason
        )
        if stream_audit_enabled():
            stream_audit(
                "provider.stray_notification",
                provider="codex_app_server",
                transport="app_server",
                model=self._model,
                method=method,
                reason=reason,
            )

    def _reply_error(self, mid: Any, method: str) -> None:
        """Decline an unexpected server-initiated request (no silent hang)."""
        logger.warning("codex app-server: declining unsupported server request method=%s", method)
        self._write(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": "clio: server requests unsupported"},
            }
        )

    def _write(self, msg: dict[str, Any]) -> None:
        """Serialize one JSON-RPC message to stdin (thread-safe)."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise CodexAppServerError("codex app-server stdin unavailable")
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                # A broken stdin means the subprocess is gone: typed + dead so the
                # pool evicts this entry and respawns on the next call.
                self._mark_dead("stdin_write_failed")
                raise CodexAppServerError(f"codex app-server write failed: {exc}") from exc

    def _request(
        self, method: str, params: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and block for its result (or raise)."""
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            slot: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
            self._pending[rid] = slot
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        try:
            reply = slot.get(timeout=timeout)
        except queue.Empty as exc:
            with self._lock:
                self._pending.pop(rid, None)
            raise CodexAppServerError(f"codex app-server request {method!r} timed out") from exc
        if "error" in reply:
            raise CodexAppServerError(f"codex app-server {method!r} error: {reply['error']}")
        return reply.get("result") or {}

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def _ensure_initialized(self, timeout: float) -> None:
        """Spawn + run the ``initialize`` handshake once."""
        if self._initialized:
            return
        with self._lock:
            already = self._initialized
        if already:
            return
        if self._proc is None:
            self._spawn()
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "clio-agent", "title": None, "version": "0.5"},
                "capabilities": None,
            },
            timeout=min(timeout, 30.0),
        )
        self._notify("initialized")
        self._initialized = True

    # -- turn driver ------------------------------------------------------- #
    def run_turn(
        self,
        *,
        prompt: str,
        effort: str | None,
        timeout: float,
    ) -> Iterator[TurnEvent]:
        """Drive one fresh ephemeral thread + turn, yielding :class:`TurnEvent`.

        The flag-OFF (non-stateful) path: a fresh ``ephemeral=True`` thread is
        started under the per-process turn lock and torn down with the turn, so
        there is no cross-call context bleed. Serialised by the per-process turn
        lock: concurrent same-key experts queue behind each other, and the lock
        wait is BOUNDED by ``timeout`` — an expired wait is a typed
        :class:`CodexAppServerError`, never an unbounded untyped block. Yields
        ``text``/``reasoning``/``usage`` events as notifications arrive, then a
        terminal ``final`` event. Raises :class:`CodexAppServerError` on an
        ``error`` notification, a transport death, or a timeout. If the caller
        abandons the generator mid-stream (``close()``), a best-effort
        ``turn/interrupt`` is sent and the sink is invalidated so late
        notifications are dropped typed, never leaked into the next turn.
        """
        deadline = time.monotonic() + timeout
        if not self._turn_lock.acquire(timeout=timeout):
            raise CodexAppServerError(
                f"codex app-server turn-lock wait timed out after {timeout}s "
                f"(a prior turn on this pooled process is still draining)"
            )
        try:
            if self._dead_reason is not None:
                raise CodexAppServerError(
                    f"codex app-server process is dead ({self._dead_reason}); "
                    f"the pool evicts it on the next call"
                )
            self._ensure_initialized(max(0.1, deadline - time.monotonic()))
            yield from self._run_turn_locked(
                prompt=prompt, effort=effort, deadline=deadline, thread_id=None, ephemeral=True
            )
        finally:
            self._turn_lock.release()

    def run_turn_on_thread(
        self,
        *,
        thread_id: str,
        prompt: str,
        effort: str | None,
        timeout: float,
    ) -> Iterator[TurnEvent]:
        """Continue an EXISTING persistent thread with one turn (#891 stateful delta).

        The flag-ON stateful path: the thread was opened once by :meth:`start_thread`
        (``ephemeral=False``) and RETAINS its prior turns server-side, so ``prompt``
        is only the NEW content (the append-only delta) — codex never re-ingests the
        static prefix it already holds. Same turn-lock discipline, bounded wait, and
        abandonment/interrupt semantics as :meth:`run_turn`; the only difference is
        that no ``thread/start`` is issued here (the thread already exists). The
        turn-pinned ``effort`` is re-sent on every ``turn/start`` so the #895
        reasoning knob keeps applying per turn on the persistent thread.
        """
        deadline = time.monotonic() + timeout
        if not self._turn_lock.acquire(timeout=timeout):
            raise CodexAppServerError(
                f"codex app-server turn-lock wait timed out after {timeout}s "
                f"(a prior turn on this pooled process is still draining)"
            )
        try:
            if self._dead_reason is not None:
                raise CodexAppServerError(
                    f"codex app-server process is dead ({self._dead_reason}); "
                    f"the pool evicts it on the next call"
                )
            self._ensure_initialized(max(0.1, deadline - time.monotonic()))
            yield from self._run_turn_locked(
                prompt=prompt, effort=effort, deadline=deadline, thread_id=thread_id
            )
        finally:
            self._turn_lock.release()

    def list_models(self, *, timeout: float = 20.0) -> list[dict[str, Any]]:
        """Query the warm app-server for its live model catalog (#1211 ``model/list``).

        Verified live against codex-cli 0.147.0: the app-server protocol carries a
        real ``model/list`` JSON-RPC method (confirmed via ``codex app-server
        generate-json-schema --experimental``) returning the ACCOUNT's current
        served models — the enumeration the static registry catalog can never
        keep up with (#1184: the registry's ``gpt-5.5``/``gpt-5.5-codex``/
        ``gpt-5.1`` are stale/rejected once the account has moved on). Does NOT
        take the per-process turn lock — a plain request/response with no
        streaming sink, same reasoning as :meth:`start_thread` (never
        interleaves with a concurrent turn's drain). Raises
        :class:`CodexAppServerError` on a dead process or a protocol/timeout
        failure — never a silent empty list.
        """
        deadline = time.monotonic() + timeout
        if self._dead_reason is not None:
            raise CodexAppServerError(
                f"codex app-server process is dead ({self._dead_reason}); "
                f"the pool evicts it on the next call"
            )
        self._ensure_initialized(max(0.1, deadline - time.monotonic()))
        result = self._request("model/list", {}, timeout=max(0.1, deadline - time.monotonic()))
        data = result.get("data")
        return data if isinstance(data, list) else []

    def start_thread(self, *, ephemeral: bool, timeout: float) -> str:
        """Open a thread and return its server-assigned id (the stateful open-handle).

        Used by the stateful-delta resolver to open ONE persistent
        (``ephemeral=False``) thread per expert-forward scope; the subsequent turns
        run on it via :meth:`run_turn_on_thread`. Does NOT take the per-process turn
        lock (``thread/start`` is a request/response with no stream sink, so it never
        interleaves with a concurrent turn's drain). Raises
        :class:`CodexAppServerError` on a dead process, a spawn/handshake failure, or
        a missing thread id — never a silent empty handle.
        """
        deadline = time.monotonic() + timeout
        if self._dead_reason is not None:
            raise CodexAppServerError(
                f"codex app-server process is dead ({self._dead_reason}); "
                f"the pool evicts it on the next call"
            )
        self._ensure_initialized(max(0.1, deadline - time.monotonic()))
        return self._start_thread(ephemeral=ephemeral, deadline=deadline)

    def _run_turn_locked(
        self,
        *,
        prompt: str,
        effort: str | None,
        deadline: float,
        thread_id: str | None,
        ephemeral: bool = False,
    ) -> Iterator[TurnEvent]:
        """Set the sink, (optionally) start a thread, run one turn, and drain it.

        The shared turn driver for both :meth:`run_turn` (``thread_id is None`` — a
        fresh ``ephemeral`` thread is started here, under the already-held turn lock,
        exactly as the pre-stateful path did) and :meth:`run_turn_on_thread`
        (``thread_id`` given — the persistent thread is reused, no ``thread/start``).
        The per-process turn lock MUST already be held. Abandonment fires a
        best-effort ``turn/interrupt`` and the ``finally`` invalidates the sink so
        late notifications are dropped typed.
        """
        sink: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._sink = sink
        resolved_thread_id = thread_id or ""
        turn_state: dict[str, Any] = {"turn_id": None}
        try:
            if thread_id is None:
                resolved_thread_id = self._start_thread(ephemeral=ephemeral, deadline=deadline)
            turn_params: dict[str, Any] = {
                "threadId": resolved_thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "summary": DEFAULT_SUMMARY,
            }
            if effort:
                turn_params["effort"] = effort
            self._notify_turn_start(turn_params)
            yield from self._drain(
                sink, deadline, thread_id=str(resolved_thread_id), turn_state=turn_state
            )
        except GeneratorExit:
            # Caller abandoned mid-stream: best-effort turn/interrupt (schema:
            # {threadId, turnId}); the finally invalidates the sink so late
            # notifications are dropped typed by _dispatch, never fed to the next turn.
            self._interrupt(str(resolved_thread_id), turn_state.get("turn_id"))
            raise
        finally:
            self._sink = None

    def _start_thread(self, *, ephemeral: bool, deadline: float) -> str:
        """Issue ``thread/start`` and return the server-assigned thread id (or raise)."""
        thread = self._request(
            "thread/start",
            {
                "model": self._model,
                "sandbox": DEFAULT_SANDBOX,
                "cwd": self._cwd,
                "ephemeral": ephemeral,
            },
            timeout=min(max(0.1, deadline - time.monotonic()), 30.0),
        )
        thread_id = ((thread.get("thread") or {}).get("id")) or thread.get("threadId")
        if not thread_id:
            raise CodexAppServerError("codex app-server thread/start returned no thread id")
        return str(thread_id)

    def _interrupt(self, thread_id: str, turn_id: Any) -> None:
        """Best-effort ``turn/interrupt`` for an abandoned turn (never raises)."""
        if not thread_id or not turn_id:
            return
        try:
            with self._lock:
                self._next_id += 1
                rid = self._next_id
                self._pending[rid] = queue.SimpleQueue()  # response consumed by reader
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "turn/interrupt",
                    "params": {"threadId": thread_id, "turnId": str(turn_id)},
                }
            )
        except Exception:  # noqa: BLE001 - abandonment cleanup must never mask GeneratorExit
            logger.debug("codex app-server: turn/interrupt failed", exc_info=True)

    def _notify_turn_start(self, params: dict[str, Any]) -> None:
        """Send ``turn/start`` as a request (its result is consumed at turn end).

        The turn is driven off notifications; the ``turn/start`` response lands in
        the pending table and is discarded by the reader after the turn completes.
        We fire it without blocking so streaming begins immediately.
        """
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            # A slot the reader can drop the (late) turn/start result into; we
            # never read it, so drain it opportunistically to avoid a leak.
            self._pending[rid] = queue.SimpleQueue()
        self._write({"jsonrpc": "2.0", "id": rid, "method": "turn/start", "params": params})

    def _drain(
        self,
        sink: queue.SimpleQueue[dict[str, Any]],
        deadline: float,
        *,
        thread_id: str,
        turn_state: dict[str, Any],
    ) -> Iterator[TurnEvent]:
        """Yield normalized events from the sink until ``turn/completed``/error.

        Notifications carrying a foreign ``threadId`` (a late event from an
        abandoned prior turn) are dropped with a typed audit reason — the
        interleave guard for the shared per-process stream.
        """
        final_text_parts: list[str] = []
        item_message_text = ""
        last_usage: dict[str, int] = {}
        reasoning_summary_parts: set[tuple[str, int]] = set()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerError("codex app-server turn timed out")
            try:
                msg = sink.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexAppServerError("codex app-server turn timed out") from exc
            method = str(msg.get("method") or "")
            params = msg.get("params") or {}
            msg_thread = str(params.get("threadId") or "")
            if msg_thread and msg_thread != thread_id:
                self._drop_stray(method, "foreign_thread_id")
                continue
            if method == "turn/started":
                turn = params.get("turn") or {}
                if turn.get("id"):
                    turn_state["turn_id"] = turn["id"]
            elif method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    final_text_parts.append(delta)
                    yield TurnEvent("text", text=delta)
            elif method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
                rtext = str(params.get("delta") or params.get("text") or "")
                if rtext:
                    yield TurnEvent("reasoning", text=rtext)
            elif method == "item/reasoning/summaryPartAdded":
                item_id = str(params.get("itemId") or "")
                try:
                    summary_index = int(params.get("summaryIndex", -1))
                except (TypeError, ValueError):
                    summary_index = -1
                part_key = (item_id, summary_index)
                if part_key not in reasoning_summary_parts:
                    if reasoning_summary_parts:
                        # Codex summary parts are separate readable thoughts. Preserve
                        # that protocol boundary instead of persisting `****` joins.
                        yield TurnEvent("reasoning", text="\n\n")
                    reasoning_summary_parts.add(part_key)
            elif method == "thread/tokenUsage/updated":
                last = (params.get("tokenUsage") or {}).get("last") or {}
                last_usage = normalize_usage(last)
                yield TurnEvent("usage", usage=last_usage)
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    item_message_text = str(item.get("text") or "")
            elif method == "error":
                raise CodexAppServerError(f"codex app-server error: {params}")
            elif method == "__closed__":
                raise CodexAppServerError("codex app-server closed mid-turn")
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "completed")
                if status == "failed":
                    raise CodexAppServerError(f"codex app-server turn failed: {turn.get('error')}")
                text = "".join(final_text_parts) or item_message_text
                yield TurnEvent("final", text=text.strip(), usage=last_usage, reason=status)
                return

    def close(self) -> None:
        """Terminate the subprocess + join the reader thread (best-effort)."""
        self._mark_dead("closed")  # a closed process must never serve another turn
        proc, self._proc = self._proc, None
        self._initialized = False
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001 - teardown must never raise
            logger.warning("codex app-server terminate failed", exc_info=True)
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(timeout=2)


# --------------------------------------------------------------------------- #
# Process pool: one warm subprocess per (model, cwd).
# --------------------------------------------------------------------------- #
class CodexAppServerPool:
    """Process-wide pool of warm ``codex app-server`` subprocesses (#891/#900).

    Keyed by ``(model, cwd)`` so distinct-model/cwd experts each hold their own
    subprocess while same-key experts share one — the spawn + ``initialize``
    handshake is paid once. Each call runs a fresh ephemeral thread on the shared
    subprocess (compartmentalisation is the per-call thread boundary, not the
    process). :meth:`close_blocking` reaps every subprocess on clean shutdown.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str | None], CodexAppServerProcess] = {}
        self._guard = threading.Lock()
        self._spawn_count = 0

    def process_for(self, *, binary: str, model: str, cwd: str | None) -> CodexAppServerProcess:
        """Return the warm process for ``(model, cwd)``, evicting a dead one first.

        A process marked dead (reader exit / write failure / spawn failure) is
        evicted and replaced with a fresh entry — the NEXT call's respawn attempt
        (bounded: one per call; a failed respawn raises typed from ``run_turn``
        and marks the fresh entry dead, so the call after that retries again
        instead of caching the corpse). This is the self-healing parity with the
        per-call ``exec`` transport.
        """
        key = (model, cwd)
        stale: CodexAppServerProcess | None = None
        with self._guard:
            proc = self._entries.get(key)
            if proc is not None and proc.is_dead:
                logger.info(
                    "codex app-server pool: evicting dead process reason=%s model=%s",
                    proc.dead_reason,
                    model,
                )
                stale, proc = proc, None
                del self._entries[key]
            if proc is None:
                proc = CodexAppServerProcess(binary=binary, model=model, cwd=cwd)
                self._entries[key] = proc
                self._spawn_count += 1
        if stale is not None:
            stale.close()  # best-effort reap outside the guard (close never raises)
        return proc

    @property
    def spawn_count(self) -> int:
        """How many processes this pool has created (process-reuse assertions)."""
        with self._guard:
            return self._spawn_count

    def close_blocking(self) -> None:
        """Terminate every pooled subprocess (#900: no orphaned codex children)."""
        with self._guard:
            procs = list(self._entries.values())
            self._entries.clear()
        for proc in procs:
            proc.close()

    def reset_for_tests(self) -> None:
        """Tear down pooled processes + counter IN PLACE (never rebind the singleton)."""
        self.close_blocking()
        with self._guard:
            self._spawn_count = 0


_APP_SERVER_POOL = CodexAppServerPool()
atexit.register(_APP_SERVER_POOL.close_blocking)


def _reset_app_server_for_tests() -> None:
    """Drop all pooled app-server state (test isolation), mutating the singleton."""
    _APP_SERVER_POOL.reset_for_tests()
