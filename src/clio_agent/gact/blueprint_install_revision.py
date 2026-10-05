"""Stage complete marketplace revisions and recover interrupted directory swaps."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Collection
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.gact.blueprint_install_files import (
    copy_blueprint_tree,
    tree_checksum,
    write_install_metadata,
)
from clio_agent.gact.blueprint_ledgers import write_json_atomic
from clio_agent.gact.blueprint_mutations import (
    BLUEPRINT_MUTATION_LOCK,
    validate_install_destination,
)
from clio_agent.platform_paths import rename_extended, rmtree_extended

_ACTIVE_REVISIONS: set[Path] = set()


def _child(parent: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Invalid blueprint revision entry")
    path = parent / name
    if path.is_symlink() or path.resolve().parent != parent.resolve():
        raise ValueError("Blueprint revision path escapes its owned directory")
    return path


def _rollback(
    root: Path, directory: Path, rows: list[dict[str, Any]], tombstones: str | None = None
) -> None:
    for row in reversed(rows):
        destination = _child(root, row["destination"])
        backup = _child(directory, row["backup"])
        staged = _child(directory, row["staged"])
        # A missing staged tree means its rename completed. If the original
        # was moved but that rename failed, the backup still takes precedence.
        if backup.exists():
            if destination.exists():
                rmtree_extended(destination)
            rename_extended(backup, destination)
        elif not row["existed"] and not staged.exists() and destination.exists():
            rmtree_extended(destination)
    ledger = _child(root, ".uninstalled.json")
    if tombstones is not None:
        temporary = directory / "tombstones.restore"
        temporary.write_bytes(bytes.fromhex(tombstones))
        os.replace(temporary, ledger)
    elif ledger.exists():
        ledger.unlink()


def recover_install_revisions(root: Path) -> None:
    """Restore interrupted revisions before installed blueprints are discovered.

    Journals contain only child names. Backups remain on disk if restoration
    fails; callers must not advertise a partially restored registry as ready.
    """
    with BLUEPRINT_MUTATION_LOCK:
        from clio_agent.gact.default_registry_migration import registry_install_lock

        owner = root.parent / f".{root.name}-revisions"
        if not owner.is_dir():
            return
        with registry_install_lock(root) as held:
            if not held:
                raise ValueError("Another CLIO process is changing this marketplace; retry shortly")
            _recover(root, owner)


def _recover(root: Path, owner: Path) -> None:
    """Recover while both the thread and process installation locks are held."""
    for directory in sorted(owner.iterdir()):
        if not directory.is_dir() or directory.is_symlink() or directory in _ACTIVE_REVISIONS:
            continue
        journal = directory / "revision.json"
        if not journal.is_file():
            continue
        state = json.loads(journal.read_text(encoding="utf-8"))
        if state.get("version", 1) != 1 or state["status"] not in {
            "preparing",
            "applying",
            "committed",
            "rolled_back",
        }:
            raise ValueError("Unsupported blueprint revision journal; retained for inspection")
        if state["status"] == "applying":
            _rollback(root, directory, state["entries"], state.get("tombstones"))
            state["status"] = "rolled_back"
            write_json_atomic(journal, state)
        if state["status"] == "committed" and state.get("retain_backups"):
            continue
        rmtree_extended(directory)


@dataclass
class InstallRevision:
    """One owned staging directory, validated before any installed tree changes."""

    root: Path
    directory: Path = field(init=False)
    entries: list[dict[str, Any]] = field(default_factory=list)
    committed: bool = False
    applying: bool = False
    tombstones: str | None = None
    _locks: ExitStack = field(default_factory=ExitStack)

    def __enter__(self) -> InstallRevision:
        from clio_agent.gact.default_registry_migration import registry_install_lock

        self._locks.enter_context(BLUEPRINT_MUTATION_LOCK)
        try:
            if not self._locks.enter_context(registry_install_lock(self.root)):
                raise ValueError("Another CLIO process is changing this marketplace; retry shortly")
            recover_install_revisions(self.root)
            self.directory = (
                self.root.parent / f".{self.root.name}-revisions" / uuid.uuid4().hex[:8]
            )
            self.directory.mkdir(parents=True)
            _ACTIVE_REVISIONS.add(self.directory)
            ledger = _child(self.root, ".uninstalled.json")
            self.tombstones = ledger.read_bytes().hex() if ledger.is_file() else None
            self._record("preparing")
            return self
        except BaseException:
            if hasattr(self, "directory"):
                _ACTIVE_REVISIONS.discard(self.directory)
            self._locks.close()
            raise

    def _record(self, status: str) -> None:
        write_json_atomic(
            self.directory / "revision.json",
            {
                "version": 1,
                "status": status,
                "entries": self.entries,
                "retain_backups": any(row["repair"] for row in self.entries),
                "tombstones": self.tombstones,
            },
        )

    def stage(
        self,
        candidate: Path,
        destination: Path,
        metadata: dict[str, Any],
        *,
        preserve_invalid: bool,
        allow_pin_change: bool,
        runtime_tool_names: Collection[str] = (),
    ) -> tuple[Path, str]:
        """Copy and validate an immutable snapshot without touching its installation."""
        from clio_agent.gact.agent_blueprints import (
            parse_agent_blueprint_root,
            validate_agent_blueprint_path,
        )

        if destination.parent.resolve() != self.root.resolve():
            raise ValueError("Blueprint destination is outside the selected registry")
        destination = _child(self.root, destination.name)
        if any(row["destination"] == destination.name for row in self.entries):
            raise ValueError("Duplicate blueprint identity in marketplace revision")
        previous, repair = validate_install_destination(
            destination,
            metadata,
            preserve_invalid=preserve_invalid,
            allow_pin_change=allow_pin_change,
        )
        index = len(self.entries)
        staged, backup = self.directory / f"new-{index}", self.directory / f"old-{index}"
        copy_blueprint_tree(candidate, staged)
        parsed = parse_agent_blueprint_root(staged, scope=str(metadata["scope"]))
        if not parsed.enabled:
            raise ValueError("Staged blueprint is invalid: " + "; ".join(parsed.validation_errors))
        if parsed.root_expert:
            validation = validate_agent_blueprint_path(
                staged, scope=str(metadata["scope"]), runtime_tool_names=runtime_tool_names
            )
            if not validation["enabled"]:
                raise ValueError(
                    f'Blueprint "{parsed.display_name}" ({parsed.id}): '
                    "Staged blueprint runtime is invalid: "
                    + "; ".join(validation["validation_errors"])
                )
        if repair:
            metadata = {**metadata, "retained_previous_revision": str(backup)}
        write_install_metadata(staged, {**metadata, "checksum": tree_checksum(staged)})
        self.entries.append(
            {
                "destination": destination.name,
                "staged": staged.name,
                "backup": backup.name,
                "existed": destination.exists(),
                "previous_checksum": tree_checksum(destination) if destination.exists() else "",
                "repair": repair,
            }
        )
        self._record("preparing")
        return staged, previous

    def apply(self) -> None:
        """Swap all staged trees under the caller's turn boundary, rolling back on failure."""
        # External editors do not take our lock. Recheck after potentially slow
        # MCP preparation, before the first rename, so their edits survive.
        for row in self.entries:
            destination = _child(self.root, row["destination"])
            current = tree_checksum(destination) if destination.exists() else ""
            if current != row["previous_checksum"]:
                raise ValueError("local_edits_present: installed files changed during preparation")
        self._record("applying")
        self.applying = True
        try:
            for row in self.entries:
                destination = _child(self.root, row["destination"])
                if destination.exists():
                    rename_extended(destination, self.directory / row["backup"])
                rename_extended(self.directory / row["staged"], destination)
        except BaseException:
            _rollback(self.root, self.directory, self.entries, self.tombstones)
            self._record("rolled_back")
            self.applying = False
            raise

    def finish(self) -> None:
        """Commit only after install receipts and uninstall decisions were persisted."""
        self._record("committed")
        self.committed = True
        self.applying = False

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.applying:
                _rollback(self.root, self.directory, self.entries, self.tombstones)
                self._record("rolled_back")
                self.applying = False
            # Never erase the only remaining backups after a failed rollback.
            if not self.applying and not (
                self.committed and any(r["repair"] for r in self.entries)
            ):
                rmtree_extended(self.directory)
        finally:
            _ACTIVE_REVISIONS.discard(self.directory)
            self._locks.close()
