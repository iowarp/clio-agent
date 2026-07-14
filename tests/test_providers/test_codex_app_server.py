"""Tests for the native ``codex app-server`` transport (#896).

The JSON-RPC/stdio framing, request/response correlation, notification routing,
delta→chunk pipeline, usage mapping, teardown, and kill-switch are all exercised
against a SCRIPTED FAKE app-server (a fake ``subprocess.Popen`` that speaks the
real protocol) so the suite runs with no ``codex`` binary. The load-bearing pins
(effort on ``turn/start``, cache/reasoning usage mapping, kill-switch routing) are
sabotage-checked.
"""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Any
from unittest.mock import patch

import pytest

from clio_agent.providers import codex_app_server as cas
from clio_agent.providers.codex_app_server import (
    CodexAppServerError,
    CodexAppServerProcess,
    normalize_usage,
)


# --------------------------------------------------------------------------- #
# Scripted fake app-server (fake subprocess.Popen).
# --------------------------------------------------------------------------- #
class _FakeStdout:
    """A blocking line iterator fed by the fake server; ``None`` ends the stream."""

    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def push(self, line: str) -> None:
        self._q.put(line)

    def close(self) -> None:
        self._q.put(None)

    def __iter__(self) -> _FakeStdout:
        return self

    def __next__(self) -> str:
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _FakeStdin:
    """Receives one full JSON-RPC message per ``write`` (mirrors the real _write)."""

    def __init__(self, on_line: Any) -> None:
        self._on_line = on_line
        self.closed = False

    def write(self, data: str) -> None:
        for line in data.splitlines():
            line = line.strip()
            if line:
                self._on_line(json.loads(line))

    def flush(self) -> None:
        pass


class FakeAppServer:
    """A scripted ``codex app-server``: replies to requests + streams turns.

    ``turn_script`` is a list of (method, params) notifications pushed on
    ``turn/start`` (deltas, usage, item/completed, turn/completed); pass
    ``turn_scripts`` (a list of scripts) to serve successive turns different
    content. ``server_request`` optionally injects a server→client request
    before the turn to check it is declined (not hung). ``turn/interrupt``
    requests are recorded on :attr:`interrupts`.
    """

    def __init__(
        self,
        *,
        turn_script: list[tuple[str, dict[str, Any]]] | None = None,
        turn_scripts: list[list[tuple[str, dict[str, Any]]]] | None = None,
        thread_id: str = "thread-1",
        server_request: bool = False,
    ) -> None:
        if turn_scripts is None:
            turn_scripts = [turn_script or []]
        self._turn_scripts = list(turn_scripts)
        self._turn_count = 0
        self.thread_id = thread_id
        self.server_request = server_request
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(self._on_line)
        self.stderr = _FakeStdout()
        self.turn_start_params: dict[str, Any] | None = None
        self.thread_start_params: list[dict[str, Any]] = []
        self.interrupts: list[dict[str, Any]] = []
        self.terminated = False
        self._next_server_id = 1000

    # -- the fake protocol ------------------------------------------------- #
    def _respond(self, mid: Any, result: dict[str, Any]) -> None:
        self.stdout.push(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}))

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self.stdout.push(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def _on_line(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            self._respond(mid, {"userAgent": "fake/1", "platformOs": "test"})
        elif method == "initialized":
            pass
        elif method == "thread/start":
            self._turn_count += 1
            self.thread_start_params.append(msg.get("params") or {})
            self._respond(mid, {"thread": {"id": f"{self.thread_id}-{self._turn_count}"}})
        elif method == "turn/interrupt":
            self.interrupts.append(msg.get("params") or {})
            self._respond(mid, {})
        elif method == "turn/start":
            self.turn_start_params = msg.get("params")
            if self.server_request:
                # A server→client request the driver must decline (not hang on).
                self.stdout.push(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": self._next_server_id,
                            "method": "exec/approval",
                            "params": {},
                        }
                    )
                )
            script_index = min(self._turn_count, len(self._turn_scripts)) - 1
            turn_thread_id = str((msg.get("params") or {}).get("threadId") or "")
            for m, p in self._turn_scripts[script_index]:
                # Stamp this turn's threadId onto scripted params that omit it —
                # matching the real server, where every turn-scoped notification
                # carries the thread it belongs to (the drain filters on it).
                stamped = dict(p)
                stamped.setdefault("threadId", turn_thread_id)
                self._notify(m, stamped)
            # A late turn/start response (the driver drops it) — realism check.
            self._respond(mid, {"turn": {"id": "turn-1", "status": "completed"}})

    # -- subprocess.Popen surface ------------------------------------------ #
    def terminate(self) -> None:
        self.terminated = True
        self.stdout.close()

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return 0 if self.terminated else None


