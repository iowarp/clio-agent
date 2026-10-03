"""Preview and explicitly migrate Agent-owned legacy paths, preserving verified backups.

Run ``clio-agent migrate-paths --from-server /old/server --apply`` with all
writers stopped. No whole ~/.clio tree is ever moved; Core owns other entries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock

from clio_agent import paths


@dataclass(frozen=True)
class Move:
    """An explicit, bounded ownership transfer."""

    source: Path
    destination: Path


def plan(
    *,
    server: Path | None = None,
    user: Path | None = None,
    workspace: Path | None = None,
    home: Path | None = None,
) -> list[Move]:
    """Inventory known Agent entries only; unknown siblings remain untouched."""
    moves: list[Move] = []
    if server is not None:
        old = server / ".clio" / "agent"
        for name in (
            "sessions.json",
            "messages",
            "prompts",
            "session-prompts",
            "a2ui",
            "context_files.json",
            "permission_policies.json",
            "infrastructure.json",
            "message_intents.json",
            "resource_deliveries.json",
            "resource-processing",
            "artifacts",
            "documents",
        ):
            moves.append(Move(old / name, (paths.canonical_root("state") / "server") / name))
        for name in ("semantic_traces", "artifact_provenance"):
            moves.append(Move(old / name, paths.canonical_root("state") / "traces" / name))
        for name in ("resources", "arc"):
            moves.append(Move(old / name, paths.canonical_root("data") / name))
        for name in (
            "config.yaml",
            "mcp.yaml",
            "hooks.json",
            "prompts",
            "commands",
            "experts",
            "expert-packs",
            "agent-blueprints",
            "model-catalog.d",
        ):
            moves.append(Move(server / ".clio" / name, paths.canonical_root("config") / name))
    if home is not None:
        moves.extend(
            [
                Move(home / ".clio/hosts", paths.host_state_dir() / "core-hosts"),
                Move(home / ".clio/plans", paths.canonical_root("state") / "plans"),
                Move(home / ".clio/mcp-runtime", paths.canonical_root("cache") / "mcp-runtime"),
                Move(
                    home / ".local/share/clio/services", paths.canonical_root("data") / "services"
                ),
                Move(
                    home / "AppData/Local/CLIO/services", paths.canonical_root("data") / "services"
                ),
            ]
        )
    if user is not None:
        for name in (
            "config.yaml",
            "mcp.yaml",
            "hooks.json",
            "hooks.trust.json",
            "prompts",
            "commands",
            "experts",
            "expert-packs",
            "model-catalog.d",
            "agent-blueprints",
            "agent-blueprint-sources.json",
            "workspaces.json",
            "agents.json",
            "sandbox",
            "provider_api_keys.json",
            "codex_credential.json",
            "mcp_oauth_tokens.json",
        ):
            moves.append(Move(user / name, paths.canonical_root("config") / name))
        for role, destination in (
            ("data", paths.canonical_root("data")),
            ("cache", paths.canonical_root("cache")),
            ("state", paths.canonical_root("state")),
        ):
            source = user / role
            if source.is_dir() and source.resolve() != destination.resolve():
                moves.extend(Move(entry, destination / entry.name) for entry in source.iterdir())
    if workspace is not None:
        for name in (
            "config.yaml",
            "mcp.yaml",
            "hooks.json",
            "prompts",
            "commands",
            "experts",
            "expert-packs",
            "agent-blueprints",
            "model-catalog.d",
            "core",
        ):
            moves.append(
                Move(workspace / ".clio" / name, paths.workspace_shared_dir(workspace) / name)
            )
        # Generated copies can contain absolute references. Leave those originals
        # usable until their owning store re-materializes them; do not orphan them.
    return [m for m in moves if m.source.exists() and m.source.resolve() != m.destination.resolve()]


def inventory(root: Path) -> dict[str, tuple[int, str]]:
    """Hash regular files without following links or special filesystem objects."""
    result: dict[str, tuple[int, str]] = {}
    pending = [root]
    while pending:
        current = pending.pop()
        mode = current.lstat().st_mode
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError(f"migration refuses links: {current}")
        if stat.S_ISDIR(mode):
            pending.extend(current.iterdir())
        elif stat.S_ISREG(mode):
            with current.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            result[str(current.relative_to(root))] = (current.stat().st_size, digest)
        else:
            raise ValueError(f"migration refuses special files: {current}")
    return result


def _copy(source: Path, destination: Path) -> None:
    # Reserve the destination exclusively; clean up only what this call created.
    created = False
    try:
        if source.is_dir():
            destination.mkdir(mode=0o700)
            created = True
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            with destination.open("xb") as output:
                created = True
                with source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, output)
            shutil.copystat(source, destination)
    except Exception:
        if created:
            _remove(destination)
        raise


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _assert_idle() -> None:
    from clio_agent.runtime.disk_gc import live_peer_clio_processes

    peers = live_peer_clio_processes(exclude_pids={os.getpid()})
    if peers:
        raise RuntimeError(
            "stop Agent, Desktop and its Core daemon before migrating; live PIDs: "
            + ", ".join(str(peer.pid) for peer in peers)
        )


def migrate(moves: list[Move], *, apply: bool = False) -> dict[str, Any]:
    """Preview by default; copy, verify, back up and cut over only with apply.

    All destinations are checked before writing. A failure rolls back installed
    destinations and restores retired sources from the verified backups.
    """
    manifests = [inventory(move.source) for move in moves]
    report: dict[str, Any] = {
        "applied": False,
        "moves": [
            {
                "source": str(move.source),
                "destination": str(move.destination),
                "bytes": sum(size for size, _ in manifest.values()),
                "files": manifest,
            }
            for move, manifest in zip(moves, manifests, strict=True)
        ],
    }
    for i, move in enumerate(moves):
        if move.destination.exists() or move.destination.is_symlink():
            raise FileExistsError(f"destination occupied: {move.destination}")
        points = [move.source.resolve(), move.destination.resolve()]
        previous_points = [p.resolve() for m in moves[:i] for p in (m.source, m.destination)]
        for point, other in [(points[0], points[1])] + [
            (point, other) for point in points for other in previous_points
        ]:
            if point == other or point in other.parents or other in point.parents:
                raise ValueError("migration paths must not overlap")
        for parent in (*move.destination.parents, *move.source.parents):
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise ValueError(f"destination parent is a link: {parent}")
    if not apply or not moves:
        return report
    _assert_idle()
    receipts = paths.canonical_root("state") / "migrations"
    receipts.mkdir(parents=True, exist_ok=True)
    with FileLock(str(receipts / "migration.lock")):
        _assert_idle()
        run = receipts / uuid.uuid4().hex
        run.mkdir(mode=0o700)
        installed: list[Move] = []
        retired: list[tuple[Move, Path]] = []
        try:
            for i, (move, manifest) in enumerate(zip(moves, manifests, strict=True)):
                backup = run / f"backup-{i}"
                _copy(move.source, backup)
                if inventory(backup) != manifest or inventory(move.source) != manifest:
                    raise RuntimeError(f"source changed during backup: {move.source}")
                move.destination.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive copy also catches a destination appearing after preview.
                _copy(backup, move.destination)
                installed.append(move)
                if inventory(move.destination) != manifest:
                    raise RuntimeError(f"copy verification failed: {move.destination}")
            _assert_idle()
            for move, manifest in zip(moves, manifests, strict=True):
                if inventory(move.source) != manifest:
                    raise RuntimeError(f"source changed before cutover: {move.source}")
                # Same-directory rename retires the entire source atomically.
                # A partially failed recursive delete can never destroy it.
                retired_path = move.source.with_name(f".{move.source.name}.migrated-{run.name}")
                if retired_path.exists():
                    raise FileExistsError(retired_path)
                move.source.rename(retired_path)
                retired.append((move, retired_path))
            report.update(applied=True, backups=str(run))
            (run / "receipt.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        except Exception:
            for move, retired_path in reversed(retired):
                if not move.source.exists():
                    retired_path.rename(move.source)
            for move in reversed(installed):
                _remove(move.destination)
            raise
        # Cutover and receipt are durable. Verified backups remain available;
        # failure to remove an inactive retirement is reported, not rolled back.
        retained = []
        for _, retired_path in retired:
            try:
                _remove(retired_path)
            except OSError:
                retained.append(str(retired_path))
        report["retained_retirements"] = retained
        (run / "receipt.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> None:
    """Run the explicitly scoped path migration command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-server", type=Path)
    parser.add_argument("--from-user", type=Path)
    parser.add_argument("--from-home", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not any((args.from_server, args.from_user, args.from_home, args.workspace)):
        parser.error("select --from-server, --from-user, --from-home or --workspace")
    try:
        report = migrate(
            plan(
                server=args.from_server,
                user=args.from_user,
                home=args.from_home,
                workspace=args.workspace,
            ),
            apply=args.apply and not args.dry_run,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Migration refused: {exc}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
