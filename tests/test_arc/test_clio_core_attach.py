"""clio-core client attach: single-source config, checked attach, typed attach state.

Regression for the ares remote-deploy failure (2026-09-25): the spawned daemon was
handed ``CLIO_SERVER_CONF=<cte.yaml>`` (port 21045) but the attaching server process
was not, so its native client read ``~/.clio/clio.yaml`` and waited 30 s on 9413 --
twice, because the failed ``clio_init`` result was ignored and ``initialize_cte``
re-ran the client init. Binding-free: the native modules are faked in ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import clio_core_attach, storage
from clio_agent.arc.clio_core_attach import (
    ClioCoreAttachError,
    ClioCoreAttachPhase,
    attach_state_snapshot,
)
from clio_agent.arc.init_degradation import (
    CLIO_CORE_CLIENT_ATTACH_FAILED,
    classify_init_failure,
    reset_arc_init_degradation,
)


@pytest.fixture(autouse=True)
def _fresh_attach_state():
    clio_core_attach.reset_attach_state()
    reset_arc_init_degradation()
    yield
    clio_core_attach.reset_attach_state()
    reset_arc_init_degradation()


def _cfg(tmp_path: Path, port: int) -> str:
    path = tmp_path / "cte.yaml"
    path.write_text(f"networking:\n  port: {port}\nruntime:\n  num_threads: 4\n", encoding="utf-8")
    return str(path)


def _fake_cte(*, client_ok: bool, seen: dict[str, Any]) -> types.ModuleType:
    """A stand-in ``clio_cte_core_ext`` recording what the native client would read."""
    mod = types.ModuleType("clio_cte_core_ext")

    def clio_init(mode: object, with_runtime: bool) -> bool:
        seen["client_init"] = (mode, with_runtime, dict(**_env_subset()))
        return client_ok

    def initialize_cte(cfg: str, query: object) -> None:
        seen["initialize_cte"] = cfg

    mod.clio_init = clio_init  # type: ignore[attr-defined]
    mod.RuntimeMode = SimpleNamespace(kClient="kClient")  # type: ignore[attr-defined]
    mod.initialize_cte = initialize_cte  # type: ignore[attr-defined]
    mod.PoolQuery = SimpleNamespace(Dynamic=lambda: "dynamic")  # type: ignore[attr-defined]
    return mod


def _env_subset() -> dict[str, str]:
    import os  # noqa: PLC0415

    return {"CLIO_SERVER_CONF": os.environ.get("CLIO_SERVER_CONF", "")}


def _wire_ensure_runtime(monkeypatch, *, client_ok: bool) -> dict[str, Any]:
    """Fake the native modules + daemon lifecycle around ``ClioCoreStore._ensure_runtime``."""
    seen: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "iowarp_core", types.ModuleType("iowarp_core"))
    monkeypatch.setitem(sys.modules, "clio_cte_core_ext", _fake_cte(client_ok=client_ok, seen=seen))
    monkeypatch.setattr(storage.ClioCoreStore, "_initialized", False)
    # Restore the stashed release params: a leaked fake config would point the
    # session-end last-one-out release at a port no daemon serves (orphan clio_run).
    monkeypatch.setattr(storage, "_active_config_path", storage._active_config_path)
    monkeypatch.setattr(storage, "_active_log_level", storage._active_log_level)
    monkeypatch.setattr(storage.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(storage, "warn_if_search_indexer_absent", lambda _cfg: None)
    monkeypatch.delenv("CLIO_SERVER_CONF", raising=False)
    monkeypatch.delenv("CLIO_CORE_PORT", raising=False)

    def ensure_daemon(_core: object, cfg: str, _level: str) -> str:
        # Returns the EFFECTIVE config: the request when this process spawns the
        # daemon, the running daemon's own when it attaches (first config wins).
        seen["daemon_conf"] = cfg
        return str(seen.get("daemon_effective_conf", cfg))

    monkeypatch.setattr(storage, "_ensure_runtime_daemon", ensure_daemon)
    deregistered: list[bool] = []
    monkeypatch.setattr(storage, "_deregister_client", lambda: deregistered.append(True))
    seen["deregistered"] = deregistered
    return seen


def test_client_reads_the_same_config_the_daemon_composes(monkeypatch, tmp_path):
    """The native client must see CLIO_SERVER_CONF == the daemon's config (the ares bug)."""
    cfg = _cfg(tmp_path, 21045)
    seen = _wire_ensure_runtime(monkeypatch, client_ok=True)

    storage.ClioCoreStore._ensure_runtime(cfg, "error", 0.0)

    assert seen["daemon_conf"] == cfg
    assert seen["client_init"][2]["CLIO_SERVER_CONF"] == cfg  # exported BEFORE the attach
    assert seen["client_init"][:2] == ("kClient", False)  # pure client, never embedded
    assert seen["initialize_cte"] == cfg
    assert storage.ClioCoreStore._initialized is True


def test_client_attaches_with_the_running_daemons_config(monkeypatch, tmp_path):
    """First config wins: the client and CTE init use the daemon's config, not the request."""
    requested = _cfg(tmp_path, 21045)
    (tmp_path / "daemon").mkdir()
    daemon_cfg = _cfg(tmp_path / "daemon", 21045)
    seen = _wire_ensure_runtime(monkeypatch, client_ok=True)
    seen["daemon_effective_conf"] = daemon_cfg

    effective = storage.ClioCoreStore._ensure_runtime(requested, "error", 0.0)

    assert effective == daemon_cfg
    assert seen["client_init"][2]["CLIO_SERVER_CONF"] == daemon_cfg
    assert seen["initialize_cte"] == daemon_cfg


