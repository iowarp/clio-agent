"""Local socket failures must not strand healthy clients or authorize replacements."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc import clio_core_liveness as liveness


def _unavailable(_port: int) -> bool:
    raise liveness.ClioCoreRuntimeLostError(
        "Local socket allocation failed", reason=liveness.SOCKET_PROBE_UNAVAILABLE_REASON
    )


def _forbidden() -> Any:
    raise AssertionError("Do not rebuild native bindings or probe a changed daemon")


@pytest.mark.parametrize("error_number", [errno.EADDRINUSE, errno.EACCES, errno.ENOBUFS, None])
def test_inconclusive_probe_is_typed_not_dead(
    monkeypatch: pytest.MonkeyPatch, error_number: int | None
) -> None:
    """Resource/permission failures and timeouts are not refused connections."""
    error = OSError(error_number, "local failure") if error_number else TimeoutError("timed out")
    monkeypatch.setattr(liveness, "_listener_present", lambda *_a, **_k: _raise(error))
    with pytest.raises(liveness.ClioCoreRuntimeLostError) as raised:
        liveness._runtime_alive(9413)
    assert raised.value.details["reason"] == liveness.SOCKET_PROBE_UNAVAILABLE_REASON
    assert raised.value.__cause__ is error


def _raise(error: Exception) -> Any:
    raise error


def test_refused_connection_still_proves_socket_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirmed refusal retains the fail-before-native-access path."""
    monkeypatch.setattr(
        liveness,
        "_listener_present",
        lambda *_a, **_k: _raise(ConnectionRefusedError(errno.ECONNREFUSED, "refused")),
    )
    assert liveness._runtime_alive(9413) is False


@pytest.mark.parametrize("auto_recover", [False, True])
def test_local_probe_failure_uses_verified_original_rpc(
    monkeypatch: pytest.MonkeyPatch, auto_recover: bool
) -> None:
    """A same-call RPC answer can establish liveness without socket allocation."""
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _cfg: (101, 123.5))
    gate = liveness.LivenessGate(
        probe=_unavailable, ttl_s=3, auto_recover=auto_recover, attached_identity=(101, 123.5)
    )
    probes: list[str] = []

    def answer() -> bool:
        probes.append("original client")
        return True

    gate.ensure_live(_forbidden, rpc_probe=answer)
    gate.ensure_live(_forbidden, rpc_probe=_forbidden)
    assert probes == ["original client"]
    assert not gate.quarantined


@pytest.mark.parametrize("identity", [None, (102, 123.5), (101, 124.0)])
@pytest.mark.parametrize("auto_recover", [False, True])
def test_inconclusive_probe_cannot_bless_unknown_or_replaced_client(
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[int, float] | None,
    auto_recover: bool,
) -> None:
    """Even an automatically recoverable gate must establish original identity."""
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _cfg: identity)
    gate = liveness.LivenessGate(
        probe=_unavailable, auto_recover=auto_recover, attached_identity=(101, 123.5)
    )
    with pytest.raises(liveness.ClioCoreRuntimeLostError) as raised:
        gate.ensure_live(_forbidden, rpc_probe=_forbidden)
    assert raised.value.details["reason"] == "clio_core_client_restart_required"
    assert gate.quarantined


def test_process_replacement_during_probe_cannot_release_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive RPC is insufficient when creation identity changed meanwhile."""
    identities = iter([(101, 123.5), (102, 124.0)])
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _cfg: next(identities))
    gate = liveness.LivenessGate(probe=_unavailable, attached_identity=(101, 123.5))
    with pytest.raises(liveness.ClioCoreRuntimeLostError):
        gate.ensure_live(_forbidden, rpc_probe=lambda: True)
    assert gate.quarantined


def test_failed_recovery_is_bounded_and_retries_after_normal_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not hammer RPCs or impose the thirty-second zombie delay on a local error."""
    clock = [100.0]
    monkeypatch.setattr(liveness.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _cfg: (101, 123.5))
    gate = liveness.LivenessGate(
        probe=_unavailable, ttl_s=3, auto_recover=False, attached_identity=(101, 123.5)
    )
    with pytest.raises(liveness.ClioCoreRuntimeLostError):
        gate.ensure_live(_forbidden, rpc_probe=lambda: False)
    with pytest.raises(liveness.ClioCoreRuntimeLostError) as raised:
        gate.ensure_live(_forbidden, rpc_probe=_forbidden)
    assert raised.value.details["reason"] == "clio_core_reconnect_backoff"
    clock[0] += 3
    gate.ensure_live(_forbidden, rpc_probe=lambda: True)
    assert not gate.quarantined


