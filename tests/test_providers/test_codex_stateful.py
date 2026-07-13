"""Stateful session-delta transport for the native ``codex app-server`` (#891).

The codex analog of ``test_claude_code_stateful``: these pin the send-side resolver
(:func:`resolve_codex_stateful_send`) + its persistent-thread lifecycle + the
transport routing on the REAL objects, driving a FAKE app-server *process* (a stub
that records ``start_thread`` / ``run_turn_on_thread`` / ``run_turn`` calls) so the
suite runs with no ``codex`` binary. The pure detector + registry machinery is shared
with claude and proved in ``test_claude_code_stateful`` / the shared unit suite; here
we pin the codex-specific glue.

Three headline SABOTAGES the task names, each with an inline note:

(a) make the prefix check fuzzy -> a divergent-prefix resolve goes to ``prefix_mismatch``
    full (a length-only check would mis-delta) -> red;
(b) drop the reset-on-respawn (peek_extra / mark_reset) -> a delta is attempted on the
    dead thread instead of a ``session_evicted`` full restart -> red;
(c) force delta on the flag-OFF path -> the inert-contract pin (full prompt, ephemeral
    ``run_turn``, pool untouched) goes red.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from clio_agent.providers import codex_stateful as cst
from clio_agent.providers import codex_stream as cs
from clio_agent.providers.codex_app_server import CodexAppServerError, TurnEvent
from clio_agent.providers.codex_stateful import (
    CodexStatefulSend,
    codex_stateful_registry,
    resolve_codex_stateful_send,
)
from clio_agent.providers.stateful_common import stateful_scope


# --------------------------------------------------------------------------- #
# Fakes: a process that records its calls + a pool that can be respawned.
# --------------------------------------------------------------------------- #
class FakeProcess:
    """A stub ``CodexAppServerProcess`` recording thread/turn calls."""

    def __init__(self, *, dead: bool = False) -> None:
        self.threads_started: list[tuple[str, bool]] = []  # (thread_id, ephemeral)
        self.turns: list[tuple[str, str, str | None]] = []  # (thread_id, prompt, effort)
        self.ephemeral_prompts: list[str] = []  # run_turn (inert path)
        self._counter = 0
        self._dead = dead

    def start_thread(self, *, ephemeral: bool, timeout: float) -> str:
        if self._dead:
            raise CodexAppServerError("codex app-server process is dead (stdout_closed)")
        self._counter += 1
        tid = f"T{self._counter}"
        self.threads_started.append((tid, ephemeral))
        return tid

    def run_turn_on_thread(
        self, *, thread_id: str, prompt: str, effort: str | None, timeout: float
    ) -> Iterator[TurnEvent]:
        self.turns.append((thread_id, prompt, effort))
        yield TurnEvent("final", text="ok", usage={}, reason="completed")

    def run_turn(self, *, prompt: str, effort: str | None, timeout: float) -> Iterator[TurnEvent]:
        self.ephemeral_prompts.append(prompt)
        yield TurnEvent("final", text="ok", usage={}, reason="completed")


class FakePool:
    """A stub app-server pool that always returns its (swappable) current process."""

    def __init__(self, process: FakeProcess) -> None:
        self._process = process

    def process_for(self, *, binary: str, model: str, cwd: str | None) -> FakeProcess:
        return self._process

    def set_process(self, process: FakeProcess) -> None:
        self._process = process


def _m(*texts: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": t} for t in texts]


def _ser(messages: list[dict[str, Any]]) -> str:
    return "|".join(str(m["content"]) for m in messages)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Reset the codex registry + pin a fake binary; enable the flag by default."""
    codex_stateful_registry().reset_for_tests()
    monkeypatch.setattr(cst, "codex_stateful_delta_enabled", lambda: True)
    # The resolver + transport resolve the binary lazily from codex_litellm.
    monkeypatch.setattr(
        "clio_agent.providers.codex_litellm._resolve_codex_binary", lambda: "codex"
    )
    yield
    codex_stateful_registry().reset_for_tests()


def _install_pool(monkeypatch: pytest.MonkeyPatch, process: FakeProcess) -> FakePool:
    pool = FakePool(process)
    # resolve imports the pool lazily from codex_app_server; _app_server_events reads
    # the module-level name in codex_stream. Patch both bindings.
    monkeypatch.setattr("clio_agent.providers.codex_app_server._APP_SERVER_POOL", pool)
    monkeypatch.setattr(cs, "_APP_SERVER_POOL", pool)
    return pool


