"""Run a Clio Coder arena match or research campaign for a repository, fenced, and print the result.

Both run in a scratch clone of the repository's HEAD under ``--state-root``; results come back
as ``clio-coder/...`` branches in the repository. The working tree is never touched.

    uv run python scripts/clio_coder_campaign.py --repo R --state-root S --home-template T \\
        arena --contender litellm/mini/m --contender litellm/mini/m --check tests "make it faster"
    uv run python scripts/clio_coder_campaign.py --repo R --state-root S --home-template T \\
        arena --vote MATCH_ID A [--adopt]
    uv run python scripts/clio_coder_campaign.py --repo R --state-root S --home-template T \\
        research --check bench --metric ops --direction min --max-iterations 2 --gate tests

An arena match's clone is kept so the person can vote later; ``--vote`` reuses it and
``--discard`` removes it. A research clone is removed once its best commit is fetched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from clio_agent.runtime.clio_coder import ClioCoderInstall, InstanceHome
from clio_agent.runtime.clio_coder.campaigns import (
    ARENA_CHOICES,
    ArenaRequest,
    CampaignWorkspace,
    ResearchRequest,
    adopt_arena_entry,
    discard_campaign,
    prepare_campaign_clone,
    run_arena,
    run_research,
    vote_arena,
)
from clio_agent.runtime.clio_coder.delegate import _new_run_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--home-template", type=Path)
    parser.add_argument("--bin")
    sub = parser.add_subparsers(dest="mode", required=True)
    arena = sub.add_parser("arena")
    arena.add_argument("prompt", nargs="?")
    arena.add_argument("--contender", action="append", default=[])
    arena.add_argument("--check")
    arena.add_argument("--timeout", type=float, default=600.0)
    arena.add_argument("--seed", type=int)
    arena.add_argument("--vote", nargs=2, metavar=("MATCH_ID", "CHOICE"))
    arena.add_argument("--adopt", action="store_true", help="fetch the voted entry as a branch")
    arena.add_argument("--discard", metavar="MATCH_ID")
    research = sub.add_parser("research")
    research.add_argument("--check", required=True)
    research.add_argument("--metric", required=True)
    research.add_argument("--direction", choices=("min", "max"), required=True)
    research.add_argument("--max-iterations", type=int, required=True)
    research.add_argument("--gate", action="append", default=[])
    research.add_argument("--scope", action="append", default=[])
    research.add_argument("--goal")
    research.add_argument("--trials", type=int)
    research.add_argument("--max-wall-minutes", type=float, default=20.0)
    research.add_argument("--target")
    research.add_argument("--model")
    return parser


def _print_event(event: dict) -> None:
    print(f"  [{event.get('kind')}] {event.get('text', '')}", file=sys.stderr, flush=True)


def _arena_workspace(args: argparse.Namespace, match_id: str) -> CampaignWorkspace:
    record = json.loads((args.state_root / "arena" / f"{match_id}.json").read_text("utf-8"))
    return CampaignWorkspace(Path(record["repo"]), Path(record["territory"]), record["base_commit"])


async def _arena(args, install, home) -> dict:
    root = args.state_root / "arena"
    if args.discard:
        workspace = _arena_workspace(args, args.discard)
        discard_campaign(workspace, root)
        (root / f"{args.discard}.json").unlink()
        return {"discarded": args.discard}
    if args.vote:
        match_id, choice = args.vote
        if choice not in ARENA_CHOICES:
            raise SystemExit(f"choice must be one of {ARENA_CHOICES}")
        workspace = _arena_workspace(args, match_id)
        revealed = await vote_arena(install, home, workspace, match_id, choice)
        out = {"vote": revealed.get("vote"), "entries": [
            {"label": e.get("label"), "reveal": e.get("reveal")} for e in revealed.get("entries", [])
        ]}  # fmt: skip
        if args.adopt and choice in ("A", "B", "C", "D"):
            match = json.loads((root / f"{match_id}.json").read_text("utf-8"))["match"]
            out["adopted"] = adopt_arena_entry(
                workspace, match, choice, args.state_root / "worktrees"
            )
        return out
    workspace = prepare_campaign_clone(args.repo, root, _new_run_id())
    request = ArenaRequest(
        prompt=args.prompt,
        contenders=args.contender,
        check=args.check,
        timeout=args.timeout,
        seed=args.seed,
    )
    match = await run_arena(install, home, workspace, request, sink=_print_event)
    record = {
        "repo": str(workspace.repo),
        "territory": str(workspace.territory),
        "base_commit": workspace.base_commit,
        "match": match,
    }
    (root / f"{match['match_id']}.json").write_text(json.dumps(record, indent=2), "utf-8")
    return match


async def _research(args, install, home) -> dict:
    root = args.state_root / "research"
    workspace = prepare_campaign_clone(args.repo, root, _new_run_id())
    request = ResearchRequest(
        check=args.check,
        metric=args.metric,
        direction=args.direction,
        max_iterations=args.max_iterations,
        gates=args.gate,
        scope=args.scope,
        goal=args.goal,
        trials=args.trials,
        max_wall_minutes=args.max_wall_minutes,
        target=args.target,
        model=args.model,
    )
    try:
        return await run_research(install, home, workspace, request, sink=_print_event)
    finally:
        discard_campaign(workspace, root)


def main() -> int:
    from clio_agent.runtime import sandbox

    args = _parser().parse_args()
    args.state_root = args.state_root.resolve()
    sandbox.install_sandbox()
    install = ClioCoderInstall.resolve(args.bin)
    home = InstanceHome(args.state_root / "home")
    home.prepare(None, template=args.home_template)
    runner = _arena if args.mode == "arena" else _research
    print(json.dumps(asyncio.run(runner(args, install, home)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
