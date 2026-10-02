"""clio-core client attach: single-source config, checked attach, typed attach state.

Regression for the ares remote-deploy failure (2026-09-25): the spawned daemon was
handed ``CLIO_SERVER_CONF=<cte.yaml>`` (port 21045) but the attaching server process
was not, so its native client read ``~/.clio/clio.yaml`` and waited 30 s on 9413 --
twice, because the failed ``clio_init`` result was ignored and ``initialize_cte``
re-ran the client init. Binding-free: the native modules are faked in ``sys.modules``.
"""

from __future__ import annotations

import os
import sys
import time
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
    CLIO_CORE_CLIENT_ATTACH_TIMEOUT,
    CLIO_CORE_POST_ATTACH_PROBE_TIMEOUT,
    ArcStoreUnavailableError,
    classify_init_failure,
)


@pytest.fixture(autouse=True)
def _fresh_attach_state():
    clio_core_attach.reset_attach_state()
    yield
    clio_core_attach.reset_attach_state()


def _cfg(tmp_path: Path, port: int) -> str:
    path = tmp_path / "cte.yaml"
    # A durable file tier: the factory refuses a config that would forget on restart.
    path.write_text(
        f"networking:\n  port: {port}\nruntime:\n  num_threads: 4\n"
        "compose:\n  - mod_name: clio_cte_core\n    storage:\n"
        '      - path: "x/storage.bin"\n        bdev_type: "file"\n'
        '        persistence_level: "temporary"\n',
        encoding="utf-8",
    )
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
    # A failed attach releases through runtime_stop (deregister + last-one-out stop, #1401);
    # keep the stop half out of this binding-free test, record the release itself.
    monkeypatch.setattr(
        storage.runtime_stop,
        "release_failed_attach",
        lambda _cfg, _level: storage._deregister_client(),
    )
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


def test_attach_hands_the_native_client_clios_bound(monkeypatch):
    """The native wait is CLIO's configured stall bound, exported before ``clio_init``."""
    monkeypatch.delenv("CLIO_WAIT_SERVER", raising=False)
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "7")
    seen: list[str | None] = []

    def _init(mode, flag):
        seen.append(os.environ.get("CLIO_WAIT_SERVER"))
        return True

    cte = SimpleNamespace(clio_init=_init, RuntimeMode=SimpleNamespace(kClient="k"))
    clio_core_attach.attach_native_client(
        cte, config_path="c.yaml", port=1, on_failure=lambda: None
    )
    assert seen == ["7"]
    assert clio_core_attach.attach_window_s() == 7.0


def _daemon_work(monkeypatch, read) -> None:
    """The daemon progress signal for a binding-free test (no real daemon is attached)."""
    from clio_agent.arc import daemon_progress  # noqa: PLC0415

    monkeypatch.setattr(daemon_progress, "daemon_work", read)


def _advancing():
    state = {"w": 0.0}

    def read() -> float:
        state["w"] += 1.0
        return state["w"]

    return read


def test_an_attach_whose_daemon_made_no_progress_is_typed_as_a_timeout(monkeypatch):
    """A ``False`` after the whole window with the daemon flat: typed timeout, deregistered."""
    monkeypatch.delenv("CLIO_WAIT_SERVER", raising=False)
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.05")
    _daemon_work(monkeypatch, lambda: 3.0)
    deregistered: list[bool] = []

    def _init(mode, flag):
        time.sleep(0.1)  # the native wait ran out
        return False

    cte = SimpleNamespace(clio_init=_init, RuntimeMode=SimpleNamespace(kClient="k"))
    with pytest.raises(ClioCoreAttachError) as info:
        clio_core_attach.attach_native_client(
            cte, config_path="c.yaml", port=1, on_failure=lambda: deregistered.append(True)
        )
    assert classify_init_failure(info.value) == CLIO_CORE_CLIENT_ATTACH_TIMEOUT
    assert "got no answer within 0.05s and the daemon made no progress" in str(info.value)
    assert "wait=no_progress" in str(info.value)
    assert info.value.stage == "client_init"
    assert deregistered == [True]


def test_a_slow_handshake_with_a_working_daemon_attaches_past_the_window(monkeypatch):
    """A slow machine: two whole windows run out while the daemon works, the third answers.

    **Sabotage:** fail on the first window that runs out (the old fixed bound) -> typed
    timeout although the daemon was working the whole time.
    """
    monkeypatch.delenv("CLIO_WAIT_SERVER", raising=False)
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.05")
    _daemon_work(monkeypatch, _advancing())
    calls: list[bool] = []

    def _init(mode, flag):
        time.sleep(0.06)
        calls.append(True)
        return len(calls) == 3

    cte = SimpleNamespace(clio_init=_init, RuntimeMode=SimpleNamespace(kClient="k"))
    clio_core_attach.attach_native_client(
        cte, config_path="c.yaml", port=1, on_failure=lambda: pytest.fail("attached")
    )
    assert len(calls) == 3


