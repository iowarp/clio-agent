"""Tests for the clio-core daemon liveness gate + quarantine (#892).

The host access violation (0xC0000005) a dead daemon triggers on the next native
op CANNOT be unit-tested directly — it crashes the interpreter. Instead we simulate
daemon loss by injecting the liveness PROBE and assert the gate raises the typed
``ClioCoreRuntimeLostError`` *before* the native binding is ever reached. The ClioCoreStore
wiring is exercised on a REAL ``ClioCoreStore`` built without the native runtime (its
attributes injected) so the assertions are on the shipped object, not a mock of it.

Sabotage note: the "probe-dead" tests assert the native sentinel is NEVER reached.
Remove ``self._live()`` from an op (or the ``ensure_live`` raise) and that op reaches
the sentinel, which raises ``_SentinelReached`` instead of ``ClioCoreRuntimeLostError`` —
the test goes red. Verified manually while authoring (see the module docstring test).
"""

from __future__ import annotations

import pytest

import clio_agent.arc.clio_core_liveness as clio_core_liveness
from clio_agent.arc.clio_core_liveness import (
    RPC_STALLED_REASON,
    ClioCoreRuntimeLostError,
    LivenessGate,
    liveness_snapshot,
)
from clio_agent.arc.storage import ClioCoreStore
from clio_agent.errors import ClioError, format_error_response
from clio_agent.runtime.clio_core_health import probe_clio_core_liveness
from clio_agent.runtime.status import IntegrationState


class _SentinelReached(Exception):
    """Raised iff a native CTE call is reached — proves the gate did NOT block."""


class _RecordingTag:
    """A fake CTE Tag whose every method records the native call it stands for."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def GetBlobSize(self, name: str) -> int:  # noqa: N802 - mirrors the native API
        self._calls.append("GetBlobSize")
        return 0

    def GetContainedBlobs(self):  # noqa: N802 - mirrors the native API
        self._calls.append("GetContainedBlobs")
        return []

    def GetTagId(self):  # noqa: N802 - mirrors the native API
        self._calls.append("GetTagId")
        return 1

    def PutBlob(self, *a, **k):  # noqa: N802 - mirrors the native API
        self._calls.append("PutBlob")


class _RecordingClioCore:
    """A fake ``clio_cte_core_ext`` module: records that a native op was reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def Tag(self, kind: str) -> _RecordingTag:  # noqa: N802 - mirrors the native API
        self.calls.append(f"Tag({kind})")
        return _RecordingTag(self.calls)

    def get_cte_client(self):
        self.calls.append("get_cte_client")
        return object()


class _SentinelClioCore:
    """A fake native module that EXPLODES if reached — the gate must block first."""

    def Tag(self, kind: str):  # noqa: N802 - mirrors the native API
        raise _SentinelReached(f"native CTE Tag({kind}) reached past the gate")

    def get_cte_client(self):
        raise _SentinelReached("native get_cte_client reached past the gate")


def _bare_clio_core_store(cte, *, probe, ttl_s=0.0, reconnect=None) -> ClioCoreStore:
    """A REAL ``ClioCoreStore`` with the native runtime bypassed and a gate injected.

    ``__init__`` connects to the shared daemon, which we must not do in a unit test,
    so the instance is built via ``__new__`` and its attributes injected — the ARC-op
    methods under test are the real, shipped ones.
    """
    store = ClioCoreStore.__new__(ClioCoreStore)
    store._cte = cte
    store._client = object()
    store._config_path = ""
    store._log_level = "error"
    store._gate = LivenessGate(config_path="", probe=probe, ttl_s=ttl_s)
    if reconnect is not None:
        store._reconnect = reconnect  # type: ignore[method-assign]
    return store


# --------------------------------------------------------------------------- #
# healthy pass-through
# --------------------------------------------------------------------------- #


def test_healthy_store_op_passes_through_to_native():
    """A live probe lets the op reach the native binding (baseline behaviour)."""
    cte = _RecordingClioCore()
    store = _bare_clio_core_store(cte, probe=lambda _port: True)
    assert store.get("conversations", "s1") is None  # native GetBlobSize == 0
    assert cte.calls == ["Tag(conversations)", "GetBlobSize"]  # native WAS reached