def test_missing_rpc_does_not_turn_probe_failure_into_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TCP retry or spawn cannot substitute for an answer from the original client."""
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _cfg: (101, 123.5))
    gate = liveness.LivenessGate(probe=_unavailable, attached_identity=(101, 123.5))
    with pytest.raises(liveness.ClioCoreRuntimeLostError):
        gate.ensure_live(_forbidden)
    assert gate.quarantined


def test_diagnostics_retain_probe_uncertainty_without_raising() -> None:
    """Health reporting must neither crash nor assert that a daemon died."""
    assert liveness.runtime_socket_status(9413, probe=_unavailable) == (
        False,
        liveness.SOCKET_PROBE_UNAVAILABLE_REASON,
    )


def test_health_rows_do_not_misreport_inconclusive_probe_as_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both public runtime rows preserve uncertainty and recommend a safe retry."""
    from clio_agent.runtime.status import IntegrationState, RuntimeProbe

    probe = RuntimeProbe(
        env={},
        module_checker=lambda name: name == "iowarp_core",
        port_checker=_unavailable,
        clio_runtime_dir=tmp_path / "runtime",
    )
    for row in (probe.probe_arc(), probe.probe_clio_core()):
        assert row.state is IntegrationState.DEGRADED
        assert row.details["reason"] == liveness.SOCKET_PROBE_UNAVAILABLE_REASON
        assert "could not establish" in row.summary
        assert "Restart" not in row.next_action


def test_live_diagnostic_probes_report_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach and gate diagnostics must not raise or state a daemon was lost."""
    from clio_agent.arc import clio_core_attach
    from clio_agent.arc.clio_core_attach import ClioCoreAttachPhase, ClioCoreAttachState
    from clio_agent.runtime import clio_core_health as health

    monkeypatch.setattr(liveness, "_runtime_alive", _unavailable)
    monkeypatch.setattr(
        liveness,
        "liveness_snapshot",
        lambda: [{"quarantined": False, "reason": "", "port": 9413}],
    )
    attached = ClioCoreAttachState(ClioCoreAttachPhase.ATTACHED, "attached", "", 9413)
    # The attach probe imports the snapshot at call time from its owner module, so patch
    # there; otherwise the result depends on whatever attach state earlier tests left.
    monkeypatch.setattr(clio_core_attach, "attach_state_snapshot", lambda: attached)
    monkeypatch.setattr(health, "attach_state_snapshot", lambda: attached)
    for rows in (health.probe_clio_core_attach(), health.probe_clio_core_liveness()):
        assert len(rows) == 1
        assert rows[0].details["reason"] == liveness.SOCKET_PROBE_UNAVAILABLE_REASON


def test_quarantined_local_probe_does_not_prescribe_killing_original_daemon() -> None:
    """A retryable original-client check must not tell the person to replace Core."""
    from clio_agent.runtime.clio_core_health import probe_clio_core_liveness

    rows = probe_clio_core_liveness(
        snapshot=[
            {
                "quarantined": True,
                "reason": liveness.SOCKET_PROBE_UNAVAILABLE_REASON,
                "restart_required": False,
                "port": 9413,
            }
        ]
    )
    assert rows[0].details["reason"] == liveness.SOCKET_PROBE_UNAVAILABLE_REASON
    assert "Restart" not in rows[0].next_action


def test_actual_native_client_answers_when_tcp_probe_cannot_allocate_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the real client/daemon and read actual stored bytes after a local failure."""
    from clio_agent.arc.storage import ClioCoreStore, make_arc_store

    store = make_arc_store(backend="cte", namespace="socket-probe-recovery")
    assert isinstance(store, ClioCoreStore)
    store.put("records", "probe-sentinel", b"original native client")
    original_client = store._client
    original_identity = liveness._daemon_identity(store._config_path)
    assert original_identity is not None
    assert store._gate._attached_identity == original_identity
    monkeypatch.setattr(store._gate, "_probe", _unavailable)
    monkeypatch.setattr(store._gate, "_last_ok_at", None)
    assert store.get("records", "probe-sentinel") == b"original native client"
    assert store._client is original_client
    assert liveness._daemon_identity(store._config_path) == original_identity
    assert not store._gate.quarantined
    store.delete("records", "probe-sentinel")


def test_startup_does_not_spawn_or_retain_registration_on_inconclusive_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unavailable local diagnostic cannot authorize a duplicate daemon."""
    from clio_agent.arc import storage
    from tests.test_arc.test_arc_store_factory import _isolate_clio_home

    _isolate_clio_home(monkeypatch, tmp_path)
    monkeypatch.setattr(storage, "_runtime_alive", _unavailable)
    monkeypatch.setattr(storage, "_spawn_runtime_daemon", lambda *_a, **_k: _forbidden())
    with pytest.raises(liveness.ClioCoreRuntimeLostError):
        storage._ensure_runtime_daemon(object(), "", "error")
    assert not storage._client_registered
    assert os.getpid() not in storage._live_client_pids()