def test_an_attach_against_an_unlocatable_daemon_names_it(monkeypatch):
    from clio_agent.arc.daemon_progress import DaemonPidUnresolved  # noqa: PLC0415

    monkeypatch.delenv("CLIO_WAIT_SERVER", raising=False)
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.05")

    def _unresolved() -> float:
        raise DaemonPidUnresolved("none")

    _daemon_work(monkeypatch, _unresolved)

    def _init(mode, flag):
        time.sleep(0.06)
        return False

    cte = SimpleNamespace(clio_init=_init, RuntimeMode=SimpleNamespace(kClient="k"))
    with pytest.raises(ClioCoreAttachError) as info:
        clio_core_attach.attach_native_client(
            cte, config_path="c.yaml", port=1, on_failure=lambda: None
        )
    assert "wait=daemon_pid_unresolved" in str(info.value)


def test_initialize_cte_against_a_stalled_daemon_fails_typed(monkeypatch):
    """``initialize_cte`` is bounded like every other clio-core wait: a whole window with
    no daemon progress is a typed ``clio_core_client_attach_timeout`` at its own stage.

    **Sabotage:** call ``initialize_cte`` inline (unbounded) -> the test hangs.
    """
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.1")
    _daemon_work(monkeypatch, lambda: 3.0)
    release = __import__("threading").Event()
    failures: list[bool] = []
    cte = SimpleNamespace(
        initialize_cte=lambda cfg, query: release.wait(30),
        PoolQuery=SimpleNamespace(Dynamic=lambda: "dynamic"),
    )
    monkeypatch.setattr(clio_core_attach, "attach_native_client", lambda *a, **k: None)
    started = time.monotonic()
    try:
        with pytest.raises(ClioCoreAttachError) as info:
            clio_core_attach.attach_and_initialize(
                cte,
                config_path="c.yaml",
                port=1,
                settle_s=0.0,
                on_failure=lambda: failures.append(True),
            )
    finally:
        release.set()
    assert time.monotonic() - started < 10.0
    assert info.value.stage == "initialize_cte"
    assert classify_init_failure(info.value) == CLIO_CORE_CLIENT_ATTACH_TIMEOUT
    assert failures == [True]


def test_a_slow_initialize_cte_with_a_working_daemon_completes(monkeypatch):
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STALL_AFTER_S", "0.1")
    _daemon_work(monkeypatch, _advancing())
    seen: list[str] = []
    cte = SimpleNamespace(
        initialize_cte=lambda cfg, query: (time.sleep(0.5), seen.append(cfg)),
        PoolQuery=SimpleNamespace(Dynamic=lambda: "dynamic"),
    )
    monkeypatch.setattr(clio_core_attach, "attach_native_client", lambda *a, **k: None)
    clio_core_attach.attach_and_initialize(
        cte, config_path="c.yaml", port=1, settle_s=0.0, on_failure=lambda: pytest.fail("no")
    )
    assert seen == ["c.yaml"]


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

    class _Store:
        """Stands in for the native ClioCoreStore build (this test is about the state)."""

        def __init__(self, *, config_path: str, namespace: str = "") -> None:
            observed.append(attach_state_snapshot().phase)  # while "attaching"

    _patch_store_build(monkeypatch, _Store)
    store = storage.make_arc_store(backend="cte", data_dir=tmp_path / "fb", config_path=cfg)

    assert isinstance(store, _Store)
    assert observed == [ClioCoreAttachPhase.STARTING]
    snap = attach_state_snapshot()
    assert snap.phase is ClioCoreAttachPhase.ATTACHED
    assert (snap.reason, snap.port, snap.config_path) == ("clio_core_attached", 21045, cfg)


def test_attach_state_unavailable_carries_the_typed_reason(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, 21045)

    def _fail(*, config_path: str, namespace: str = "") -> None:
        raise ClioCoreAttachError(port=21045, config_path=config_path)

    _patch_store_build(monkeypatch, _fail)
    with pytest.raises(ArcStoreUnavailableError) as caught:  # typed, never another store
        storage.make_arc_store(backend="cte", data_dir=tmp_path / "fb", config_path=cfg)

    assert caught.value.reason == CLIO_CORE_CLIENT_ATTACH_FAILED
    snap = attach_state_snapshot()
    assert snap.phase is ClioCoreAttachPhase.UNAVAILABLE
    assert snap.reason == CLIO_CORE_CLIENT_ATTACH_FAILED
    assert "21045" in snap.error


