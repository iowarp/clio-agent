"""Run commands on cluster nodes over SSH (epic #667, multinode deployment).

The deployer needs to start a ``clio_run`` daemon and isolated workers on each node. With SSH
assumed (SLURM acquisition / cloud allocation with key access from the master), this module is
the thin remote-exec layer: :class:`SshRunner` runs/launches/kills commands on a node, and
:class:`SshSpawner` adapts it to the :class:`~clio_agent.runtime.worker_fleet.Spawner` protocol
so a fleet can place workers on a remote node.

Two gotchas the research surfaced are baked in:
- ``LD_LIBRARY_PATH`` is mandatory and NOT auto-set; ``ssh host -- cmd`` does not load a login
  profile, so every command carries an explicit ``env`` prefix.
- A node that is THIS host runs locally (no ssh hop) so a single-box deploy works without ssh
  keys — the same code path validates the orchestration end-to-end before a real cluster.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


def _local_aliases() -> set[str]:
    names = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    with contextlib.suppress(Exception):
        names.add(socket.gethostname())
    with contextlib.suppress(Exception):
        names.add(socket.getfqdn())
    return {n.lower() for n in names if n}


@dataclass
class SshRunner:
    """Execute commands on a node, locally when the node IS this host (no ssh), else over ssh.

    ``ld_library_path`` is prepended to every command's env (the clio-core dlopen requirement).
    ``user``/``opts`` shape the ssh invocation. ``force_ssh`` makes even the local host go over
    ssh (to exercise the real path on one box when keys are set up)."""

    user: str = ""
    opts: Sequence[str] = field(default_factory=lambda: ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"])
    ld_library_path: str = ""
    force_ssh: bool = False
    _local: set[str] = field(default_factory=_local_aliases, init=False)

    def is_local(self, host: str) -> bool:
        return not self.force_ssh and host.lower() in self._local

    def _env_prefix(self, env: Optional[dict]) -> list[str]:
        merged = dict(env or {})
        if self.ld_library_path:
            existing = merged.get("LD_LIBRARY_PATH", "")
            merged["LD_LIBRARY_PATH"] = (
                f"{self.ld_library_path}:{existing}" if existing else self.ld_library_path
            )
        if not merged:
            return []
        return ["env", *[f"{k}={v}" for k, v in merged.items()]]

    def build_command(self, host: str, argv: Sequence[str], env: Optional[dict] = None) -> list[str]:
        """The full argv to invoke (for inspection/tests). Local: env-prefixed argv. Remote:
        ``ssh [opts] [user@]host -- <single shell string>`` (env+argv shlex-quoted)."""
        prefixed = [*self._env_prefix(env), *argv]
        if self.is_local(host):
            return prefixed
        target = f"{self.user}@{host}" if self.user else host
        remote = " ".join(shlex.quote(a) for a in prefixed)
        return ["ssh", *self.opts, target, "--", remote]

    def run(
        self, host: str, argv: Sequence[str], env: Optional[dict] = None, *, check: bool = True,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess:
        """Run a command to completion on ``host`` and capture output."""
        cmd = self.build_command(host, argv, env)
        return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)

    def launch(
        self, host: str, argv: Sequence[str], env: Optional[dict] = None, *, log: str = "",
    ) -> "RemoteHandle":
        """Start a long-running process on ``host`` in the background; return a handle whose
        ``pid`` is the remote (or local) process id, for :meth:`is_alive` / :meth:`terminate`."""
        prefixed = [*self._env_prefix(env), *argv]
        redirect = f">{shlex.quote(log)} 2>&1" if log else ">/dev/null 2>&1"
        inner = " ".join(shlex.quote(a) for a in prefixed)
        # setsid so the child is its own session leader (clean kill); echo the pid back
        spawn = f"setsid {inner} {redirect} & echo $!"
        if self.is_local(host):
            out = subprocess.run(["bash", "-lc", spawn], capture_output=True, text=True, timeout=30)
        else:
            target = f"{self.user}@{host}" if self.user else host
            out = subprocess.run(
                ["ssh", *self.opts, target, "--", spawn], capture_output=True, text=True, timeout=30
            )
        pid = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
        return RemoteHandle(host=host, pid=pid, runner=self)


@dataclass
class RemoteHandle:
    host: str
    pid: str
    runner: "SshRunner"

    def is_alive(self) -> bool:
        if not self.pid:
            return False
        try:
            res = self.runner.run(self.host, ["kill", "-0", self.pid], check=False, timeout=15)
        except Exception:
            return False
        return res.returncode == 0

    def terminate(self, *, timeout: float = 10.0) -> None:
        if not self.pid:
            return
        with contextlib.suppress(Exception):
            # negative pid kills the whole session group (setsid leader)
            self.runner.run(self.host, ["kill", "-TERM", f"-{self.pid}"], check=False, timeout=15)
        with contextlib.suppress(Exception):
            self.runner.run(self.host, ["kill", "-KILL", f"-{self.pid}"], check=False, timeout=15)


class SshSpawner:
    """:class:`~clio_agent.runtime.worker_fleet.Spawner` that places each worker on a node via
    :class:`SshRunner`. The target node + the worker command come from ``node`` and ``command``;
    a fleet using this spawner runs its replicas on that one node (the deployer makes one per
    node). Handle is the :class:`RemoteHandle`."""

    def __init__(self, node: str, command: Sequence[str], runner: SshRunner, *, log_dir: str = "") -> None:
        self._node = node
        self._command = list(command)
        self._runner = runner
        self._log_dir = log_dir

    def spawn(self, *, role: str, worker_id: str, env: Any) -> RemoteHandle:
        log = os.path.join(self._log_dir, f"{role}.{worker_id}.log") if self._log_dir else ""
        return self._runner.launch(self._node, self._command, dict(env), log=log)

    def is_alive(self, handle: RemoteHandle) -> bool:
        return handle.is_alive()

    def terminate(self, handle: RemoteHandle, *, timeout: float = 10.0) -> None:
        handle.terminate(timeout=timeout)