def test_failed_client_init_raises_typed_at_once_and_deregisters(monkeypatch, tmp_path):
    """A False clio_init fails fast: no initialize_cte (the second 30 s wait), no vote held."""
    cfg = _cfg(tmp_path, 21045)
    seen = _wire_ensure_runtime(monkeypatch, client_ok=False)

    with pytest.raises(ClioCoreAttachError) as info:
        storage.ClioCoreStore._ensure_runtime(cfg, "error", 0.0)

    assert info.value.port == 21045
    assert info.value.config_path == cfg
    assert "21045" in str(info.value) and cfg in str(info.value)
    assert "initialize_cte" not in seen
    assert seen["deregistered"] == [True]
    assert storage.ClioCoreStore._initialized is False
    assert classify_init_failure(info.value) == CLIO_CORE_CLIENT_ATTACH_FAILED


def test_attach_native_client_noop_on_success():
    calls: list[bool] = []
    cte = SimpleNamespace(
        clio_init=lambda mode, flag: True, RuntimeMode=SimpleNamespace(kClient="k")
    )
    clio_core_attach.attach_native_client(
        cte, config_path="c.yaml", port=1, on_failure=lambda: calls.append(True)
    )
    assert calls == []


def test_config_file_port_wins_over_core_port_override(monkeypatch, tmp_path):
    """The port probed is the port the daemon binds: the config file it is spawned with."""
    cfg = _cfg(tmp_path, 21045)
    monkeypatch.setenv("CLIO_CORE_PORT", "9413")
    assert storage._resolve_runtime_port(cfg) == 21045
    # A config without networking.port still honours the override.
    bare = tmp_path / "bare.yaml"
    bare.write_text("runtime:\n  num_threads: 4\n", encoding="utf-8")
    assert storage._resolve_runtime_port(str(bare)) == 9413


def _patch_store_build(monkeypatch, build) -> None:
    from clio_agent.arc import clio_core_file_capacity  # noqa: PLC0415

    monkeypatch.setattr(clio_core_file_capacity, "preflight_clio_core_config", lambda *a, **k: None)
    monkeypatch.setattr(storage, "ClioCoreStore", build)


def test_attach_state_starting_then_attached(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, 21045)
    observed: list[ClioCoreAttachPhase] = []

    class _Store(storage.LocalFSStore):
        def __init__(self, *, config_path: str) -> None:
            observed.append(attach_state_snapshot().phase)  # while "attaching"
            super().__init__(tmp_path / "arc")

    _patch_store_build(monkeypatch, _Store)
    store = storage.make_arc_store(backend="cte", data_dir=tmp_path / "fb", config_path=cfg)

    assert isinstance(store, _Store)
    assert observed == [ClioCoreAttachPhase.STARTING]
    snap = attach_state_snapshot()
    assert snap.phase is ClioCoreAttachPhase.ATTACHED
    assert (snap.reason, snap.port, snap.config_path) == ("clio_core_attached", 21045, cfg)


def test_attach_state_unavailable_carries_the_typed_reason(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, 21045)

    def _fail(*, config_path: str) -> None:
        raise ClioCoreAttachError(port=21045, config_path=config_path)

    _patch_store_build(monkeypatch, _fail)
    store = storage.make_arc_store(backend="cte", data_dir=tmp_path / "fb", config_path=cfg)

    assert isinstance(store, storage.LocalFSStore)  # LOUD degrade, not a crash
    snap = attach_state_snapshot()
    assert snap.phase is ClioCoreAttachPhase.UNAVAILABLE
    assert snap.reason == CLIO_CORE_CLIENT_ATTACH_FAILED
    assert "21045" in snap.error


def test_attach_state_not_selected_for_explicit_local(tmp_path):
    storage.make_arc_store(backend="local", data_dir=tmp_path / "arc")
    assert attach_state_snapshot().phase is ClioCoreAttachPhase.NOT_SELECTED


def _probe_store(port: int = 21045) -> SimpleNamespace:
    return SimpleNamespace(
        _HEALTH_PROBE_KIND="segments",
        _HEALTH_PROBE_NAME="__probe__",
        _gate=SimpleNamespace(port=port),
        _config_path="cte.yaml",
    )


def test_post_attach_probe_failure_raises_typed_and_deregisters(monkeypatch):
    """A clean clio_init/initialize_cte whose first RPC fails degrades typed at init."""
    from clio_agent.arc import rpc_liveness  # noqa: PLC0415

    monkeypatch.setattr(rpc_liveness, "store_rpc_health_probe", lambda *a, **k: False)
    deregistered: list[bool] = []

    with pytest.raises(ClioCoreAttachError) as info:
        clio_core_attach.verify_post_attach(
            _probe_store(), on_failure=lambda: deregistered.append(True)
        )

    assert info.value.stage == "post_attach_probe"
    assert "stage=post_attach_probe" in str(info.value) and "21045" in str(info.value)
    assert classify_init_failure(info.value) == CLIO_CORE_CLIENT_ATTACH_FAILED
    assert deregistered == [True]


def test_post_attach_probe_success_hands_the_store_out(monkeypatch):
    from clio_agent.arc import rpc_liveness  # noqa: PLC0415

    seen: dict[str, Any] = {}

    def probe(store: object, *, kind: str, name: str) -> bool:
        seen["probe"] = (kind, name)
        return True

    monkeypatch.setattr(rpc_liveness, "store_rpc_health_probe", probe)
    clio_core_attach.verify_post_attach(_probe_store(), on_failure=lambda: pytest.fail("no"))
    assert seen["probe"] == ("segments", "__probe__")  # the store's own liveness sentinel
