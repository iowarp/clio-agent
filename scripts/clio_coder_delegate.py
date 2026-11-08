"""Delegate one coding task to a Clio Coder instance and print the verified hand-back.

Example (isolated home seeded from a template, a scratch repository, a caller-side check)::

    uv run python scripts/clio_coder_delegate.py \\
        --bin /path/to/clio-coder/dist/cli/index.js \\
        --repo /scratch/repo --state-root /scratch/delegations \\
        --home-template /scratch/home-template \\
        --verify "python3 -c 'import calc; assert calc.add(2, 3) == 5'" \\
        "calc.py has a bug in add(). Fix it."

With ``--tui`` the script prepares an interactive session instead: it writes the fenced launch to
``<state-root>/tui-<run>.sh`` for a terminal host to run, waits for the TUI to exit, then hands
the work back the same way. Keep the script running meanwhile: it owns the instance's egress.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from clio_agent.runtime.clio_coder import (
    ClioCoderInstall,
    ClioCoderTarget,
    DelegationRequest,
    RunOptions,
    delegate_coding_task,
)
from clio_agent.runtime.clio_coder.acp import allow_permissions_once, reject_permissions
from clio_agent.runtime.clio_coder.interactive import (
    describe_launch,
    finish_interactive_session,
    prepare_interactive_session,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--bin", help="clio-coder executable or dist/cli/index.js")
    parser.add_argument("--surface", choices=("headless", "acp"))
    parser.add_argument("--follow-up", action="append", default=[], help="ACP follow-up prompt")
    parser.add_argument("--in-place", action="store_true", help="work in --repo, no worktree")
    parser.add_argument("--discard-worktree", action="store_true")
    parser.add_argument("--home-template", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--target")
    parser.add_argument("--target-url", help="write a one-target home for this endpoint")
    parser.add_argument("--target-runtime", default="openai-compat")
    parser.add_argument("--api-key-env")
    parser.add_argument("--autonomy", choices=("default", "yolo"), default="default")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--verify", help="command run in the worktree after hand-back")
    parser.add_argument("--approve", action="store_true", help="ACP: allow mediated asks once")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--tui", action="store_true", help="interactive session for a terminal")
    return parser


def _print_event(event: dict[str, Any]) -> None:
    if event.get("kind") == "text":
        return
    detail = " ".join(f"{k}={v}" for k, v in event.items() if k != "kind" and v is not None)
    print(f"[clio-coder] {event.get('kind')}: {detail}"[:300], file=sys.stderr, flush=True)


async def _run_tui(request: DelegationRequest, args: argparse.Namespace):
    session = prepare_interactive_session(
        request, autonomy=None if args.autonomy == "default" else args.autonomy
    )
    state_root = request.state_root.resolve()
    exit_file = state_root / f"tui-{session.run_id}.exit"
    launch_file = state_root / f"tui-{session.run_id}.sh"
    launch_file.write_text(session.launch.shell_command(exit_file=exit_file) + "\n", "utf-8")
    print(json.dumps(describe_launch(session) | {"launch_file": str(launch_file)}), flush=True)
    deadline = time.monotonic() + args.timeout
    while not exit_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(1)
    code = int(exit_file.read_text().strip() or 1) if exit_file.exists() else None
    for path in (exit_file, launch_file):
        path.unlink(missing_ok=True)
    return await finish_interactive_session(
        session, keep_worktree=not args.discard_worktree, exit_code=code
    )


def main() -> int:
    from clio_agent.runtime import sandbox

    args = _parser().parse_args()
    # Like the server, resolve the OS fence once so every instance this host spawns is confined.
    sandbox.install_sandbox()
    target = None
    if args.target_url and args.model:
        target = ClioCoderTarget(
            model=args.model,
            target_id=args.target or "default",
            runtime=args.target_runtime,
            url=args.target_url,
            api_key_env=args.api_key_env,
        )
    request = DelegationRequest(
        task=args.task,
        repo=args.repo,
        state_root=args.state_root,
        follow_ups=args.follow_up,
        surface=args.surface,
        isolate=not args.in_place,
        target=target,
        home_template=args.home_template,
        options=RunOptions(
            autonomy=args.autonomy,
            model=args.model,
            target=args.target,
            read_only=args.read_only,
            timeout=args.timeout,
            max_cost_usd=args.max_cost_usd,
        ),
        verify_command=shlex.split(args.verify) if args.verify else None,
        keep_worktree=not args.discard_worktree,
        install=ClioCoderInstall.resolve(args.bin),
    )
    if args.tui:
        result = asyncio.run(_run_tui(request, args))
    else:
        permission = allow_permissions_once if args.approve else reject_permissions
        result = asyncio.run(
            delegate_coding_task(request, sink=_print_event, on_permission=permission)
        )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"outcome: {result.outcome} ({result.outcome_detail}) exit={result.exit_code}")
        print(f"session: {result.session_id} model={result.model} surface={result.surface}")
        print(f"workspace: {result.workspace} branch={result.branch}")
        print(f"changed: {', '.join(result.changed_files) or '(none)'}")
        print(f"usage: {json.dumps(result.usage)}")
        if result.verification is not None:
            print(f"verified: {result.verified} exit={result.verification.exit_code}")
        print("--- answer ---\n" + result.text.strip())
        print("--- diff ---\n" + result.diff.rstrip())
    checked = result.verification is None or result.verified
    return 0 if result.outcome == "succeeded" and checked else 1


if __name__ == "__main__":
    raise SystemExit(main())
