"""Cluster deploy: spec parsing, validation, and config/hostfile rendering (pure, no daemon).

The live up/status/down orchestration over a real daemon is exercised by the cross_process
deploy test; these cover the deterministic logic (the part that must be exactly right before
any cluster allocation is spent)."""

from __future__ import annotations

import yaml

from clio_agent.runtime.cluster_config import ClusterConfig
from clio_agent.runtime.cluster_deploy import ClusterDeployer, ClusterSpec, Node, WorkerSpec


def _spec(**kw):
    base = {
        "shared_dir": "/shared/x",
        "nodes": [Node("node0", "10.0.0.1"), Node("node1", "10.0.0.2")],
        "workers": [WorkerSpec("data", 2)],
    }
    base.update(kw)
    return ClusterSpec(**base)


def test_validate_catches_issues():
    assert _spec().validate() == []
    assert any("shared_dir" in i for i in _spec(shared_dir="").validate())
    assert any("nodes is empty" in i for i in _spec(nodes=[]).validate())
    assert any("duplicate" in i for i in _spec(
        nodes=[Node("a", "1.1.1.1"), Node("b", "1.1.1.1")]
    ).validate())
    assert any("unknown node" in i for i in _spec(
        workers=[WorkerSpec("data", 1, nodes=["ghost"])]
    ).validate())
    assert any("workers is empty" in i for i in _spec(workers=[]).validate())


def test_render_single_node_has_no_hostfile(tmp_path):
    spec = _spec(shared_dir=str(tmp_path), nodes=[Node("only", "127.0.0.1")])
    cfg_path, hostfile = ClusterDeployer(spec).render()
    assert hostfile.read_text().strip() == "127.0.0.1"
    doc = yaml.safe_load(cfg_path.read_text())
    assert "hostfile" not in doc["networking"]  # single node binds 0.0.0.0, no hostfile
    # the bundled bounded config carried through
    bdev = next(m for m in doc["compose"] if m["mod_name"] == "clio_bdev")
    assert bdev["capacity"] != "0g"


def test_render_multinode_writes_ordered_hostfile_and_points_config_at_it(tmp_path):
    spec = _spec(
        shared_dir=str(tmp_path),
        nodes=[Node("n0", "10.0.0.1"), Node("n1", "10.0.0.2"), Node("n2", "10.0.0.3")],
        port=9500,
    )
    cfg_path, hostfile = ClusterDeployer(spec).render()
    # hostfile line ORDER == node_id (load-bearing); addrs, in order
    assert hostfile.read_text().splitlines() == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    doc = yaml.safe_load(cfg_path.read_text())
    assert doc["networking"]["hostfile"] == str(hostfile)
    assert doc["networking"]["port"] == 9500


def test_spec_from_config():
    cfg = ClusterConfig(
        data={
            "cluster": {
                "shared_dir": "/nfs/clio",
                "python": "/opt/py/bin/python",
                "clio_core": {"bin": "/opt/clio_run", "ld_library_path": "/lib", "port": 9413},
                "ssh": {"user": "deployer", "force_ssh": True},
                "nodes": [{"host": "n0", "addr": "10.0.0.1"}],
                "workers": [{"role": "data", "replicas": 3, "nodes": ["n0"]}],
                "worker_env": {"CLIO_LM_PROVIDER": "argonne"},
            }
        }
    )
    spec = ClusterSpec.from_config(cfg)
    assert spec.shared_dir == "/nfs/clio"
    assert spec.python == "/opt/py/bin/python"
    assert spec.clio_core_bin == "/opt/clio_run"
    assert spec.ld_library_path == "/lib"
    assert spec.ssh_user == "deployer" and spec.force_ssh is True
    assert spec.nodes[0].addr == "10.0.0.1"
    assert spec.workers[0].replicas == 3 and spec.workers[0].nodes == ["n0"]
    assert spec.worker_env["CLIO_LM_PROVIDER"] == "argonne"


# --- live orchestration on ONE box (local mode: nodes=localhost, no ssh keys needed) -------

import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_CROSS = os.environ.get("CLIO_RUN_CROSS_PROCESS") == "1"
_ECHO_WORKER = Path(__file__).resolve().parent / "_isolated_cte_worker.py"


@pytest.mark.cross_process
@pytest.mark.skipif(not _CROSS, reason="set CLIO_RUN_CROSS_PROCESS=1")
def test_local_deploy_up_presence_status_down(tmp_path):
    """The deployer brings up a real daemon + isolated worker PROCESSES on one box (local mode,
    no ssh), the workers attach over CTE and announce presence, status reflects them, and down
    tears it all back down. Validates the full orchestration before a real cluster."""
    import iowarp_core

    pkg = os.path.dirname(iowarp_core.__file__)
    libdir, bindir = os.path.join(pkg, "lib"), os.path.join(pkg, "bin")
    os.environ["LD_LIBRARY_PATH"] = f"{libdir}:{bindir}:" + os.environ.get("LD_LIBRARY_PATH", "")
    os.makedirs("/tmp/clio_cte_tier", exist_ok=True)  # the bundled config's file-tier dir

    prefix = f"deploy_{os.getpid()}_"
    spec = ClusterSpec(
        shared_dir=str(tmp_path / "shared"),
        nodes=[Node("localhost", "127.0.0.1")],
        workers=[WorkerSpec("calc", 2)],
        python=sys.executable,
        clio_core_bin=os.path.join(bindir, "clio_run"),
        ld_library_path=f"{libdir}:{bindir}",
        worker_command=[sys.executable, str(_ECHO_WORKER)],
        worker_env={"CLIO_CORE_PREFIX": prefix},
    )
    deployer = ClusterDeployer(spec)
    deployer.down()  # clear any stale daemon/workers first (idempotent)
    try:
        state = deployer.up(ready_timeout=60)
        assert len(state["workers"]) == 2, state

        os.environ["CLIO_CTE_WITH_RUNTIME"] = "0"
        os.environ["CLIO_ARC_STORE"] = "cte"
        from clio_agent.arc.storage import make_arc_store
        from clio_agent.runtime.clio_core_transport import live_workers

        store = make_arc_store(backend="cte")
        present: list = []
        for _ in range(600):
            present = live_workers(store, "calc", prefix=prefix)
            if len(present) >= 2:
                break
            time.sleep(0.1)
        assert len(present) >= 2, f"deployed workers never announced presence: {present}"

        st = deployer.status()
        assert st["daemons"]["localhost"]["reachable"], st
        assert sum(1 for w in st["workers"] if w["alive"]) >= 2, st
    finally:
        deployer.down()
    assert not deployer._port_open("127.0.0.1"), "daemon still up after down"


def test_cluster_cli_validate_and_render(tmp_path, capsys):
    import yaml

    from clio_agent.runtime.cluster_cli import main

    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump({"cluster": {
        "shared_dir": str(tmp_path / "shared"),
        "nodes": [{"host": "n0", "addr": "10.0.0.1"}, {"host": "n1", "addr": "10.0.0.2"}],
        "workers": [{"role": "data", "replicas": 2}],
    }}))
    assert main(["validate", "-c", str(good)]) == 0
    assert main(["render", "-c", str(good)]) == 0
    assert (tmp_path / "shared" / "hostfile").read_text().splitlines() == ["10.0.0.1", "10.0.0.2"]

    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"cluster": {"nodes": [], "workers": []}}))
    assert main(["validate", "-c", str(bad)]) == 1  # missing shared_dir + empty nodes/workers


def test_cluster_cli_requires_config():
    from clio_agent.runtime.cluster_cli import main

    assert main(["status", "-c", ""]) == 2  # no config -> usage error