def _default_script() -> list[tuple[str, dict[str, Any]]]:
    usage = {
        "inputTokens": 18360,
        "cachedInputTokens": 1920,
        "outputTokens": 42,
        "reasoningOutputTokens": 8,
        "totalTokens": 18402,
    }
    return [
        ("thread/started", {"thread": {"id": "thread-1"}}),
        ("turn/started", {}),
        ("item/agentMessage/delta", {"delta": "The sky "}),
        ("item/agentMessage/delta", {"delta": "is blue."}),
        ("thread/tokenUsage/updated", {"tokenUsage": {"last": usage}}),
        ("item/completed", {"item": {"type": "agentMessage", "text": "The sky is blue."}}),
        ("turn/completed", {"turn": {"status": "completed"}}),
    ]


def _make_process(fake: FakeAppServer) -> CodexAppServerProcess:
    proc = CodexAppServerProcess(binary="codex", model="gpt-5.5", cwd=None)
    with patch.object(cas.subprocess, "Popen", return_value=fake):
        # Spawn is lazy inside run_turn; force it so the reader thread starts.
        proc._spawn()
    return proc


# --------------------------------------------------------------------------- #
# normalize_usage (pure unit).
# --------------------------------------------------------------------------- #
def test_normalize_usage_maps_cache_and_reasoning() -> None:
    out = normalize_usage(
        {
            "inputTokens": 100,
            "cachedInputTokens": 40,
            "outputTokens": 20,
            "reasoningOutputTokens": 5,
            "totalTokens": 120,
        }
    )
    assert out == {
        "input_tokens": 100,
        "cache_read_input_tokens": 40,  # cachedInputTokens → cache-read column
        "cache_creation_input_tokens": 0,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,  # reasoningOutputTokens → reasoning column
        "total_tokens": 120,
    }


def test_normalize_usage_tolerates_missing_and_junk() -> None:
    assert normalize_usage(None)["input_tokens"] == 0
    assert normalize_usage({"inputTokens": "oops"})["input_tokens"] == 0


# --------------------------------------------------------------------------- #
# Protocol framing + delta/usage streaming against the fake.
# --------------------------------------------------------------------------- #
def test_run_turn_streams_deltas_usage_and_final() -> None:
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        events = list(proc.run_turn(prompt="why is the sky blue?", effort="low", timeout=10.0))
    finally:
        proc.close()

    texts = [e.text for e in events if e.kind == "text"]
    assert texts == ["The sky ", "is blue."]
    usages = [e for e in events if e.kind == "usage"]
    assert usages and usages[-1].usage["cache_read_input_tokens"] == 1920
    final = [e for e in events if e.kind == "final"]
    assert len(final) == 1
    assert final[0].text == "The sky is blue."
    assert final[0].usage["input_tokens"] == 18360
    assert final[0].reason == "completed"


def test_run_turn_pins_effort_on_turn_start() -> None:
    """LOAD-BEARING: the #895 effort must reach turn/start (closes the no-op)."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        list(proc.run_turn(prompt="hi", effort="high", timeout=10.0))
    finally:
        proc.close()
    assert fake.turn_start_params is not None
    assert fake.turn_start_params["effort"] == "high"


def test_run_turn_omits_effort_when_unset() -> None:
    """SABOTAGE twin: no effort → no 'effort' key on turn/start (codex default)."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        list(proc.run_turn(prompt="hi", effort=None, timeout=10.0))
    finally:
        proc.close()
    assert fake.turn_start_params is not None
    assert "effort" not in fake.turn_start_params