def _resolve(messages: list[dict[str, Any]], full: str) -> CodexStatefulSend:
    return resolve_codex_stateful_send(
        messages=messages,
        full_prompt=full,
        model="gpt-5.6",
        cwd="/w",
        effort="low",
        serialize=_ser,
        start_timeout=10.0,
    )


# --------------------------------------------------------------------------- #
# 1. Engaged: first call opens a persistent thread; the next call is a delta.
# --------------------------------------------------------------------------- #
def test_first_call_opens_persistent_thread_full(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    with stateful_scope("s"):
        s1 = _resolve(_m("a", "b"), "AB")
    assert s1.engaged is True
    assert s1.mode == "full"
    assert s1.reason == "first_call"
    assert s1.prompt == "AB"  # the FULL prompt on the first (opening) turn
    assert s1.thread_id == "T1"
    # A PERSISTENT thread (ephemeral=False) was opened exactly once.
    assert fake.threads_started == [("T1", False)]


def test_second_call_is_delta_on_the_same_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    with stateful_scope("s"):
        s1 = _resolve(_m("a", "b"), "AB")
        s2 = _resolve(_m("a", "b", "c"), "ABC")
    assert s2.mode == "delta"
    assert s2.reason is None
    assert s2.thread_id == s1.thread_id  # SAME persistent thread
    assert s2.prompt == _ser(_m("c"))  # ONLY the appended tail
    assert s2.delta_chars == len(_ser(_m("c")))
    # No SECOND thread opened — the delta continued the existing one.
    assert fake.threads_started == [("T1", False)]


def test_divergent_prefix_restarts_full_prefix_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # SABOTAGE (a): a length-only prefix check would mis-classify this LONGER, divergent-
    # interior list as a delta (its tail). new is longer than prior AND diverges at an
    # interior index, so only a true byte-compare declines it -> full prefix_mismatch.
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    new = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "ZZ"},
        {"role": "user", "content": "c"},
    ]
    with stateful_scope("s"):
        _resolve(_m("a", "b"), "AB")
        s2 = _resolve(new, "AZZC")
    assert s2.mode == "full"
    assert s2.reason == "prefix_mismatch"
    assert s2.prompt == "AZZC"  # the full prompt, not a bogus delta tail
    # A fresh persistent thread was opened for the restart.
    assert fake.threads_started == [("T1", False), ("T2", False)]
    assert s2.thread_id == "T2"


# --------------------------------------------------------------------------- #
# 2. Reset-on-respawn: a changed pool process forces a session_evicted restart.
# --------------------------------------------------------------------------- #
def test_process_respawn_forces_session_evicted_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    # SABOTAGE (b): drop the peek_extra/mark_reset respawn guard and s2 becomes a delta
    # sent to a DEAD thread on the old (evicted) process -> this pin goes red.
    fake1 = FakeProcess()
    pool = _install_pool(monkeypatch, fake1)
    with stateful_scope("s"):
        s1 = _resolve(_m("a", "b"), "AB")
        assert s1.thread_id == "T1"
        # The pool respawned: the same key now yields a FRESH process (old thread dead).
        fake2 = FakeProcess()
        pool.set_process(fake2)
        s2 = _resolve(_m("a", "b", "c"), "ABC")  # a valid extension, but the process changed
    assert s2.mode == "full"
    assert s2.reason == "session_evicted"
    assert s2.process is fake2
    assert s2.thread_id == "T1"  # a fresh thread on the NEW process (its own counter)
    assert fake2.threads_started == [("T1", False)]


