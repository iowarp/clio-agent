"""Tests for the per-RPC stall (failure-to-progress) guard (#948 S4).

The incident: a ZOMBIE clio-core daemon (RPC port still ACCEPTS connections while its
runtime is internally dead) makes a native op CONNECT and then never return, freezing
the caller (the event loop) forever. The #892 socket-probe gate cannot catch this — the
socket is alive — so this guard watches for FAILURE TO PROGRESS at the individual RPC
level: a call that returns nothing within ``stall_after_s`` is a stalled peer. It is
NOT an absolute/wall-clock bound: a call that responds (even just under the window) on a
later attempt SUCCEEDS — long-running work is never killed for duration.

Hermetic: the "stalled peer" is a callable that blocks on an Event (no daemon, no socket
double needed). Sabotage anchors are noted per-test — dropping the reconnect step or the
typed degrade turns the corresponding assertion red.
"""

from __future__ import annotations

import threading

import pytest

from clio_agent.arc.clio_core_liveness import ClioCoreRuntimeLostError, LivenessGate
from clio_agent.arc.rpc_liveness import (
    RPC_STALLED_REASON,
    LivenessPolicy,
    call_with_liveness,
    guard_store_op,
    resolve_liveness_policy,
)
from clio_agent.arc.storage import ClioCoreStore
from clio_agent.errors import format_error_response

# A fast policy so the stall window is a fraction of a second under test.
_FAST = LivenessPolicy(stall_after_s=0.05, retries=2, backoff_initial_s=0.0, backoff_max_s=0.0)
_NO_SLEEP = lambda _s: None  # noqa: E731 - trivial test seam


class _StallingCall:
    """A callable that STALLS (blocks) on its first ``stall_attempts`` invocations, then
    returns ``result``. Mirrors a zombie RPC: the stalled invocations are abandoned by
    the watcher (daemon threads) and released at teardown so nothing leaks past the test.
    """

    def __init__(self, stall_attempts: int, result: str = "ok") -> None:
        self.stall_attempts = stall_attempts
        self.result = result
        self.calls = 0
        self._lock = threading.Lock()
        self._release = threading.Event()

    def __call__(self) -> str:
        with self._lock:
            self.calls += 1
            n = self.calls
        if n <= self.stall_attempts:
            self._release.wait(10.0)  # abandoned by the watcher; released at teardown
            raise RuntimeError("released after abandonment (never observed by the caller)")
        return self.result

    def release(self) -> None:
        self._release.set()


@pytest.fixture
def released():
    """Release any abandoned stalling threads after the test."""
    calls: list[_StallingCall] = []
    yield calls
    for c in calls:
        c.release()


# --------------------------------------------------------------------------- #
# stall -> reconnect(N) -> typed degrade  (primary incident fix + sabotage anchor)
# --------------------------------------------------------------------------- #


def test_stalled_rpc_retries_with_reconnect_then_typed_degrade(released):
    """Every attempt stalls: the ladder reconnects before each retry and, exhausted,
    raises the typed ``arc_runtime_lost`` degrade (reason ``clio_core_rpc_stalled``).

    Sabotage: delete the ``_reconnect_before_retry`` call and ``reconnects`` stays 0;
    delete the final ``raise`` and this ``pytest.raises`` fails."""
    stall = _StallingCall(stall_attempts=99)  # never progresses
    released.append(stall)
    reconnects = {"n": 0}
    exhausted = {"reason": ""}

    def reconnect() -> None:
        reconnects["n"] += 1

    with pytest.raises(ClioCoreRuntimeLostError) as exc:
        call_with_liveness(
            stall,
            op_name="get",
            port=9413,
            reconnect=reconnect,
            policy=_FAST,
            on_exhausted=lambda r: exhausted.__setitem__("reason", r),
            _sleep=_NO_SLEEP,
        )
    payload = format_error_response(exc.value)
    assert payload["error"] == "arc_runtime_lost"
    assert payload["details"]["reason"] == RPC_STALLED_REASON
    assert payload["details"]["attempts"] == 3  # retries=2 -> 3 attempts
    # RECONNECT ran before each of the 2 retries (a zombie socket is never reused).
    assert reconnects["n"] == 2
    assert exhausted["reason"] == RPC_STALLED_REASON  # store quarantines on this


