"""Only a verified surviving Core process may recover without rebuilding bindings."""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import clio_core_liveness as liveness
from clio_agent.arc import rpc_liveness


def _gate(monkeypatch: pytest.MonkeyPatch) -> liveness.LivenessGate:
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _config: (101, 123.5))
    gate = liveness.LivenessGate(
        probe=lambda _port: True, ttl_s=0, auto_recover=False, attached_identity=(101, 123.5)
    )
    gate.note_rpc_stalled()
    gate._last_recovery_at = None
    return gate


def test_same_client_probe_recovers_only_after_actual_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No reconnect runs, a failed probe keeps quarantine, and success clears it."""
    gate = _gate(monkeypatch)

    def forbidden_reconnect() -> None:
        raise AssertionError("A paused original daemon must retain its native client")

    with pytest.raises(liveness.ClioCoreRuntimeLostError):
        gate.ensure_live(forbidden_reconnect, rpc_probe=lambda: False)
    assert gate.quarantined
    assert gate.status()["restart_required"] is False
    gate._last_recovery_at = None
    gate.ensure_live(forbidden_reconnect, rpc_probe=lambda: True)
    assert not gate.quarantined


@pytest.mark.parametrize("identity", [None, (101, 124.0), (102, 123.5)])
def test_unknown_replaced_or_reused_daemon_cannot_touch_client(
    identity: tuple[int, float] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listening socket or recycled process ID cannot authorize native access."""
    gate = _gate(monkeypatch)
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _config: identity)

    def forbidden() -> Any:
        raise AssertionError("A changed runtime must neither reconnect nor probe")

    with pytest.raises(liveness.ClioCoreRuntimeLostError) as raised:
        gate.ensure_live(forbidden, rpc_probe=forbidden)
    assert raised.value.details["reason"] == "clio_core_client_restart_required"
    assert gate.quarantined and gate.status()["restart_required"] is True


def test_socket_loss_still_requires_restart_for_same_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The surviving-runtime exception applies only to a recorded RPC stall."""
    gate = _gate(monkeypatch)
    gate._reason = "clio_core_daemon_not_listening"

    def forbidden() -> Any:
        raise AssertionError("Socket loss cannot use the same-client recovery exception")

    with pytest.raises(liveness.ClioCoreRuntimeLostError):
        gate.ensure_live(forbidden, rpc_probe=forbidden)
    assert gate.status()["restart_required"] is True


def test_identity_change_during_probe_cannot_clear_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply from a runtime that disappeared during the probe is insufficient."""
    gate = _gate(monkeypatch)

    def replaced_during_probe() -> bool:
        monkeypatch.setattr(liveness, "_daemon_identity", lambda _config: (101, 124.0))
        return True

    def forbidden_reconnect() -> None:
        raise AssertionError("Native rebinding remains forbidden")

    with pytest.raises(liveness.ClioCoreRuntimeLostError):
        gate.ensure_live(forbidden_reconnect, rpc_probe=replaced_during_probe)
    assert gate.quarantined and gate.status()["restart_required"] is True


def test_later_gate_cannot_rebind_old_client_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing a namespace store after replacement cannot bless the old client."""
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _config: (102, 124.0))
    gate = liveness.LivenessGate(
        probe=lambda _port: True, ttl_s=0, auto_recover=False, attached_identity=(101, 123.5)
    )
    gate.note_rpc_stalled()
    gate._last_recovery_at = None

    def forbidden() -> Any:
        raise AssertionError("The process attach identity belongs to the original client")

    with pytest.raises(liveness.ClioCoreRuntimeLostError):
        gate.ensure_live(forbidden, rpc_probe=forbidden)
    assert gate.status()["restart_required"] is True


def test_existing_client_health_probe_never_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bounded health helper uses the supplied client's GIL-free round trip."""
    from clio_agent.arc import clio_core_async_ops

    client, cte = object(), object()
    calls: list[tuple[Any, Any, str]] = []

    class Store:
        _client, _cte = client, cte
        _gate = type("Gate", (), {"port": 9413})()

        def _reconnect(self) -> None:
            raise AssertionError("Do not reconstruct a live native client")

    def answer(actual_client: Any, actual_cte: Any, name: str) -> bool:
        calls.append((actual_client, actual_cte, name))
        return True

    monkeypatch.setattr(clio_core_async_ops, "rpc_answers", answer)
    monkeypatch.setattr("clio_agent.arc.daemon_progress.daemon_work", lambda: 0.0)
    assert rpc_liveness.store_rpc_health_probe(
        Store(), kind="records", name="sentinel", reconnect_client=False
    )
    assert calls == [(client, cte, "sentinel")]