# --------------------------------------------------------------------------- #
# probe-dead: typed raise BEFORE the native call (sabotage anchor)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("get", ("conversations", "s1")),
        ("put", ("conversations", "s1", b"x")),
        ("exists", ("conversations", "s1")),
        ("delete", ("conversations", "s1")),
        ("clear", ()),
        ("search", ("conversations", "q")),
    ],
)
def test_probe_dead_raises_before_native_binding(op, args):
    """A dead daemon quarantines and raises typed BEFORE touching the sentinel."""
    store = _bare_clio_core_store(_SentinelClioCore(), probe=lambda _port: False)
    with pytest.raises(ClioCoreRuntimeLostError) as exc:  # NOT _SentinelReached
        getattr(store, op)(*args)
    assert exc.value.error_type == "arc_runtime_lost"
    assert store._gate.quarantined is True


def test_scan_dead_raises_before_native_binding():
    """scan() is a generator; iterating a dead store raises typed, not _SentinelReached."""
    store = _bare_clio_core_store(_SentinelClioCore(), probe=lambda _port: False)
    with pytest.raises(ClioCoreRuntimeLostError):
        list(store.scan("conversations"))
    assert store._gate.quarantined is True


# --------------------------------------------------------------------------- #
# typed error surface
# --------------------------------------------------------------------------- #


def test_clio_core_runtime_lost_is_structured_clio_error():
    """The error is a ClioError that serializes to a typed, attributable payload."""
    err = ClioCoreRuntimeLostError("gone", reason="clio_core_daemon_not_listening", port=9413)
    assert isinstance(err, ClioError)
    payload = format_error_response(err)
    assert payload["error"] == "arc_runtime_lost"
    assert payload["details"]["reason"] == "clio_core_daemon_not_listening"
    assert payload["details"]["port"] == 9413
    assert "recovery_actions" in payload["details"]


# --------------------------------------------------------------------------- #
# TTL caching: the probe is not called per-op
# --------------------------------------------------------------------------- #


def test_ttl_caches_probe_across_ops():
    """Within the TTL, a confirmed-live probe is reused — not re-run every op."""
    calls = {"n": 0}

    def probe(_port):
        calls["n"] += 1
        return True

    cte = _RecordingClioCore()
    store = _bare_clio_core_store(cte, probe=probe, ttl_s=100.0)
    for _ in range(5):
        store.get("conversations", "s1")
    assert calls["n"] == 1  # one probe, cached for the rest


# --------------------------------------------------------------------------- #
# recovery: reconnect succeeds -> leave quarantine; fails -> stay quarantined
# --------------------------------------------------------------------------- #


def test_reconnect_recovers_and_leaves_quarantine():
    """probe dead -> quarantine+raise; probe back + reconnect ok -> next op passes."""
    # Probe: dead on the first op (quarantine), alive thereafter (recovery + steady).
    seq = iter([False, True, True, True])
    reconnected = {"n": 0}

    def reconnect():
        reconnected["n"] += 1

    gate = LivenessGate(config_path="", probe=lambda _p: next(seq), ttl_s=0.0)

    with pytest.raises(ClioCoreRuntimeLostError):
        gate.ensure_live(reconnect)  # first op: dead -> quarantine
    assert gate.quarantined is True

    gate.ensure_live(reconnect)  # next op: quarantined -> one reconnect attempt -> ok
    assert gate.quarantined is False
    assert reconnected["n"] == 1


def test_reconnect_failure_stays_quarantined_and_typed():
    """A reconnect that raises keeps the store quarantined and re-raises typed."""

    def reconnect():
        raise RuntimeError("daemon never rebound the port")

    gate = LivenessGate(config_path="", probe=lambda _p: False, ttl_s=0.0)
    with pytest.raises(ClioCoreRuntimeLostError):
        gate.ensure_live(reconnect)  # dead -> quarantine
    with pytest.raises(ClioCoreRuntimeLostError) as exc:
        gate.ensure_live(reconnect)  # quarantined -> reconnect raises -> stay quarantined
    assert exc.value.details["reason"] == "clio_core_reconnect_failed"
    assert gate.quarantined is True


