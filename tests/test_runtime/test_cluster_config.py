"""ClusterConfig precedence (config file > env > default) + the bundled daemon config."""

from __future__ import annotations

import os

import pytest

from clio_agent.runtime.cluster_config import (
    ClusterConfig,
    default_daemon_config_path,
)


@pytest.fixture(autouse=True)
def _restore_env():
    """apply_to_env / build_app write os.environ DIRECTLY (not via monkeypatch). Snapshot and
    restore so these tests never leak CLIO_* into later tests in the same process."""
    snap = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snap)


def test_precedence_config_over_env_over_default(monkeypatch):
    # default when neither file nor env set
    cfg = ClusterConfig(data={})
    monkeypatch.delenv("CLIO_CORE_POLL", raising=False)
    assert cfg.poll_interval == 0.05

    # env overrides default
    monkeypatch.setenv("CLIO_CORE_POLL", "0.2")
    assert ClusterConfig(data={}).poll_interval == 0.2

    # config FILE overrides env (the user's precedence: file wins)
    cfg = ClusterConfig(data={"transport": {"poll_interval": 0.01}})
    assert cfg.poll_interval == 0.01  # file beats the env var still set to 0.2


def test_pool_query_default_and_override(monkeypatch):
    monkeypatch.delenv("CLIO_CORE_POOL_QUERY", raising=False)
    assert ClusterConfig(data={}).pool_query == "dynamic"
    assert ClusterConfig(data={"transport": {"pool_query": "broadcast"}}).pool_query == "broadcast"


def test_daemon_config_path_falls_back_to_bundled(monkeypatch):
    monkeypatch.delenv("CLIO_SERVER_CONF", raising=False)
    assert ClusterConfig(data={}).daemon_config_path == default_daemon_config_path()
    # env overrides the bundled default
    monkeypatch.setenv("CLIO_SERVER_CONF", "/some/other.yaml")
    assert ClusterConfig(data={}).daemon_config_path == "/some/other.yaml"
    # file overrides env
    cfg = ClusterConfig(data={"daemon": {"config_path": "/from/file.yaml"}})
    assert cfg.daemon_config_path == "/from/file.yaml"


def test_apply_to_env_pushes_file_values_over_env(monkeypatch):
    monkeypatch.setenv("CLIO_CORE_POLL", "0.5")  # pre-existing env
    monkeypatch.delenv("CLIO_SERVER_CONF", raising=False)
    cfg = ClusterConfig(
        data={"transport": {"poll_interval": 0.02, "pool_query": "broadcast", "prefix": "x_"}}
    )
    written = cfg.apply_to_env()
    # file value overrode the env var
    assert os.environ["CLIO_CORE_POLL"] == "0.02"
    assert os.environ["CLIO_CORE_POOL_QUERY"] == "broadcast"
    assert os.environ["CLIO_CORE_PREFIX"] == "x_"
    # CLIO_SERVER_CONF defaulted to the bundled config (nothing set it)
    assert os.environ["CLIO_SERVER_CONF"] == default_daemon_config_path()
    assert "CLIO_CORE_POLL" in written


def test_bundled_daemon_config_is_valid_and_bounded():
    import yaml

    path = default_daemon_config_path()
    assert os.path.exists(path), f"bundled daemon config missing: {path}"
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    # bounded allocator (never "0g" = 80% of RAM) + a file storage tier for disk durability
    bdev = next(m for m in doc["compose"] if m["mod_name"] == "clio_bdev")
    assert bdev["capacity"] != "0g" and bdev["capacity"].upper().endswith(("GB", "MB"))
    cte = next(m for m in doc["compose"] if m["mod_name"] == "clio_cte_core")
    tiers = cte["storage"]
    assert any(t["bdev_type"] == "file" for t in tiers), "no disk tier — would fill RAM"


def test_cluster_section_accessor():
    cfg = ClusterConfig(data={"cluster": {"nodes": [{"host": "n0", "addr": "10.0.0.1"}]}})
    assert cfg.cluster["nodes"][0]["host"] == "n0"
    assert ClusterConfig(data={}).cluster == {}


