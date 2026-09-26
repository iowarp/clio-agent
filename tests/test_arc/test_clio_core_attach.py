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
    monkeypatch.setattr(storage.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(storage, "warn_if_search_indexer_absent", lambda _cfg: None)
    monkeypatch.delenv("CLIO_SERVER_CONF", raising=False)
    monkeypatch.delenv("CLIO_CORE_PORT", raising=False)

    def ensure_daemon(_core: object, cfg: str, _level: str) -> None:
        # The daemon is spawned with CLIO_SERVER_CONF=cfg; record what THIS process
        # exports at the moment the daemon is ensured.
        seen["daemon_conf"] = cfg
        seen["client_env_at_daemon_time"] = _env_subset()["CLIO_SERVER_CONF"]

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
    assert seen["client_env_at_daemon_time"] == cfg  # exported BEFORE the daemon/attach
    assert seen["client_init"][2]["CLIO_SERVER_CONF"] == cfg
    assert seen["client_init"][:2] == ("kClient", False)  # pure client, never embedded
    assert seen["initialize_cte"] == cfg
    assert storage.ClioCoreStore._initialized is True


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