@pytest.mark.parametrize("after_attach", [(101, 123.5), (102, 124.0), None])
def test_process_attach_identity_is_preserved_by_later_store_construction(
    after_attach: tuple[int, float] | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The actual attach/constructor path cannot bless a later replacement daemon."""
    from clio_agent.arc import clio_core_attach, storage
    from tests.test_arc.test_clio_core_attach import _cfg, _wire_ensure_runtime

    _wire_ensure_runtime(monkeypatch, client_ok=True)
    monkeypatch.setattr(storage.ClioCoreStore, "_attached_runtime_identity", None)
    identities = iter([(101, 123.5), after_attach])
    monkeypatch.setattr(liveness, "_daemon_identity", lambda _config: next(identities))
    clio_core_attach.reset_attach_state()
    try:
        config = _cfg(tmp_path, 23490)
        storage.ClioCoreStore._ensure_runtime(config, "error", 0.0)
        expected = (101, 123.5) if after_attach == (101, 123.5) else None
        assert storage.ClioCoreStore._attached_runtime_identity == expected
        monkeypatch.setattr(liveness, "_daemon_identity", lambda _config: (103, 125.0))
        monkeypatch.setattr(storage.ClioCoreStore, "_attach_verified", True)
        monkeypatch.setattr(
            sys.modules["clio_cte_core_ext"], "get_cte_client", object, raising=False
        )
        monkeypatch.setattr(
            storage, "TagIds", lambda *_args: SimpleNamespace(prewarm=lambda _: None)
        )
        monkeypatch.setattr(storage, "indexer_pool", lambda _config: "")
        store = storage.ClioCoreStore(config_path=config, namespace="later")
        assert store._gate._attached_identity == expected
        store._gate.note_rpc_stalled()
        store._gate._last_recovery_at = None

        def forbidden() -> Any:
            raise AssertionError("A later store cannot retarget the process's native attach")

        with pytest.raises(liveness.ClioCoreRuntimeLostError):
            store._gate.ensure_live(forbidden, rpc_probe=forbidden)
        with pytest.raises(liveness.ClioCoreRuntimeLostError):
            store._reconnect()
        assert store._gate.quarantined and store._gate.status()["restart_required"] is True
    finally:
        clio_core_attach.reset_attach_state()


def test_same_daemon_retry_retains_client_without_spawn_or_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The healthy original process needs neither another daemon nor another client."""
    from clio_agent.arc import storage

    monkeypatch.setattr(liveness, "_daemon_identity", lambda _config: (101, 123.5))
    monkeypatch.setattr(storage.ClioCoreStore, "_attached_runtime_identity", (101, 123.5))
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: True)
    store = object.__new__(storage.ClioCoreStore)
    store._config_path = "original.yaml"
    store._gate = liveness.LivenessGate(auto_recover=False, attached_identity=(101, 123.5))
    client, tag_ids = object(), object()
    store._client, store._tag_ids = client, tag_ids  # type: ignore[assignment]

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("The retry must retain the attached native client")

    store._cte = SimpleNamespace(get_cte_client=forbidden)
    monkeypatch.setattr(storage, "_ensure_runtime_daemon", forbidden)
    store._reconnect()
    assert store._client is client and store._tag_ids is tag_ids
    assert not store._gate.quarantined