def test_reconnect_reconstructs_the_client_each_retry(released):
    """The reconnect seam is the client-rebuild; count the reconstructions == retries."""
    stall = _StallingCall(stall_attempts=99)
    released.append(stall)
    rebuilt: list[int] = []
    with pytest.raises(ClioCoreRuntimeLostError):
        call_with_liveness(
            stall,
            op_name="scan",
            port=9413,
            reconnect=lambda: rebuilt.append(1),
            policy=_FAST,
            _sleep=_NO_SLEEP,
        )
    assert sum(rebuilt) == _FAST.retries  # one rebuild before every retry


# --------------------------------------------------------------------------- #
# NOT an absolute kill: a call that responds on a later attempt SUCCEEDS
# --------------------------------------------------------------------------- #


def test_slow_call_that_progresses_on_retry_is_not_killed(released):
    """Attempt 1 stalls; after a reconnect, attempt 2 responds -> SUCCESS.

    Proves the guard bounds a STALL, not the total duration: there is no absolute
    wall-clock kill carried across attempts — a peer that starts responding recovers."""
    call = _StallingCall(stall_attempts=1, result="value-after-reconnect")
    released.append(call)
    reconnects = {"n": 0}
    result = call_with_liveness(
        call,
        op_name="get",
        port=9413,
        reconnect=lambda: reconnects.__setitem__("n", reconnects["n"] + 1),
        policy=_FAST,
        _sleep=_NO_SLEEP,
    )
    assert result == "value-after-reconnect"
    assert reconnects["n"] == 1  # exactly one reconnect, before the successful retry


def test_healthy_call_returns_immediately_without_reconnect():
    """A call that completes within the window returns its value; no reconnect, no raise."""
    reconnects = {"n": 0}
    out = call_with_liveness(
        lambda: 42,
        op_name="exists",
        port=9413,
        reconnect=lambda: reconnects.__setitem__("n", 1),
        policy=_FAST,
        _sleep=_NO_SLEEP,
    )
    assert out == 42
    assert reconnects["n"] == 0


def test_real_rpc_error_propagates_and_is_not_a_stall():
    """A call that RAISES completed (it responded) — the error propagates unmodified and
    is NOT retried as a stall (reconnect stays untouched)."""
    reconnects = {"n": 0}

    def boom():
        raise RuntimeError("PutBlob operation failed (rc=13)")

    with pytest.raises(RuntimeError, match="rc=13"):
        call_with_liveness(
            boom,
            op_name="put",
            port=9413,
            reconnect=lambda: reconnects.__setitem__("n", 1),
            policy=_FAST,
            _sleep=_NO_SLEEP,
        )
    assert reconnects["n"] == 0  # a real error is not a stall


# --------------------------------------------------------------------------- #
# store integration: a stalling native op degrades typed + quarantines
# --------------------------------------------------------------------------- #


class _StallingCte:
    """A fake ``clio_cte_core_ext`` whose Tag.GetBlobSize STALLS on the first store op,
    then (after the store reconnect swaps it out) a healthy replacement is used."""

    def __init__(self, stall: _StallingCall) -> None:
        self._stall = stall

    def Tag(self, kind):  # noqa: N802 - mirrors the native API
        stall = self._stall

        class _Tag:
            def GetBlobSize(self, name):  # noqa: N802
                stall()  # blocks on the first invocation
                return 0

        return _Tag()


def _stalling_store(cte, *, reconnect) -> ClioCoreStore:
    store = ClioCoreStore.__new__(ClioCoreStore)
    store._cte = cte
    store._client = object()
    store._config_path = ""
    store._log_level = "error"
    store._gate = LivenessGate(config_path="", probe=lambda _p: True, ttl_s=100.0)
    store._reconnect = reconnect  # type: ignore[method-assign]
    return store


