"""Multinode deployment of the CLIO distributable runtime (epic #667).

Brings up the *infrastructure* a detached delegation cluster needs — a ``clio_run`` daemon per
node plus isolated worker processes — from a single config file, over SSH. The parent agent
(the clio app with ``CLIO_EXPERT_INVOKER=clio_core_isolated``) then routes delegations to the
workers; cross-node blob routing is clio-core's (proven) networking layer.

Definition (the ``cluster:`` section of the CLIO_CLUSTER_CONFIG yaml)::

    cluster:
      shared_dir: /shared/clio-agent        # NFS/shared mount, same path on every node;
                                            # the rendered daemon config + hostfile live here
      python: python3                       # interpreter on each node (clio_agent installed)
      clio_core:
        bin: clio_run
        ld_library_path: /opt/iowarp/lib:/opt/iowarp/bin   # mandatory; ssh does not load a profile
        port: 9413
      ssh: { user: "", force_ssh: false }
      nodes:                                # LINE ORDER == clio-core node_id (load-bearing)
        - { host: node0, addr: 10.0.0.1 }   # host = gethostname() match; addr = dialable
        - { host: node1, addr: 10.0.0.2 }
      workers:
        - { role: data, replicas: 2 }       # nodes: [..] optional (default: all)
      worker_env: { CLIO_LM_PROVIDER: argonne }   # env every worker needs

Flow of :meth:`ClusterDeployer.up`: render the byte-identical daemon config + hostfile to the
shared dir → start the seed daemon (``start``) + the rest (``start --induct``) → barrier on
every daemon's port → launch isolated workers attached to each node's LOCAL daemon → persist
state for ``down``/``status``. ``down`` reverses it. Run on one box (nodes = localhost) with no
ssh keys to validate the whole orchestration before spending cluster allocation.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from clio_agent.runtime.cluster_config import ClusterConfig, default_daemon_config_path
from clio_agent.runtime.ssh import RemoteHandle, SshRunner


@dataclass
class Node:
    host: str
    addr: str


@dataclass
class WorkerSpec:
    role: str
    replicas: int = 1
    nodes: Optional[list[str]] = None  # which node hosts; None = all


@dataclass
class ClusterSpec:
    shared_dir: str
    nodes: list[Node]
    workers: list[WorkerSpec]
    python: str = "python3"
    clio_core_bin: str = "clio_run"
    ld_library_path: str = ""
    port: int = 9413
    ssh_user: str = ""
    force_ssh: bool = False
    worker_env: dict = field(default_factory=dict)
    worker_command: Optional[list[str]] = None  # default: [python, -m, clio_core_worker]

    @classmethod
    def from_config(cls, cfg: ClusterConfig) -> "ClusterSpec":
        c = cfg.cluster
        if not c:
            raise ValueError("config has no `cluster:` section — nothing to deploy")
        nodes = [Node(host=n["host"], addr=n.get("addr", n["host"])) for n in c.get("nodes", [])]
        workers = [
            WorkerSpec(role=w["role"], replicas=int(w.get("replicas", 1)), nodes=w.get("nodes"))
            for w in c.get("workers", [])
        ]
        cc = c.get("clio_core", {})
        ssh = c.get("ssh", {})
        return cls(
            shared_dir=c.get("shared_dir", ""),
            nodes=nodes,
            workers=workers,
            python=c.get("python", "python3"),
            clio_core_bin=cc.get("bin", "clio_run"),
            ld_library_path=cc.get("ld_library_path", ""),
            port=int(cc.get("port", 9413)),
            ssh_user=ssh.get("user", ""),
            force_ssh=bool(ssh.get("force_ssh", False)),
            worker_env=dict(c.get("worker_env", {})),
            worker_command=c.get("worker_command"),
        )

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.shared_dir:
            issues.append("cluster.shared_dir is required (the path daemon config + hostfile render to)")
        if not self.nodes:
            issues.append("cluster.nodes is empty — at least one node is required")
        addrs = [n.addr for n in self.nodes]
        if len(set(addrs)) != len(addrs):
            issues.append(f"duplicate node addrs: {addrs}")
        hosts = {n.host for n in self.nodes}
        for w in self.workers:
            if w.replicas < 1:
                issues.append(f"worker role {w.role!r} has replicas < 1")
            for h in w.nodes or []:
                if h not in hosts:
                    issues.append(f"worker role {w.role!r} placed on unknown node host {h!r}")
        if not self.workers:
            issues.append("cluster.workers is empty — no roles to place")
        return issues


class ClusterDeployer:
    """Render + bring up + tear down the daemon/worker infrastructure for a deployment."""

    def __init__(self, spec: ClusterSpec, *, base_daemon_config: str = "") -> None:
        self.spec = spec
        self._base_config = base_daemon_config or default_daemon_config_path()
        self._runner = SshRunner(
            user=spec.ssh_user, ld_library_path=spec.ld_library_path, force_ssh=spec.force_ssh
        )

    # ---- paths / state -------------------------------------------------------------

    @property
    def shared(self) -> Path:
        return Path(self.spec.shared_dir)

    @property
    def daemon_config_path(self) -> Path:
        return self.shared / "clio.yaml"

    @property
    def hostfile_path(self) -> Path:
        return self.shared / "hostfile"

    @property
    def state_path(self) -> Path:
        return self.shared / "deploy_state.json"

    # ---- render --------------------------------------------------------------------

    def render(self) -> tuple[Path, Path]:
        """Write the hostfile (one addr per line, order == node_id) and the byte-identical
        daemon config (bundled base + networking.hostfile) to the shared dir. Returns the paths."""
        import yaml  # noqa: PLC0415

        self.shared.mkdir(parents=True, exist_ok=True)
        single_node = len(self.spec.nodes) <= 1
        self.hostfile_path.write_text("\n".join(n.addr for n in self.spec.nodes) + "\n")

        with open(self._base_config, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        net = doc.setdefault("networking", {})
        net["port"] = self.spec.port
        # multinode points every node's daemon at the shared hostfile; single-node leaves it
        # unset (binds 0.0.0.0) so a one-box run needs no hostfile semantics.
        if single_node:
            net.pop("hostfile", None)
        else:
            net["hostfile"] = str(self.hostfile_path)
        self.daemon_config_path.write_text(yaml.safe_dump(doc, sort_keys=False))
        return self.daemon_config_path, self.hostfile_path

    # ---- bring up ------------------------------------------------------------------

    def _daemon_env(self) -> dict:
        return {"CLIO_SERVER_CONF": str(self.daemon_config_path)}

    def _port_open(self, addr: str, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((addr, self.spec.port), timeout=timeout):
                return True
        except OSError:
            return False

    def up(self, *, config_file: str = "", ready_timeout: float = 60.0, log_dir: str = "") -> dict:
        """Render, start every node's daemon (seed=start, rest=--induct), barrier on readiness,
        then launch the isolated workers. Returns the persisted deployment state."""
        self.render()
        logs = log_dir or str(self.shared / "logs")
        Path(logs).mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {"daemons": {}, "workers": []}

        # daemons: first node is the seed (plain start); the rest induct into the live cluster
        for i, node in enumerate(self.spec.nodes):
            argv = [self.spec.clio_core_bin, "start"] + (["--induct"] if i > 0 else [])
            h = self._runner.launch(
                node.host, argv, self._daemon_env(), log=f"{logs}/daemon.{node.host}.log"
            )
            state["daemons"][node.host] = {"pid": h.pid, "addr": node.addr}
            if i == 0:
                self._wait_port(node.addr, ready_timeout)  # seed must be up before inductions

        for node in self.spec.nodes:
            self._wait_port(node.addr, ready_timeout)  # barrier: every daemon answering

        # workers: each role's replicas on each placed node, attached to that node's LOCAL daemon
        for w in self.spec.workers:
            for node in self.spec.nodes:
                if w.nodes and node.host not in w.nodes:
                    continue
                for r in range(w.replicas):
                    wid = f"{w.role}-{node.host}-{r}"
                    env = self._worker_env(w.role, wid, config_file)
                    cmd = self.spec.worker_command or [
                        self.spec.python,
                        "-m",
                        "clio_agent.runtime.clio_core_worker",
                    ]
                    h = self._runner.launch(
                        node.host, cmd, env, log=f"{logs}/worker.{wid}.log"
                    )
                    state["workers"].append(
                        {"host": node.host, "worker_id": wid, "role": w.role, "pid": h.pid}
                    )

        self.state_path.write_text(json.dumps(state, indent=2))
        return state

    def _worker_env(self, role: str, worker_id: str, config_file: str) -> dict:
        env = {
            **self.spec.worker_env,
            "CLIO_ARC_STORE": "cte",
            "CLIO_CTE_WITH_RUNTIME": "0",
            "CLIO_CORE_ISOLATED": "1",
            "CLIO_CORE_ROLE": role,
            "CLIO_CORE_WORKER_ID": worker_id,
            "CLIO_SERVER_CONF": str(self.daemon_config_path),
        }
        if config_file:
            env["CLIO_CLUSTER_CONFIG"] = config_file  # workers honor the same config file
        return env

    def _wait_port(self, addr: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._port_open(addr):
                return
            time.sleep(0.5)
        raise TimeoutError(f"daemon on {addr}:{self.spec.port} not ready within {timeout}s")

    # ---- status / down -------------------------------------------------------------

    def status(self) -> dict:
        """Per-node daemon reachability + per-worker liveness (from the persisted state)."""
        out: dict[str, Any] = {"daemons": {}, "workers": []}
        for node in self.spec.nodes:
            out["daemons"][node.host] = {
                "addr": node.addr,
                "port": self.spec.port,
                "reachable": self._port_open(node.addr),
            }
        state = self._load_state()
        for w in state.get("workers", []):
            h = RemoteHandle(host=w["host"], pid=str(w.get("pid", "")), runner=self._runner)
            out["workers"].append({**w, "alive": h.is_alive()})
        return out

    def down(self, *, timeout: float = 10.0, port_close_timeout: float = 45.0) -> None:
        """Terminate every worker, then stop every node's daemon (``clio_run stop`` is the
        primary, graceful mechanism — it can take tens of seconds to release the port)."""
        state = self._load_state()
        for w in state.get("workers", []):
            RemoteHandle(host=w["host"], pid=str(w.get("pid", "")), runner=self._runner).terminate(
                timeout=timeout
            )
        for node in self.spec.nodes:
            try:
                self._runner.run(
                    node.host, [self.spec.clio_core_bin, "stop"], self._daemon_env(), check=False
                )
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        # also kill the daemon launcher pid in case `stop` didn't reap it
        for host, d in state.get("daemons", {}).items():
            RemoteHandle(host=host, pid=str(d.get("pid", "")), runner=self._runner).terminate(
                timeout=timeout
            )
        # `clio_run stop` is graceful (a grace period before the port frees); wait for each
        # daemon's port to actually close so `down` is synchronous (status afterwards is honest).
        deadline = time.monotonic() + port_close_timeout
        for node in self.spec.nodes:
            while self._port_open(node.addr) and time.monotonic() < deadline:
                time.sleep(0.5)
        if self.state_path.exists():
            self.state_path.unlink()

    def _load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"daemons": {}, "workers": []}