def test_reconnect_is_rate_limited_within_ttl():
    """Two quarantined ops inside one TTL make at most one reconnect attempt."""
    attempts = {"n": 0}

    def reconnect():
        attempts["n"] += 1
        raise RuntimeError("still down")

    gate = LivenessGate(config_path="", probe=lambda _p: False, ttl_s=100.0)
    with pytest.raises(ClioCoreRuntimeLostError):
        gate.ensure_live(reconnect)  # dead -> quarantine (no reconnect this op)
    with pytest.raises(ClioCoreRuntimeLostError):
        gate.ensure_live(reconnect)  # reconnect attempt #1
    with pytest.raises(ClioCoreRuntimeLostError) as exc:
        gate.ensure_live(reconnect)  # within TTL -> back-off, NO new attempt
    assert attempts["n"] == 1
    assert exc.value.details["reason"] == "clio_core_reconnect_backoff"


# --------------------------------------------------------------------------- #
# reason-aware recovery: an rpc_stalled (ZOMBIE) quarantine needs an RPC-LEVEL probe
# --------------------------------------------------------------------------- #


def test_rpc_stalled_recovery_requires_rpc_probe_not_socket(monkeypatch):
    """The blocker fix: a quarantine set by the stall ladder (reason=rpc_stalled) is a
    ZOMBIE whose socket still ACCEPTS. Recovery must NOT dissolve it on the socket probe
    / a plain reconnect (which a zombie passes); it recovers ONLY when a real RPC-level
    probe answers, and re-probes are rate-limited so the intervening ops fail fast.

    Sabotage: route the rpc_stalled branch back through ``reconnect`` (socket-gated) and
    the very first quarantined op un-quarantines (``gate.quarantined`` after step 2
    flips to False) — this test goes red.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(clio_core_liveness.time, "monotonic", lambda: clock["t"])
    # probe=True => the SOCKET always accepts (the zombie's defining property).
    gate = LivenessGate(config_path="", probe=lambda _p: True, ttl_s=0.5)
    probe = {"n": 0, "healthy": False}
    socket_reconnects = {"n": 0}

    def rpc_probe() -> bool:
        probe["n"] += 1
        return probe["healthy"]

    def reconnect() -> None:  # the SOCKET-gated seam; must NOT be used for a zombie
        socket_reconnects["n"] += 1

    # Ladder exhausted -> quarantine (arms the recovery clock so the next op is spaced).
    gate.note_rpc_stalled()
    assert gate.quarantined is True

    # SECOND op, immediately: rate-limited -> fails fast typed, NO probe, NO socket reconnect.
    with pytest.raises(ClioCoreRuntimeLostError) as exc:
        gate.ensure_live(reconnect, rpc_probe=rpc_probe)
    assert exc.value.details["reason"] == "clio_core_reconnect_backoff"
    assert probe["n"] == 0
    assert socket_reconnects["n"] == 0
    assert gate.quarantined is True

    # Past the (raised) rpc_stalled recovery TTL: an RPC probe runs; the zombie still
    # hangs -> probe returns False -> STAY quarantined, and the socket seam is untouched.
    clock["t"] += 31.0
    with pytest.raises(ClioCoreRuntimeLostError) as exc2:
        gate.ensure_live(reconnect, rpc_probe=rpc_probe)
    assert exc2.value.details["reason"] == RPC_STALLED_REASON
    assert probe["n"] == 1
    assert socket_reconnects["n"] == 0  # socket-probe recovery is NEVER used for a zombie
    assert gate.quarantined is True

    # Daemon comes back healthy: the next spaced RPC probe answers -> leave quarantine.
    probe["healthy"] = True
    clock["t"] += 31.0
    gate.ensure_live(reconnect, rpc_probe=rpc_probe)  # no raise
    assert probe["n"] == 2
    assert gate.quarantined is False


def test_rpc_stalled_recovery_without_probe_stays_quarantined(monkeypatch):
    """No ``rpc_probe`` supplied => an rpc_stalled quarantine cannot be left (a socket
    reconnect a zombie passes must never silently dissolve it)."""
    clock = {"t": 500.0}
    monkeypatch.setattr(clio_core_liveness.time, "monotonic", lambda: clock["t"])
    gate = LivenessGate(config_path="", probe=lambda _p: True, ttl_s=0.5)
    gate.note_rpc_stalled()
    clock["t"] += 31.0  # past the recovery back-off
    with pytest.raises(ClioCoreRuntimeLostError) as exc:
        gate.ensure_live(lambda: None)  # no rpc_probe
    assert exc.value.details["reason"] == RPC_STALLED_REASON
    assert gate.quarantined is True


def test_socket_loss_quarantine_still_recovers_via_reconnect():
    """A socket-loss quarantine (reason=daemon_not_listening) keeps today's socket-probe
    recovery: a successful reconnect leaves quarantine (reason-aware, other branch)."""
    seq = iter([False, True, True, True])
    reconnects = {"n": 0}
    probe_calls = {"n": 0}

    def rpc_probe() -> bool:  # must NOT be consulted for a socket-loss quarantine
        probe_calls["n"] += 1
        return True

    gate = LivenessGate(config_path="", probe=lambda _p: next(seq), ttl_s=0.0)
    with pytest.raises(ClioCoreRuntimeLostError):
        gate.ensure_live(
            lambda: reconnects.__setitem__("n", reconnects["n"] + 1), rpc_probe=rpc_probe
        )
    assert gate.quarantined is True
    gate.ensure_live(lambda: reconnects.__setitem__("n", reconnects["n"] + 1), rpc_probe=rpc_probe)
    assert gate.quarantined is False
    assert reconnects["n"] == 1
    assert probe_calls["n"] == 0  # the RPC probe is only for rpc_stalled quarantines


# --------------------------------------------------------------------------- #
# doctor visibility
# --------------------------------------------------------------------------- #


def test_doctor_reports_quarantined_gate():
    rows = probe_clio_core_liveness(
        snapshot=[{"quarantined": True, "reason": "clio_core_daemon_not_listening", "port": 9413}]
    )
    assert len(rows) == 1
    assert rows[0].state is IntegrationState.DEGRADED
    assert rows[0].details["reason"] == "clio_core_store_quarantined"


def test_doctor_reports_healthy_gate():
    rows = probe_clio_core_liveness(snapshot=[{"quarantined": False, "reason": "", "port": 9413}])
    assert len(rows) == 1
    assert rows[0].state is IntegrationState.READY


def test_doctor_no_gate_no_row():
    assert probe_clio_core_liveness(snapshot=[]) == []


def test_live_gate_registers_in_snapshot():
    """A constructed gate is visible to the process-local registry the doctor reads."""
    gate = LivenessGate(config_path="", probe=lambda _p: True, ttl_s=0.0)
    ports = [g["port"] for g in liveness_snapshot()]
    assert gate.port in ports


# --------------------------------------------------------------------------- #
# gact/ARC degradation flow: raise loudly, no swallow, no LocalFS fallback
# --------------------------------------------------------------------------- #


def test_arc_memory_propagates_quarantine_loudly_without_fallback(tmp_path):
    """The ARC seam gact uses re-raises the typed error — not swallow, not fallback.

    ``ARCMemory.get_conversation`` reads through ``store.get`` (the exact op a turn
    runs to load history); a quarantined clio-core store raises ``ClioCoreRuntimeLostError``,
    which propagates unswallowed. The turn's broad-except envelope then turns that
    into a structured error turn (a ClioError, never a raw traceback / 500), and the
    backend is NOT silently swapped to LocalFS — ``_store`` stays the quarantined clio-core store.
    """
    from clio_agent.arc.memory import ARCMemory

    store = _bare_clio_core_store(_SentinelClioCore(), probe=lambda _port: False)
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    with pytest.raises(ClioCoreRuntimeLostError) as exc:
        arc.get_conversation("sess-x")
    # structured + attributable (reaches the trace as a typed reason, not internal_error)
    assert format_error_response(exc.value)["error"] == "arc_runtime_lost"
    assert arc._store is store  # no silent LocalFS fallback
    assert store._gate.quarantined is True
