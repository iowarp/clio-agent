"""Run evaluation tasks against a running GACT server.

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.runner <command>``

Commands:

- ``setup``: install (or update) and trust the ``clio-eval`` marketplace from
  the fixtures folder, then check that the standard agent resolves with each
  eval plugin.
- ``plan``: print the runs and prompts a ``run`` would make; touches nothing.
- ``run``: for each task and run index, run it and write
  ``<results>/<config>/<task>/run-<n>.json``. Existing results are kept
  (``--force`` reruns them), so an invocation stopped by the usage guard can
  simply be repeated after the window resets.

The usage guard reads ``GET /v1/fleet`` before starting each run and stops
scheduling when a 7-day window is at or above ``--stop-at`` (default 0.93).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling import tasks
from scripts.eval.tool_calling.fixtures.plugins import CALL_LOG_ENV
from scripts.eval.tool_calling.http_client import GactClient, GactError
from scripts.eval.tool_calling.paths import (
    DEFAULT_FIXTURES,
    DEFAULT_RESULTS,
    DEFAULT_WORK,
    MARKETPLACE_DIR,
    MARKETPLACE_NAME,
    manifest_path,
)
from scripts.eval.tool_calling.session_run import (
    RunContext,
    RunSpec,
    execute_run,
    result_path,
)
from scripts.eval.tool_calling.usage_guard import DEFAULT_STOP_AT, GuardDecision, decide

EXIT_STOPPED = 3


def load_manifest(fixtures: Path) -> dict[str, Any]:
    path = manifest_path(fixtures)
    if not path.is_file():
        raise SystemExit(f"no manifest at {path}; run fixtures.generate first")
    return json.loads(path.read_text(encoding="utf-8"))


def plan_runs(
    task_ids: list[str], n: int, results: Path, config: str, *, force: bool
) -> list[RunSpec]:
    """Runs to make, task-major, skipping those with a result unless ``force``."""
    specs = [RunSpec(t, i) for t in task_ids for i in range(1, n + 1)]
    if force:
        return specs
    return [s for s in specs if not result_path(results, config, s.task, s.index).is_file()]


def guard_from_fleet(client: GactClient, stop_at: float, account: str) -> GuardDecision:
    try:
        return decide(client.fleet(), stop_at=stop_at, account=account)
    except (GactError, OSError) as exc:
        return GuardDecision(True, f"fleet unreadable, not starting more runs: {exc}")


def schedule(
    specs: list[RunSpec],
    *,
    concurrency: int,
    guard: Any,
    run: Any,
) -> tuple[list[tuple[RunSpec, Any]], GuardDecision | None, list[RunSpec]]:
    """Run ``specs`` with at most ``concurrency`` at once; check ``guard`` before each start.

    Returns the finished runs, the decision that stopped scheduling (if any)
    and the runs that never started.
    """
    pending = list(specs)
    finished: list[tuple[RunSpec, Any]] = []
    stopped: GuardDecision | None = None
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        active: dict[Future[Any], RunSpec] = {}
        while pending or active:
            while pending and stopped is None and len(active) < max(1, concurrency):
                decision = guard()
                print(f"usage guard: {decision.reason}", flush=True)
                if decision.stop:
                    stopped = decision
                    break
                spec = pending.pop(0)
                print(f"start {spec.task} run {spec.index}", flush=True)
                active[pool.submit(run, spec)] = spec
            if not active:
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                spec = active.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - one failed run must not stop the batch
                    outcome = exc
                finished.append((spec, outcome))
                print(f"done  {spec.task} run {spec.index}: {_describe(outcome)}", flush=True)
    return finished, stopped, pending


def _describe(outcome: Any) -> str:
    if isinstance(outcome, BaseException):
        return f"FAILED {type(outcome).__name__}: {outcome}"
    result = outcome or {}
    final = (result.get("outcome") or {}).get("reason", "?")
    return f"{final}, {result.get('wall_s')}s, session {result.get('session_id')}"


def _client(args: argparse.Namespace) -> GactClient:
    token = os.environ.get(args.token_env, "") if args.token_env else ""
    return GactClient(args.base_url, token=token)


def cmd_setup(args: argparse.Namespace) -> int:
    client = _client(args)
    manifest = load_manifest(args.fixtures)
    source = str((args.fixtures / MARKETPLACE_DIR).resolve())
    installed = {row.get("name") for row in client.marketplaces()}
    if MARKETPLACE_NAME in installed:
        client.update_marketplace(MARKETPLACE_NAME)
        print(f"updated marketplace {MARKETPLACE_NAME} from {source}")
    else:
        client.install_marketplace(source, MARKETPLACE_NAME)
        print(f"installed marketplace {MARKETPLACE_NAME} from {source}")
    client.trust_marketplace(MARKETPLACE_NAME)
    print(f"trusted {MARKETPLACE_NAME}")
    for plugin in manifest["marketplace"]["plugins"]:
        ref = f"{MARKETPLACE_NAME}/{plugin}"
        view = client.resolve_agent(args.agent_ref, [ref])
        ids = [str(row.get("id")) for row in view.get("plugins") or []]
        print(f"{args.agent_ref} + {ref}: {'ok' if plugin in ids else 'NOT RESOLVED'}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.fixtures)
    specs = plan_runs(tasks.select(args.tasks), args.n, args.results, args.config, force=args.force)
    for spec in specs:
        task = tasks.task(spec.task, manifest, h3_autocompact_pct=args.h3_autocompact_pct)
        print(f"{args.config} {spec.task} run {spec.index} [{task.agent_delta()['add']}]")
        print(f"    {task.prompt}")
    print(f"{len(specs)} runs")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    client = _client(args)
    manifest = load_manifest(args.fixtures)
    client.health()
    try:
        fleet_settings = client.fleet_settings()
    except GactError as exc:
        fleet_settings = {"unavailable": str(exc)}
    ctx = RunContext(
        client=client,
        fixtures=args.fixtures,
        manifest=manifest,
        results=args.results,
        work_root=args.work_root,
        config=args.config,
        provider=args.provider,
        model=args.model,
        agent_ref=args.agent_ref,
        approval_mode=args.approval_mode,
        extra_metadata=json.loads(args.session_metadata) if args.session_metadata else {},
        h3_autocompact_pct=args.h3_autocompact_pct,
        poll_s=args.poll,
        settle_s=args.settle,
        timeout_s=args.timeout,
        interaction_timeout_s=args.interaction_timeout,
        stream_audit_log=args.stream_audit_log,
        call_log=args.call_log,
        keep_export=args.keep_export,
        fleet_settings=fleet_settings,
    )
    specs = plan_runs(tasks.select(args.tasks), args.n, args.results, args.config, force=args.force)
    print(f"{len(specs)} runs to make for {args.config}", flush=True)
    concurrency = 1 if args.serial else args.concurrency
    finished, stopped, never = schedule(
        specs,
        concurrency=concurrency,
        guard=lambda: guard_from_fleet(client, args.stop_at, args.account),
        run=lambda spec: execute_run(ctx, spec),
    )
    failures = sum(1 for _, outcome in finished if isinstance(outcome, BaseException))
    print(f"{len(finished)} runs finished, {failures} failed")
    if stopped is not None:
        print(f"STOPPED: {stopped.reason}. {len(never)} runs not started:")
        for spec in never:
            print(f"  {spec.task} run {spec.index}")
        return EXIT_STOPPED
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runner", description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--token-env", default="CLIO_GACT_BEARER_TOKEN", help="env var holding a bearer token"
    )
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--agent-ref", default=tasks.STANDARD_AGENT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="install and trust the clio-eval marketplace")
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="base", help="configuration label, e.g. base, phase1")
        p.add_argument("--tasks", default="all", help="e.g. P1,P3-P5,H*")
        p.add_argument("--n", type=int, default=3, help="runs per task")
        p.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
        p.add_argument("--force", action="store_true", help="rerun runs that have a result")
        p.add_argument("--h3-autocompact-pct", type=float, default=tasks.H3_AUTOCOMPACT_PCT)
    run = sub.choices["run"]
    run.add_argument("--work-root", type=Path, default=DEFAULT_WORK)
    run.add_argument("--provider", default="claude_code")
    run.add_argument("--model", default="claude-sonnet-5-5", help="'' keeps the server default")
    run.add_argument("--approval-mode", default="bypass")
    run.add_argument("--session-metadata", default="", help="JSON merged into session metadata")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--serial", action="store_true", help="one run at a time (timing pass)")
    mode.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--stop-at", type=float, default=DEFAULT_STOP_AT)
    run.add_argument("--account", default="claude_code", help="fleet account the guard reads")
    run.add_argument("--poll", type=float, default=2.0, help="seconds between status polls")
    run.add_argument("--settle", type=float, default=6.0, help="quiet seconds before 'ended'")
    run.add_argument("--timeout", type=float, default=3600.0, help="seconds per run")
    run.add_argument("--interaction-timeout", type=float, default=120.0)
    run.add_argument(
        "--stream-audit-log",
        type=Path,
        default=os.environ.get("CLIO_STREAM_AUDIT_LOG") or None,
        help="the server's CLIO_STREAM_AUDIT_LOG (prefix resets); default from the env",
    )
    run.add_argument(
        "--call-log",
        type=Path,
        default=os.environ.get(CALL_LOG_ENV) or None,
        help=f"the server's {CALL_LOG_ENV} (synthetic server calls); default from the env",
    )
    run.add_argument("--keep-export", action="store_true", help="also save the raw export")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fixtures = args.fixtures.resolve()
    handlers = {"setup": cmd_setup, "plan": cmd_plan, "run": cmd_run}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