def test_turn_start_body_matches_real_schema() -> None:
    """The turn/start request body matches the v2 TurnStartParams schema exactly
    as smoked against codex-cli 0.144.1: threadId, input=[UserInput text with
    text_elements], summary; effort only when pinned."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        list(proc.run_turn(prompt="why is the sky blue?", effort="low", timeout=10.0))
    finally:
        proc.close()
    params = fake.turn_start_params
    assert params is not None
    # threadId is the id thread/start returned for THIS turn.
    assert params["threadId"] == "thread-1-1"
    # UserInput: type=text carries the prompt verbatim + the required
    # text_elements array (v2/UserInput.ts).
    assert params["input"] == [
        {"type": "text", "text": "why is the sky blue?", "text_elements": []}
    ]
    # ReasoningSummary request rides every turn (auto|concise|detailed|none).
    assert params["summary"] == "detailed"
    assert params["effort"] == "low"
    # No stray keys beyond the schema surface we drive.
    assert set(params) == {"threadId", "input", "summary", "effort"}


def test_run_turn_falls_back_to_item_text_without_deltas() -> None:
    script = [
        ("item/completed", {"item": {"type": "agentMessage", "text": "batched answer"}}),
        ("turn/completed", {"turn": {"status": "completed"}}),
    ]
    fake = FakeAppServer(turn_script=script)
    proc = _make_process(fake)
    try:
        events = list(proc.run_turn(prompt="hi", effort=None, timeout=10.0))
    finally:
        proc.close()
    final = [e for e in events if e.kind == "final"][0]
    assert final.text == "batched answer"


def test_error_notification_raises_typed() -> None:
    script = [("error", {"message": "backend exploded"})]
    fake = FakeAppServer(turn_script=script)
    proc = _make_process(fake)
    try:
        with pytest.raises(CodexAppServerError, match="backend exploded"):
            list(proc.run_turn(prompt="hi", effort=None, timeout=10.0))
    finally:
        proc.close()


def test_failed_turn_status_raises() -> None:
    script = [("turn/completed", {"turn": {"status": "failed", "error": "nope"}})]
    fake = FakeAppServer(turn_script=script)
    proc = _make_process(fake)
    try:
        with pytest.raises(CodexAppServerError, match="turn failed"):
            list(proc.run_turn(prompt="hi", effort=None, timeout=10.0))
    finally:
        proc.close()


def test_turn_timeout_raises() -> None:
    fake = FakeAppServer(turn_script=[("turn/started", {})])  # never completes
    proc = _make_process(fake)
    try:
        with pytest.raises(CodexAppServerError, match="timed out"):
            list(proc.run_turn(prompt="hi", effort=None, timeout=0.5))
    finally:
        proc.close()


def test_server_request_is_declined_not_hung() -> None:
    """An unexpected server→client request is declined so the turn never hangs."""
    fake = FakeAppServer(turn_script=_default_script(), server_request=True)
    proc = _make_process(fake)
    try:
        events = list(proc.run_turn(prompt="hi", effort=None, timeout=10.0))
    finally:
        proc.close()
    assert any(e.kind == "final" for e in events)


def test_stdout_close_midturn_raises() -> None:
    """A transport death mid-turn is a typed error, never a silent hang."""
    fake = FakeAppServer(turn_script=[("turn/started", {})])
    proc = _make_process(fake)

    def _die() -> None:
        fake.stdout.close()

    # After starting the turn, kill the stream from another thread.
    import threading

    threading.Timer(0.3, _die).start()
    try:
        with pytest.raises(CodexAppServerError):
            list(proc.run_turn(prompt="hi", effort=None, timeout=10.0))
    finally:
        proc.close()


# --------------------------------------------------------------------------- #
# Pool reuse + teardown (#900).
# --------------------------------------------------------------------------- #
def test_pool_reuses_process_per_key_and_tears_down() -> None:
    pool = cas.CodexAppServerPool()
    fakes: list[FakeAppServer] = []

    def _fake_popen(*_a: Any, **_k: Any) -> FakeAppServer:
        f = FakeAppServer(turn_script=_default_script())
        fakes.append(f)
        return f

    with patch.object(cas.subprocess, "Popen", side_effect=_fake_popen):
        p1 = pool.process_for(binary="codex", model="gpt-5.5", cwd=None)
        p2 = pool.process_for(binary="codex", model="gpt-5.5", cwd=None)
        p3 = pool.process_for(binary="codex", model="gpt-5.6", cwd=None)
        assert p1 is p2  # same key → shared process
        assert p3 is not p1  # distinct model → distinct process
        assert pool.spawn_count == 2
        # Force spawns so there are real reader threads to reap.
        p1._spawn()
        p3._spawn()
        pool.close_blocking()
    assert all(f.terminated for f in fakes)
    assert pool.spawn_count == 2  # close does not reset the counter


def test_reset_for_tests_clears_pool() -> None:
    pool = cas.CodexAppServerPool()
    with patch.object(cas.subprocess, "Popen", return_value=FakeAppServer(turn_script=[])):
        pool.process_for(binary="codex", model="m", cwd=None)
        assert pool.spawn_count == 1
        pool.reset_for_tests()
    assert pool.spawn_count == 0


# --------------------------------------------------------------------------- #
# Dead-process eviction + bounded respawn (the review BLOCKER pin).
# --------------------------------------------------------------------------- #
def test_pool_evicts_dead_process_and_respawns() -> None:
    """A mid-turn transport death poisons NOTHING: the next call for the same
    key gets a FRESH process (spawn_count increments) and succeeds."""
    import threading

    pool = cas.CodexAppServerPool()
    fakes: list[FakeAppServer] = []
    scripts: list[list[tuple[str, dict[str, Any]]]] = [
        [("turn/started", {})],  # first process: the turn never completes
        _default_script(),  # respawned process: a healthy turn
    ]

    def _fake_popen(*_a: Any, **_k: Any) -> FakeAppServer:
        f = FakeAppServer(turn_script=scripts[len(fakes)])
        fakes.append(f)
        return f

    with patch.object(cas.subprocess, "Popen", side_effect=_fake_popen):
        p1 = pool.process_for(binary="codex", model="gpt-5.5", cwd=None)
        # Kill the transport mid-turn (while the drain is blocked waiting): the
        # turn fails typed and p1 is marked dead.
        p1._spawn()
        dying = fakes[0]
        threading.Timer(0.2, dying.stdout.close).start()
        with pytest.raises(CodexAppServerError):
            list(p1.run_turn(prompt="hi", effort=None, timeout=10.0))
        assert p1.is_dead
        assert p1.dead_reason == "stdout_closed"

        # Next call for the SAME key: the corpse is evicted, a fresh process is
        # created (spawn_count increments) and the turn succeeds.
        assert pool.spawn_count == 1
        p2 = pool.process_for(binary="codex", model="gpt-5.5", cwd=None)
        assert p2 is not p1
        assert pool.spawn_count == 2
        events = list(p2.run_turn(prompt="hi again", effort=None, timeout=10.0))
        assert [e.text for e in events if e.kind == "text"] == ["The sky ", "is blue."]
        pool.close_blocking()


def test_pool_respawn_failure_is_typed_not_a_hang() -> None:
    """When the respawn itself fails, the call gets a typed error (bounded: one
    attempt per call), and the call AFTER that retries a fresh spawn again —
    the failed entry is never cached as healthy."""
    pool = cas.CodexAppServerPool()

    with patch.object(cas.subprocess, "Popen", side_effect=OSError("binary gone")):
        p1 = pool.process_for(binary="codex", model="gpt-5.5", cwd=None)
        with pytest.raises(CodexAppServerError, match="failed to spawn"):
            list(p1.run_turn(prompt="hi", effort=None, timeout=5.0))
        assert p1.is_dead and p1.dead_reason == "spawn_failed"

    # The pool does not serve the spawn-failed corpse: next call gets a fresh
    # entry (one respawn attempt per call) which now succeeds.
    fake = FakeAppServer(turn_script=_default_script())
    with patch.object(cas.subprocess, "Popen", return_value=fake):
        p2 = pool.process_for(binary="codex", model="gpt-5.5", cwd=None)
        assert p2 is not p1
        assert pool.spawn_count == 2
        events = list(p2.run_turn(prompt="hi", effort=None, timeout=10.0))
        assert any(e.kind == "final" for e in events)
        pool.close_blocking()


def test_dead_process_direct_run_turn_is_typed() -> None:
    """Defensive: driving a dead process directly (bypassing the pool) raises
    typed immediately instead of writing to a broken pipe."""
    proc = CodexAppServerProcess(binary="codex", model="m", cwd=None)
    proc._mark_dead("stdout_closed")
    with pytest.raises(CodexAppServerError, match="dead"):
        list(proc.run_turn(prompt="hi", effort=None, timeout=5.0))


# --------------------------------------------------------------------------- #
# Turn-lock bounded wait (review SHOULD-FIX 2 pin).
# --------------------------------------------------------------------------- #
def test_turn_lock_wait_timeout_is_typed() -> None:
    """A queued turn that cannot acquire the per-process lock within its timeout
    fails with a typed CodexAppServerError — never an unbounded untyped wait."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        assert proc._turn_lock.acquire(timeout=1.0)  # hold the lock like a stuck turn
        try:
            with pytest.raises(CodexAppServerError, match="turn-lock wait timed out"):
                list(proc.run_turn(prompt="hi", effort=None, timeout=0.3))
        finally:
            proc._turn_lock.release()
    finally:
        proc.close()