# --------------------------------------------------------------------------- #
# 3. Typed resets: provider_error (note_error) and ops_reset (mark_reset).
# --------------------------------------------------------------------------- #
def test_note_error_forces_provider_error_full(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    with stateful_scope("s"):
        s1 = _resolve(_m("a", "b"), "AB")
        assert s1.mode == "full" and s1.reason == "first_call"
        s1.note_error()  # a mid-flight send failure drops the poisoned thread
        s2 = _resolve(_m("a", "b", "c"), "ABC")
    assert s2.mode == "full"
    assert s2.reason == "provider_error"


def test_ops_reset_forces_full_even_on_a_valid_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    with stateful_scope("s"):
        _resolve(_m("a", "b"), "AB")
        _resolve(_m("a", "b", "c"), "ABC")  # a delta
        codex_stateful_registry().mark_reset("s", "ops_reset")  # an ARC compaction hook
        s3 = _resolve(_m("a", "b", "c", "d"), "ABCD")
    assert s3.mode == "full"
    assert s3.reason == "ops_reset"


def test_lru_eviction_flags_session_evicted(monkeypatch: pytest.MonkeyPatch) -> None:
    # thread evicted mid-run: a bounded registry drops the LRU scope's thread ref while
    # it is STILL live (two parallel experts, scopes held simultaneously — nested here).
    monkeypatch.setenv("CLIO_CODEX_STATEFUL_CAPACITY", "1")
    codex_stateful_registry().reset_for_tests()  # re-read capacity=1
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    with stateful_scope("A"):
        _resolve(_m("a"), "A")  # opens entry A
        with stateful_scope("B"):
            _resolve(_m("b"), "B")  # opens entry B -> evicts A (capacity 1), flags A
        # back in scope A, whose entry was evicted while live.
        sA = _resolve(_m("a", "a2"), "AA2")
    assert sA.mode == "full"
    assert sA.reason == "session_evicted"


# --------------------------------------------------------------------------- #
# 4. Inertness (flag OFF / no scope) — the classic-contract pins (SABOTAGE c).
# --------------------------------------------------------------------------- #
def test_inert_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cst, "codex_stateful_delta_enabled", lambda: False)
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    with stateful_scope("s"):
        s = _resolve(_m("a", "b"), "AB")
    assert s.engaged is False
    assert s.mode == "full"
    assert s.prompt == "AB"  # the full prompt, unchanged
    assert s.thread_id == ""
    assert s.process is None
    # The pool/registry is NEVER touched on the inert path.
    assert fake.threads_started == []
    assert codex_stateful_registry().live_count == 0


def test_inert_without_a_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    # No active scope (classic loop) -> inert even with the flag on.
    s = _resolve(_m("a", "b"), "AB")
    assert s.engaged is False
    assert s.prompt == "AB"
    assert fake.threads_started == []


def test_call_id_is_minted_per_call_engaged_and_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    with stateful_scope("s"):
        engaged = _resolve(_m("a", "b"), "AB")
    monkeypatch.setattr(cst, "codex_stateful_delta_enabled", lambda: False)
    inert = _resolve(_m("a", "b"), "AB")
    assert engaged.call_id and inert.call_id
    assert engaged.call_id != inert.call_id  # one fresh id per LM call


# --------------------------------------------------------------------------- #
# 5. Transport routing: engaged -> run_turn_on_thread; inert -> run_turn.
# --------------------------------------------------------------------------- #
def test_events_engaged_continues_the_persistent_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    send = CodexStatefulSend(
        prompt="DELTA",
        thread_id="T1",
        mode="delta",
        reason=None,
        delta_chars=5,
        engaged=True,
        process=fake,
        session_key=("s", "m", None, "low"),
        scope_token="s",
        call_id="cid",
    )
    events = list(
        cs._app_server_events(
            prompt="DELTA", model="m", cwd=None, effort="low", timeout=5.0, send=send
        )
    )
    assert [e.kind for e in events] == ["final"]
    assert fake.turns == [("T1", "DELTA", "low")]  # continued the thread with the delta
    assert fake.ephemeral_prompts == []  # NEVER opened a fresh ephemeral thread


def test_events_inert_uses_fresh_ephemeral_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # SABOTAGE (c): if _app_server_events routed an inert send to run_turn_on_thread,
    # ephemeral_prompts would be empty and turns non-empty -> this pin goes red.
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    events = list(
        cs._app_server_events(prompt="FULL", model="m", cwd=None, effort=None, timeout=5.0, send=None)
    )
    assert [e.kind for e in events] == ["final"]
    assert fake.ephemeral_prompts == ["FULL"]  # the byte-identical ephemeral path
    assert fake.turns == []


# --------------------------------------------------------------------------- #
# 6. Scope teardown releases the codex leg (the shared #900 seam).
# --------------------------------------------------------------------------- #
def test_scope_exit_releases_codex_registry_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess()
    _install_pool(monkeypatch, fake)
    with stateful_scope("s"):
        _resolve(_m("a", "b"), "AB")
        assert codex_stateful_registry().live_count == 1
    # The one scope the ReActV2 loop binds releases the codex leg on exit.
    assert codex_stateful_registry().live_count == 0