class _Future:
    """A stand-in binding ``Future``: answers with ``code`` or never answers at all."""

    def __init__(self, *, code: int | None) -> None:
        self.code = code
        self.waits: list[float] = []

    def done(self) -> bool:
        return self.code is not None

    def wait(self, max_sec: float = -1.0) -> int:
        # The real binding: against a daemon that is gone, wait() on an unfinished Future
        # ignores max_sec and blocks for good (holding the GIL). Never call it early.
        assert self.code is not None, "wait() on an unfinished Future can block forever"
        self.waits.append(max_sec)
        return self.code


def _probe_store(future: _Future, seen: dict[str, Any] | None = None) -> SimpleNamespace:
    def async_tag_query(regex: str, max_tags: int, query: object) -> _Future:
        if seen is not None:
            seen["query"] = (regex, max_tags, query)
        return future

    return SimpleNamespace(
        _HEALTH_PROBE_NAME="__probe__",
        _client=SimpleNamespace(AsyncTagQuery=async_tag_query),
        _cte=SimpleNamespace(PoolQuery=SimpleNamespace(Dynamic=lambda: "dynamic")),
        _gate=SimpleNamespace(port=21045),
        _config_path="cte.yaml",
    )


def _short_window(monkeypatch, seconds: float) -> None:
    from clio_agent.arc import rpc_liveness  # noqa: PLC0415

    monkeypatch.setattr(rpc_liveness, "health_probe_window_s", lambda policy=None: seconds)


def _stuck_daemon(monkeypatch) -> None:
    """The probed daemon makes no progress: its work counter stays flat.

    The stand-in store is attached to no real daemon, so the progress signal must come
    from the same stand-in, not from whatever clio-core daemon this worker runs (on
    Linux an idle one polls, so its CPU time always advances).
    """
    from clio_agent.arc import daemon_progress  # noqa: PLC0415

    monkeypatch.setattr(daemon_progress, "daemon_work", lambda: 1.0)


def test_post_attach_probe_against_a_stuck_daemon_expires_typed(monkeypatch):
    """A daemon that never answers costs the bound, then a typed error; never a hang."""
    _short_window(monkeypatch, 0.3)
    _stuck_daemon(monkeypatch)
    future = _Future(code=None)
    deregistered: list[bool] = []
    started = time.monotonic()

    with pytest.raises(ClioCoreAttachError) as info:
        clio_core_attach.verify_post_attach(
            _probe_store(future), on_failure=lambda: deregistered.append(True)
        )

    assert time.monotonic() - started < 5.0
    assert info.value.stage == "post_attach_probe"
    assert "made no progress for 0.3s" in str(info.value) and "21045" in str(info.value)
    assert classify_init_failure(info.value) == CLIO_CORE_POST_ATTACH_PROBE_TIMEOUT
    assert deregistered == [True]
    assert future.waits == []  # never waited on the unfinished Future


def test_post_attach_probe_nonzero_code_raises_typed(monkeypatch):
    _short_window(monkeypatch, 5.0)
    with pytest.raises(ClioCoreAttachError) as info:
        clio_core_attach.verify_post_attach(_probe_store(_Future(code=7)), on_failure=lambda: None)
    assert "return code 7" in str(info.value)


def test_post_attach_probe_success_hands_the_store_out(monkeypatch):
    _short_window(monkeypatch, 5.0)
    seen: dict[str, Any] = {}
    clio_core_attach.verify_post_attach(
        _probe_store(_Future(code=0), seen), on_failure=lambda: pytest.fail("no")
    )
    assert seen["query"] == ("__probe__", 1, "dynamic")  # the sentinel, async, no Tag()


def test_post_attach_window_derives_from_the_liveness_policy(monkeypatch):
    from clio_agent.arc import rpc_liveness  # noqa: PLC0415

    policy = rpc_liveness.LivenessPolicy(
        stall_after_s=2.5, retries=1, backoff_initial_s=1.0, backoff_max_s=1.0
    )
    assert rpc_liveness.health_probe_window_s(policy) == 2.5
    long = rpc_liveness.LivenessPolicy(
        stall_after_s=300.0, retries=1, backoff_initial_s=1.0, backoff_max_s=1.0
    )
    assert rpc_liveness.health_probe_window_s(long) == rpc_liveness._HEALTH_PROBE_MAX_S