# --------------------------------------------------------------------------- #
# Abandonment (review SHOULD-FIX 3 pin).
# --------------------------------------------------------------------------- #
def test_abandoned_turn_does_not_leak_into_next_turn() -> None:
    """Abandon mid-stream → turn/interrupt is sent, and the NEXT turn's events
    contain none of the prior turn's deltas (the interleave guard)."""
    old_usage = {"inputTokens": 1, "cachedInputTokens": 0, "outputTokens": 1, "totalTokens": 2}
    turn1 = [
        ("turn/started", {"turn": {"id": "turn-old"}}),
        ("item/agentMessage/delta", {"delta": "OLD-DELTA-1"}),
        ("item/agentMessage/delta", {"delta": "OLD-DELTA-2"}),
        ("thread/tokenUsage/updated", {"tokenUsage": {"last": old_usage}}),
        ("turn/completed", {"turn": {"status": "completed"}}),
    ]
    turn2 = [
        ("turn/started", {"turn": {"id": "turn-new"}}),
        ("item/agentMessage/delta", {"delta": "NEW-DELTA"}),
        ("item/completed", {"item": {"type": "agentMessage", "text": "NEW-DELTA"}}),
        ("turn/completed", {"turn": {"status": "completed"}}),
    ]
    fake = FakeAppServer(turn_scripts=[turn1, turn2])
    proc = _make_process(fake)
    try:
        gen = proc.run_turn(prompt="first", effort=None, timeout=10.0)
        first = next(gen)
        while first.kind != "text":
            first = next(gen)
        assert first.text == "OLD-DELTA-1"
        gen.close()  # abandon mid-stream (fires GeneratorExit inside run_turn)

        # The abandonment sent a best-effort turn/interrupt with the schema shape.
        assert fake.interrupts == [{"threadId": "thread-1-1", "turnId": "turn-old"}]

        # The next turn sees ONLY its own deltas — nothing from the abandoned turn.
        events = list(proc.run_turn(prompt="second", effort=None, timeout=10.0))
        texts = [e.text for e in events if e.kind == "text"]
        assert texts == ["NEW-DELTA"]
        assert not any("OLD-DELTA" in e.text for e in events)
        final = [e for e in events if e.kind == "final"][0]
        assert final.text == "NEW-DELTA"
    finally:
        proc.close()