def test_store_get_stall_degrades_typed_and_quarantines(monkeypatch, released):
    """A real ClioCoreStore.get whose native op never returns degrades to the typed
    error and QUARANTINES the gate (so the next op fails fast, not another full ladder).

    Sabotage: remove ``on_exhausted=self._gate.note_rpc_stalled`` from the store wiring
    and the ``quarantined`` assertion fails."""
    # Force the fast policy for the store path (no real config resolution / long waits).
    monkeypatch.setattr("clio_agent.arc.rpc_liveness.resolve_liveness_policy", lambda: _FAST)
    monkeypatch.setattr("clio_agent.arc.rpc_liveness.time.sleep", _NO_SLEEP)
    stall = _StallingCall(stall_attempts=99)
    released.append(stall)
    reconnects = {"n": 0}
    store = _stalling_store(
        _StallingCte(stall), reconnect=lambda: reconnects.__setitem__("n", reconnects["n"] + 1)
    )
    with pytest.raises(ClioCoreRuntimeLostError) as exc:
        store.get("conversations", "sess-x")
    assert exc.value.details["reason"] == RPC_STALLED_REASON
    assert reconnects["n"] == _FAST.retries  # reconnected before each retry
    assert store._gate.quarantined is True  # next op fails fast via _live()


def test_guard_store_op_gate_blocks_dead_socket_before_stall_watch():
    """The decorator runs the cheap socket gate FIRST: a dead probe raises typed BEFORE
    a worker thread is ever spawned (no stall window paid for a plainly-dead daemon)."""

    class _Store:
        def __init__(self):
            self._gate = LivenessGate(config_path="", probe=lambda _p: False, ttl_s=0.0)
            self.reached = False

        def _live(self):
            self._gate.ensure_live(self._reconnect)

        def _reconnect(self):
            raise RuntimeError("daemon down")

        @guard_store_op("get")
        def get(self):
            self.reached = True
            return "should-not-reach"

    store = _Store()
    with pytest.raises(ClioCoreRuntimeLostError):
        store.get()
    assert store.reached is False


# --------------------------------------------------------------------------- #
# config round-trip: file -> env -> default (#985) + doc regeneration
# --------------------------------------------------------------------------- #


def test_policy_defaults(monkeypatch):
    for var in (
        "CLIO_ARC_LIVENESS_STALL_AFTER_S",
        "CLIO_ARC_LIVENESS_RETRIES",
        "CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S",
        "CLIO_ARC_LIVENESS_BACKOFF_MAX_S",
    ):
        monkeypatch.delenv(var, raising=False)
    from clio_agent import conf

    conf.reload()
    policy = resolve_liveness_policy()
    assert (policy.stall_after_s, policy.retries) == (30.0, 3)
    assert (policy.backoff_initial_s, policy.backoff_max_s) == (2.0, 15.0)


def test_env_override_and_bad_value_fallback(monkeypatch):
    from clio_agent import conf

    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "7.5")
    monkeypatch.setenv("CLIO_ARC_LIVENESS_RETRIES", "5")
    conf.reload()
    policy = resolve_liveness_policy()
    assert policy.stall_after_s == 7.5
    assert policy.retries == 5
    # A non-positive stall window must NOT silently disable the guard — it fails safe.
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "-1")
    conf.reload()
    assert resolve_liveness_policy().stall_after_s == 30.0


def test_env_reference_documents_the_knobs():
    """The four knobs are AST-discovered into docs/ENVIRONMENT.md (drift-guarded)."""
    from scripts.gen_env_reference import generate

    markdown, _dotenv = generate()
    for var in (
        "CLIO_ARC_LIVENESS_STALL_AFTER_S",
        "CLIO_ARC_LIVENESS_RETRIES",
        "CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S",
        "CLIO_ARC_LIVENESS_BACKOFF_MAX_S",
    ):
        assert var in markdown
