"""``clio-cluster`` — deploy/manage the CLIO distributable runtime infrastructure.

The single command an operator runs (on the master / login node, with SSH to the cluster):

    clio-cluster validate [-c clio-cluster.yaml]   # check the deployment spec
    clio-cluster render   [-c ...]                 # write daemon config + hostfile (no start)
    clio-cluster up       [-c ...]                 # start daemons + isolated workers
    clio-cluster status   [-c ...]                 # per-node daemon + worker liveness
    clio-cluster down     [-c ...]                 # stop workers + daemons

``-c`` (or ``$CLIO_CLUSTER_CONFIG``) is the deployment YAML (its ``cluster:`` section defines
nodes, worker placement, ssh, and the shared config path). The parent agent is then started
normally with ``CLIO_EXPERT_INVOKER=clio_core_isolated`` + the same ``CLIO_CLUSTER_CONFIG`` and
delegates to the workers this command brought up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from clio_agent.runtime.cluster_config import ClusterConfig
from clio_agent.runtime.cluster_deploy import ClusterDeployer, ClusterSpec


def _build(config: str) -> tuple[ClusterDeployer, str]:
    cfg = ClusterConfig(path=config) if config else ClusterConfig()
    cfg.apply_to_env()  # config-file values win over env, per the deployment's precedence
    spec = ClusterSpec.from_config(cfg)
    return ClusterDeployer(spec), (config or os.environ.get("CLIO_CLUSTER_CONFIG", ""))


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="clio-cluster", description=__doc__.splitlines()[0])
    p.add_argument(
        "command", choices=["validate", "render", "up", "status", "down"], help="action"
    )
    p.add_argument(
        "-c", "--config", default=os.environ.get("CLIO_CLUSTER_CONFIG", ""),
        help="deployment YAML (default: $CLIO_CLUSTER_CONFIG)",
    )
    p.add_argument("--ready-timeout", type=float, default=60.0, help="daemon readiness barrier (s)")
    args = p.parse_args(argv)

    if not args.config and not os.environ.get("CLIO_CLUSTER_CONFIG"):
        print("clio-cluster: no config (-c FILE or $CLIO_CLUSTER_CONFIG)", file=sys.stderr)
        return 2

    deployer, config_file = _build(args.config)

    issues = deployer.spec.validate()
    if args.command == "validate":
        if issues:
            print("invalid deployment spec:", file=sys.stderr)
            for i in issues:
                print(f"  - {i}", file=sys.stderr)
            return 1
        print(f"ok: {len(deployer.spec.nodes)} node(s), "
              f"{sum(w.replicas for w in deployer.spec.workers)} worker(s)")
        return 0

    if issues and args.command in ("up", "render"):
        print("invalid deployment spec (run `validate`):", file=sys.stderr)
        for i in issues:
            print(f"  - {i}", file=sys.stderr)
        return 1

    if args.command == "render":
        cfg_path, hostfile = deployer.render()
        print(f"daemon config: {cfg_path}\nhostfile:      {hostfile}")
        return 0
    if args.command == "up":
        state = deployer.up(config_file=config_file, ready_timeout=args.ready_timeout)
        print(json.dumps(state, indent=2))
        return 0
    if args.command == "status":
        print(json.dumps(deployer.status(), indent=2))
        return 0
    if args.command == "down":
        deployer.down()
        print("down: workers + daemons stopped")
        return 0
    return 2  # pragma: no cover - argparse restricts the choices


if __name__ == "__main__":
    raise SystemExit(main())