def test_stray_notification_after_turn_is_dropped_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late notification with no active turn is dropped with a typed audit
    reason (no_active_turn) — never buffered into a later turn."""
    dropped: list[dict[str, Any]] = []
    monkeypatch.setattr(cas, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        cas, "stream_audit", lambda stage, **f: dropped.append({**f, "stage": stage})
    )

    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        list(proc.run_turn(prompt="hi", effort=None, timeout=10.0))
        # A late delta from the (finished) turn arrives after the sink is gone.
        fake._notify("item/agentMessage/delta", {"threadId": "thread-1-1", "delta": "LATE"})
        deadline = __import__("time").monotonic() + 5.0
        while not dropped and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
    finally:
        proc.close()
    stray = [d for d in dropped if d["stage"] == "provider.stray_notification"]
    assert stray, "late notification was not audited as a typed stray drop"
    assert stray[0]["reason"] == "no_active_turn"
    assert stray[0]["method"] == "item/agentMessage/delta"


# --------------------------------------------------------------------------- #
# Kill-switch deletion (v0.8.0).
# --------------------------------------------------------------------------- #
def test_kill_switch_machinery_is_deleted() -> None:
    """v0.8.0: the #896 kill-switch and its downgrade catalog are gone for good.

    The module must not grow them back — a broken app-server is a typed hard
    error, never a silent downgrade to a batch path (which no longer exists).
    """
    assert not hasattr(cas, "app_server_enabled")
    assert not hasattr(cas, "transport_fallback_payload")
    assert not hasattr(cas, "TRANSPORT_FALLBACK_REASONS")


def test_spawn_failure_is_typed() -> None:
    proc = CodexAppServerProcess(binary="codex", model="m", cwd=None)
    with (
        patch.object(cas.subprocess, "Popen", side_effect=OSError("no binary")),
        pytest.raises(CodexAppServerError, match="failed to spawn"),
    ):
        proc._spawn()


def test_async_bridge_over_fake() -> None:
    """The whole async path (executor bridge) drains a fake turn to completion."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)

    async def _drive() -> list[cas.TurnEvent]:
        loop = asyncio.get_running_loop()
        gen = proc.run_turn(prompt="hi", effort="low", timeout=10.0)
        out: list[cas.TurnEvent] = []
        sentinel = object()

        def _next() -> Any:
            try:
                return next(gen)
            except StopIteration:
                return sentinel

        while True:
            ev = await loop.run_in_executor(None, _next)
            if ev is sentinel:
                break
            out.append(ev)
        return out

    try:
        events = asyncio.run(_drive())
    finally:
        proc.close()
    assert [e.text for e in events if e.kind == "text"] == ["The sky ", "is blue."]