def test_resolve_pool_query_modes(monkeypatch):
    from clio_agent.arc.storage import _resolve_pool_query

    class _FakePoolQuery:
        @staticmethod
        def Broadcast():
            return "BCAST"

        @staticmethod
        def Dynamic():
            return "DYN"

        @staticmethod
        def Local():
            return "LOCAL"

    class _FakeCte:
        PoolQuery = _FakePoolQuery

    cte = _FakeCte()
    monkeypatch.setenv("CLIO_CORE_POOL_QUERY", "broadcast")
    assert _resolve_pool_query(cte) == "BCAST"
    monkeypatch.setenv("CLIO_CORE_POOL_QUERY", "local")
    assert _resolve_pool_query(cte) == "LOCAL"
    monkeypatch.setenv("CLIO_CORE_POOL_QUERY", "dynamic")
    assert _resolve_pool_query(cte) == "DYN"
    monkeypatch.delenv("CLIO_CORE_POOL_QUERY", raising=False)
    assert _resolve_pool_query(cte) == "DYN"  # default
    monkeypatch.setenv("CLIO_CORE_POOL_QUERY", "bogus")
    assert _resolve_pool_query(cte) == "DYN"  # unknown -> dynamic


def test_build_app_applies_cluster_config_when_set(monkeypatch, tmp_path):
    import yaml

    from clio_agent.gact.app import build_app

    cfg_path = tmp_path / "clio-cluster.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"transport": {"poll_interval": 0.011, "pool_query": "broadcast"}})
    )
    monkeypatch.setenv("CLIO_CLUSTER_CONFIG", str(cfg_path))
    monkeypatch.delenv("CLIO_CORE_POLL", raising=False)
    build_app()  # applies the config file to the env
    assert os.environ["CLIO_CORE_POLL"] == "0.011"
    assert os.environ["CLIO_CORE_POOL_QUERY"] == "broadcast"


async def test_config_file_drives_a_live_isolated_delegation(tmp_path, monkeypatch):
    """End-to-end: a config FILE (not env) sets the pull transport's prefix/poll, and a real
    isolated delegation runs with them in effect. If the file had NOT driven the prefix, the
    parent would route to the default 'clio_core_' queue and miss the worker on 'cfgdriven_'."""
    import asyncio
    import contextlib
    from types import SimpleNamespace

    import yaml

    from clio_agent.arc.storage import make_arc_store
    from clio_agent.gact.delegation_invoker import (
        expert_result_from_prediction,
        run_child_via_boundary,
    )
    from clio_agent.runtime.clio_core_transport import live_workers, run_isolated_worker
    from clio_agent.runtime.cluster_config import ClusterConfig

    prefix = "cfgdriven_"
    cfg_file = tmp_path / "clio-cluster.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {"transport": {"poll_interval": 0.02, "prefix": prefix, "ready_timeout": 10}}
        )
    )
    # wipe env so a pass PROVES the values came from the file, not the environment
    for k in ("CLIO_CORE_POLL", "CLIO_CORE_PREFIX", "CLIO_CORE_READY_TIMEOUT", "CLIO_CORE_ROLE"):
        monkeypatch.delenv(k, raising=False)
    ClusterConfig(path=str(cfg_file)).apply_to_env()
    assert os.environ["CLIO_CORE_PREFIX"] == prefix and os.environ["CLIO_CORE_POLL"] == "0.02"

    store = make_arc_store(backend="local", data_dir=str(tmp_path / "store"))
    pred = SimpleNamespace(
        answer="42", next_expert="", next_task="", expert_handoffs="", workflow_state={}
    )

    async def worker_handler(req):
        return expert_result_from_prediction(pred, expert_id=req.expert_id)

    stop = asyncio.Event()
    worker = asyncio.ensure_future(
        run_isolated_worker(store, worker_handler, role="data", worker_id="w1", prefix=prefix, stop=stop, poll=0.02)
    )
    try:
        for _ in range(200):
            if live_workers(store, "data", prefix=prefix):
                break
            await asyncio.sleep(0.02)

        async def parent_run_child(agent_def, prompt):
            raise AssertionError("isolated mode must not run the child in the parent")

        out = await run_child_via_boundary(
            SimpleNamespace(id="data"), "q",
            run_child=parent_run_child, mode="clio_core_isolated", store=store, role="data",
        )
        assert out.answer == "42"  # delegated over the config-file-driven prefix
    finally:
        stop.set()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker
