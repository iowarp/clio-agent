"""Serialized, lossless mutation of installed marketplace snapshots."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from clio_agent.gact.blueprint_install_files import tree_checksum

# Routes run in worker threads while discovery runs during turn preparation.
# Re-entrant because a staged file edit can commit through the same installer.
BLUEPRINT_MUTATION_LOCK = threading.RLock()


def replace_installed_blueprint(
    candidate: Path, destination: Path, metadata: dict[str, Any], *, preserve_invalid: bool = False
) -> tuple[str, dict[str, str]]:
    """Stage and validate a snapshot, refusing to overwrite edits or another source.

    The old directory remains available until the replacement is complete. A
    failed final rename restores it. This is a per-blueprint transaction, not a
    promise that a multi-blueprint marketplace changes atomically.
    """
    from clio_agent.gact.agent_blueprint_refresh import _default_blueprint_root_disabled
    from clio_agent.gact.agent_blueprints import read_install_metadata
    from clio_agent.gact.default_registry_migration import replace_pack_atomically

    with BLUEPRINT_MUTATION_LOCK:
        previous = read_install_metadata(destination)
        checksum = previous.get("checksum", "")
        repair = (
            preserve_invalid
            and destination.exists()
            and _default_blueprint_root_disabled(destination)[0]
        )
        if destination.exists() and not repair:
            if not checksum or tree_checksum(destination) != checksum:
                raise ValueError("local_edits_present: save or publish the working draft first")
            if previous.get("source") != metadata.get("source"):
                raise ValueError(
                    "source_conflict: this installed blueprint belongs to another source"
                )
            if previous.get("pinned_commit") and previous["pinned_commit"] != metadata.get(
                "pinned_commit"
            ):
                raise ValueError("pinned_revision: changing the pin requires an explicit selection")
        replace_pack_atomically(
            candidate, destination.parent, destination.name, metadata, keep_backup=repair
        )
        return checksum, read_install_metadata(destination)