# --------------------------------------------------------------------------- #
# Persistent-thread methods (the #891 stateful-delta transport surface).
# --------------------------------------------------------------------------- #
def test_start_thread_opens_persistent_thread_and_returns_id() -> None:
    """start_thread issues one thread/start with ephemeral=False and returns the id."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        tid = proc.start_thread(ephemeral=False, timeout=10.0)
    finally:
        proc.close()
    assert tid == "thread-1-1"
    assert len(fake.thread_start_params) == 1
    # LOAD-BEARING: a persistent thread is ephemeral=False (retains state server-side).
    assert fake.thread_start_params[0]["ephemeral"] is False


def test_run_turn_on_thread_continues_without_a_fresh_thread_start() -> None:
    """The delta path reuses an existing thread: no SECOND thread/start is issued,
    the turn/start carries the given threadId, and effort still reaches turn/start."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        tid = proc.start_thread(ephemeral=False, timeout=10.0)
        events = list(
            proc.run_turn_on_thread(thread_id=tid, prompt="delta body", effort="high", timeout=10.0)
        )
    finally:
        proc.close()
    # Exactly ONE thread/start total (the open) — the turn did NOT start a new thread.
    assert len(fake.thread_start_params) == 1
    assert fake.turn_start_params is not None
    assert fake.turn_start_params["threadId"] == tid
    assert fake.turn_start_params["effort"] == "high"
    assert fake.turn_start_params["input"] == [
        {"type": "text", "text": "delta body", "text_elements": []}
    ]
    assert [e.text for e in events if e.kind == "text"] == ["The sky ", "is blue."]


def test_run_turn_on_thread_omits_effort_when_unset() -> None:
    """No effort → no 'effort' key on the continued turn (codex default)."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        tid = proc.start_thread(ephemeral=False, timeout=10.0)
        list(proc.run_turn_on_thread(thread_id=tid, prompt="x", effort=None, timeout=10.0))
    finally:
        proc.close()
    assert fake.turn_start_params is not None
    assert "effort" not in fake.turn_start_params


def test_run_turn_still_uses_ephemeral_thread_byte_identical() -> None:
    """SABOTAGE twin: the flag-OFF run_turn path MUST still open ephemeral=True — the
    refactor to share _run_turn_locked must not leak the persistent flag into it."""
    fake = FakeAppServer(turn_script=_default_script())
    proc = _make_process(fake)
    try:
        list(proc.run_turn(prompt="hi", effort=None, timeout=10.0))
    finally:
        proc.close()
    assert len(fake.thread_start_params) == 1
    assert fake.thread_start_params[0]["ephemeral"] is True


def test_start_thread_on_dead_process_is_typed() -> None:
    """Defensive: opening a thread on a dead process raises typed, not a broken pipe."""
    proc = CodexAppServerProcess(binary="codex", model="m", cwd=None)
    proc._mark_dead("stdout_closed")
    with pytest.raises(CodexAppServerError, match="dead"):
        proc.start_thread(ephemeral=False, timeout=5.0)


def test_run_turn_on_thread_on_dead_process_is_typed() -> None:
    """Defensive: continuing a thread on a dead process raises typed immediately."""
    proc = CodexAppServerProcess(binary="codex", model="m", cwd=None)
    proc._mark_dead("stdout_closed")
    with pytest.raises(CodexAppServerError, match="dead"):
        list(proc.run_turn_on_thread(thread_id="t", prompt="hi", effort=None, timeout=5.0))
