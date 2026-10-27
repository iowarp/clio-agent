#!/usr/bin/env python3
"""Run the desktop app's SSH deployment of Clio over a real SSH connection, step by step.

The install matrix measured SSH deployment with ``main``'s one-line installer, which is
not what the app does. This script builds the exact command specs the app sends to its
SSH transport (``build_driver_plan`` for the claim, install and start steps, then the
app's status, stop and teardown commands) and runs each one over ``ssh``. It records
every step the same way ``record_step.py`` does, so a failing step is reported with its
last output line and the run carries on.

    python scripts/ci/ssh_deploy_steps.py --file timings.jsonl \\
        --ssh "ssh -i key -p 2222 -o StrictHostKeyChecking=no clio@127.0.0.1" \\
        --root /home/clio/clio-ci --port 17950

The sequence: fresh deploy (claim, install, start), status, stop, status, a second
deploy over that install (claim, reuse, start), stop, teardown of the root, and the
files left behind.
"""

from __future__ import annotations

import argparse
import json
import platform
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from clio_agent.gact.infrastructure.clio_agent_deploy import (
    parse_claim,
    status_command,
    teardown_command,
)
from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.models import (
    CommandSpec,
    InfrastructureTarget,
    TargetFacts,
)
from clio_agent.gact.infrastructure.remote_lifecycle import stop_owned_command


def _target(root: str) -> InfrastructureTarget:
    return InfrastructureTarget.model_validate(
        {
            "id": "ci",
            "label": "ci-ssh",
            "kind": "ssh",
            "install_root": root,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )


def _plan(root: str, port: int) -> Any:
    return build_driver_plan(
        service_id="clio_agent",
        action="install",
        variant_id="released",
        configuration={"port": str(port)},
        facts=TargetFacts(target_id="ci", label="ci-ssh", os="linux", arch=platform.machine()),
        target=_target(root),
    )


class Runner:
    """Runs command specs over one ssh command line and records each as a timed step."""

    def __init__(self, ssh: str, file: Path) -> None:
        self.ssh = shlex.split(ssh)
        self.file = file

    def run(self, name: str, spec: CommandSpec) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        remote = shlex.join([spec.program, *spec.args])
        try:
            result = subprocess.run(  # noqa: S603 - the workflow's own ssh command
                [*self.ssh, remote],
                input=spec.stdin,
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            result = subprocess.CompletedProcess(expired.cmd, 124, str(expired.stdout or ""), "")
        lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        ok = result.returncode in spec.allowed_exit_codes
        last = lines[-1] if lines else ""
        row = {
            "name": f"ssh deploy: {name}",
            "os": "Linux (ssh target)",
            "seconds": round(time.monotonic() - started, 1),
            "exit_code": result.returncode,
            "killed": result.returncode == 124,
            "outcome": f"ok: {last[:100]}" if ok else f"failed: {last[:160] or 'no output'}",
        }
        with self.file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)
        if not ok:
            print("\n".join(lines[-20:]), flush=True)
        return result

    def deploy(self, label: str, root: str, port: int) -> Any:
        """Claim, install and start, as the app runs them; stop at the first failure."""
        plan = _plan(root, port)
        for spec in plan.commands:
            tag = spec.args[1].splitlines()[0].removeprefix("# clio-deploy:")
            result = self.run(f"{label} {tag}", spec)
            if tag == "claim":
                claim = parse_claim(result.stdout)
                print(f"claim: {claim}", flush=True)
                if claim is not None and claim.result not in {"free", "stopped"}:
                    break  # the app asks the person here; this run doesn't answer
            if result.returncode not in spec.allowed_exit_codes:
                break
        return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ssh", required=True, help="ssh command line up to the host")
    parser.add_argument("--file", required=True, type=Path, help="JSON-lines timings file")
    parser.add_argument("--root", default="", help="install root (empty: the app's default)")
    parser.add_argument("--port", type=int, default=17800)
    args = parser.parse_args()
    runner = Runner(args.ssh, args.file)

    first = runner.deploy("fresh", args.root, args.port)
    runner.run("status after start", status_command(first.remote_launch.root, args.port))
    runner.run("stop", stop_owned_command(first.remote_launch))
    runner.run("status after stop", status_command(first.remote_launch.root, args.port))
    second = runner.deploy("again", args.root, args.port)
    runner.run("stop again", stop_owned_command(second.remote_launch))
    root = second.remote_launch.root
    runner.run("teardown", teardown_command(root, args.port, purge_root=True))
    left = CommandSpec(
        program="bash",
        args=[
            "-lc",
            'echo "files=$(find "$1" -type f 2>/dev/null | wc -l)'
            ' processes=$(pgrep -u "$(id -u)" -f "[c]lio_agent|[c]lio_run|[c]himaera" | wc -l)"',
            "x",
            root,
        ],
    )
    runner.run("files and processes left", left)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
