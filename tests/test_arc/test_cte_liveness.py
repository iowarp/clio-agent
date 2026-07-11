"""Tests for the clio-core CTE daemon liveness gate + quarantine (#892).

The host access violation (0xC0000005) a dead daemon triggers on the next native
op CANNOT be unit-tested directly — it crashes the interpreter. Instead we simulate
daemon loss by injecting the liveness PROBE and assert the gate raises the typed
``CTERuntimeLostError`` *before* the native binding is ever reached. The CTEStore
wiring is exercised on a REAL ``CTEStore`` built without the native runtime (its
attributes injected) so the assertions are on the shipped object, not a mock of it.

Sabotage note: the "probe-dead" tests assert the native sentinel is NEVER reached.
Remove ``self._live()`` from an op (or the ``ensure_live`` raise) and that op reaches
the sentinel, which raises ``_SentinelReached`` instead of ``CTERuntimeLostError`` —
the test goes red. Verified manually while authoring (see the module docstring test).
"""

from __future__ import annotations

import pytest

from clio_agent.arc.cte_liveness import (
    CTERuntimeLostError,
    LivenessGate,
    liveness_snapshot,
)
from clio_agent.arc.storage import CTEStore
from clio_agent.errors import ClioError, format_error_response
from clio_agent.runtime.cte_health import probe_cte_liveness
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


class _RecordingCTE:
    """A fake ``clio_cte_core_ext`` module: records that a native op was reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def Tag(self, kind: str) -> _RecordingTag:  # noqa: N802 - mirrors the native API
        self.calls.append(f"Tag({kind})")
        return _RecordingTag(self.calls)

    def get_cte_client(self):
        self.calls.append("get_cte_client")
        return object()


class _SentinelCTE:
    """A fake native module that EXPLODES if reached — the gate must block first."""

    def Tag(self, kind: str):  # noqa: N802 - mirrors the native API
        raise _SentinelReached(f"native CTE Tag({kind}) reached past the gate")

    def get_cte_client(self):
        raise _SentinelReached("native get_cte_client reached past the gate")


def _bare_cte_store(cte, *, probe, ttl_s=0.0, reconnect=None) -> CTEStore:
    """A REAL ``CTEStore`` with the native runtime bypassed and a gate injected.

    ``__init__`` connects to the shared daemon, which we must not do in a unit test,
    so the instance is built via ``__new__`` and its attributes injected — the ARC-op
    methods under test are the real, shipped ones.
    """
    store = CTEStore.__new__(CTEStore)
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
    cte = _RecordingCTE()
    store = _bare_cte_store(cte, probe=lambda _port: True)
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
    store = _bare_cte_store(_SentinelCTE(), probe=lambda _port: False)
    with pytest.raises(CTERuntimeLostError) as exc:  # NOT _SentinelReached
        getattr(store, op)(*args)
    assert exc.value.error_type == "arc_runtime_lost"
    assert store._gate.quarantined is True


def test_scan_dead_raises_before_native_binding():
    """scan() is a generator; iterating a dead store raises typed, not _SentinelReached."""
    store = _bare_cte_store(_SentinelCTE(), probe=lambda _port: False)
    with pytest.raises(CTERuntimeLostError):
        list(store.scan("conversations"))
    assert store._gate.quarantined is True


# --------------------------------------------------------------------------- #
# typed error surface
# --------------------------------------------------------------------------- #


def test_cte_runtime_lost_is_structured_clio_error():
    """The error is a ClioError that serializes to a typed, attributable payload."""
    err = CTERuntimeLostError("gone", reason="cte_daemon_not_listening", port=9413)
    assert isinstance(err, ClioError)
    payload = format_error_response(err)
    assert payload["error"] == "arc_runtime_lost"
    assert payload["details"]["reason"] == "cte_daemon_not_listening"
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

    cte = _RecordingCTE()
    store = _bare_cte_store(cte, probe=probe, ttl_s=100.0)
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

    with pytest.raises(CTERuntimeLostError):
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
    with pytest.raises(CTERuntimeLostError):
        gate.ensure_live(reconnect)  # dead -> quarantine
    with pytest.raises(CTERuntimeLostError) as exc:
        gate.ensure_live(reconnect)  # quarantined -> reconnect raises -> stay quarantined
    assert exc.value.details["reason"] == "cte_reconnect_failed"
    assert gate.quarantined is True


def test_reconnect_is_rate_limited_within_ttl():
    """Two quarantined ops inside one TTL make at most one reconnect attempt."""
    attempts = {"n": 0}

    def reconnect():
        attempts["n"] += 1
        raise RuntimeError("still down")

    gate = LivenessGate(config_path="", probe=lambda _p: False, ttl_s=100.0)
    with pytest.raises(CTERuntimeLostError):
        gate.ensure_live(reconnect)  # dead -> quarantine (no reconnect this op)
    with pytest.raises(CTERuntimeLostError):
        gate.ensure_live(reconnect)  # reconnect attempt #1
    with pytest.raises(CTERuntimeLostError) as exc:
        gate.ensure_live(reconnect)  # within TTL -> back-off, NO new attempt
    assert attempts["n"] == 1
    assert exc.value.details["reason"] == "cte_reconnect_backoff"


# --------------------------------------------------------------------------- #
# doctor visibility
# --------------------------------------------------------------------------- #


def test_doctor_reports_quarantined_gate():
    rows = probe_cte_liveness(
        snapshot=[{"quarantined": True, "reason": "cte_daemon_not_listening", "port": 9413}]
    )
    assert len(rows) == 1
    assert rows[0].state is IntegrationState.DEGRADED
    assert rows[0].details["reason"] == "cte_store_quarantined"


def test_doctor_reports_healthy_gate():
    rows = probe_cte_liveness(snapshot=[{"quarantined": False, "reason": "", "port": 9413}])
    assert len(rows) == 1
    assert rows[0].state is IntegrationState.READY


def test_doctor_no_gate_no_row():
    assert probe_cte_liveness(snapshot=[]) == []


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
    runs to load history); a quarantined CTE store raises ``CTERuntimeLostError``,
    which propagates unswallowed. The turn's broad-except envelope then turns that
    into a structured error turn (a ClioError, never a raw traceback / 500), and the
    backend is NOT silently swapped to LocalFS — ``_store`` stays the quarantined CTE.
    """
    from clio_agent.arc.memory import ARCMemory

    store = _bare_cte_store(_SentinelCTE(), probe=lambda _port: False)
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    with pytest.raises(CTERuntimeLostError) as exc:
        arc.get_conversation("sess-x")
    # structured + attributable (reaches the trace as a typed reason, not internal_error)
    assert format_error_response(exc.value)["error"] == "arc_runtime_lost"
    assert arc._store is store  # no silent LocalFS fallback
    assert store._gate.quarantined is True
