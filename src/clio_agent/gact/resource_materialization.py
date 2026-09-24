"""Materialize immutable uploaded resources as mutable workspace inputs.

Custody remains authoritative for hashes, revisions, conversion, and provenance.
The copy under ``.clio/inputs`` exists so workspace-scoped filesystem tools can
consume or transform an upload without ever mutating the custody original.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.resource_custody import ResourceMaterialization, windows_safe_filename
from clio_agent.platform_paths import short_stage_name, win_extended_path

if TYPE_CHECKING:
    from clio_agent.gact.resource_custody import ResourceRecord, ResourceStore

logger = logging.getLogger(__name__)

MANAGED_INPUT_DIRECTORY = Path(".clio") / "inputs"


def managed_input_relative_path(record: "ResourceRecord") -> Path:
    """Return the collision-safe workspace-relative path for ``record``.

    The resource's own ``id`` namespaces every upload into its own directory,
    so the name only has to be a safe FILENAME within that directory, never
    unique on its own. ``record.name`` is the user's original, unsanitized
    display name (see ``resource_custody._safe_name``) — :func:`
    windows_safe_filename` maps it into a real filename here, so a name like
    "Q3: notes?.md" still gets a distinct working copy instead of ever having
    failed to upload (S2 hardening: names are sanitized, never rejected).
    """

    if not record.id.startswith("res_") or not record.id.removeprefix("res_").isalnum():
        raise ValueError("resource id is not safe for workspace materialization")
    return MANAGED_INPUT_DIRECTORY / record.id / windows_safe_filename(record.name)


def materialize_resource(
    store: "ResourceStore", record: "ResourceRecord", workspace_root: str | Path
) -> "ResourceRecord":
    """Atomically copy a ready custody original into its active workspace.

    This deliberately uses a byte copy rather than a hardlink. The workspace
    input is a mutable working copy; editing it must not alter immutable custody.

    A workspace root is user-chosen and arbitrarily deep, so the composed
    destination (and its short-named staging sibling) can exceed Windows'
    260-character ``MAX_PATH`` even though every parent directory exists
    (observed live). Every OS-level call below routes its path through
    :func:`win_extended_path`, which is a no-op off win32.
    """

    source = store.content_path(record)
    root = Path(workspace_root).expanduser().resolve(strict=False)
    relative = managed_input_relative_path(record)
    destination = (root / relative).resolve(strict=False)
    destination.relative_to(root)

    recorded = Path(record.workspace_path).resolve(strict=False) if record.workspace_path else None
    if recorded == destination and os.path.isfile(win_extended_path(destination)):
        return record

    os.makedirs(win_extended_path(destination.parent), exist_ok=True)
    temporary = destination.with_name(short_stage_name())
    try:
        with (
            open(win_extended_path(source), "rb") as reader,
            open(win_extended_path(temporary), "xb") as writer,
        ):
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(win_extended_path(temporary), win_extended_path(destination))
    finally:
        _silent_unlink(temporary)

    updated = store.set_workspace_path(record.id, str(destination))
    if recorded is not None and recorded != destination:
        remove_materialized_resource(record)
    return updated


def _silent_unlink(path: Path) -> None:
    """Remove ``path`` if present (long-path-safe); a missing file is a no-op."""
    extended = win_extended_path(path)
    if os.path.exists(extended):
        os.remove(extended)


def materialize_resource_for_app(app: Any, record: "ResourceRecord") -> "ResourceRecord":
    """Materialize ``record`` under the root registered on its owning app."""

    workspace = app.state.workspaces.get(record.workspace_id)
    root = str(getattr(workspace, "root_path", "") or "") if workspace is not None else ""
    if not root:
        raise ValueError(f"workspace has no registered root: {record.workspace_id}")
    return materialize_resource(app.state.resource_store, record, root)


def remove_materialized_resource(record: "ResourceRecord") -> None:
    """Remove only the validated managed directory for ``record`` if present."""

    if not record.workspace_path:
        return
    target = Path(record.workspace_path).expanduser().resolve(strict=False)
    resource_dir = target.parent
    inputs_dir = resource_dir.parent
    clio_dir = inputs_dir.parent
    if (
        target.name != windows_safe_filename(record.name)
        or resource_dir.name != record.id
        or inputs_dir.name != "inputs"
        or clio_dir.name != ".clio"
    ):
        raise ValueError("recorded workspace input path is outside the managed layout")
    extended_resource_dir = win_extended_path(resource_dir)
    if os.path.isdir(extended_resource_dir):
        shutil.rmtree(extended_resource_dir)
    extended_inputs_dir = win_extended_path(inputs_dir)
    if os.path.isdir(extended_inputs_dir) and not os.listdir(extended_inputs_dir):
        os.rmdir(extended_inputs_dir)


def materialize_once(app: Any, record: "ResourceRecord") -> "ResourceRecord":
    """Materialize (or retry materializing) one ready resource's workspace copy.

    The SINGLE owner of "attempt this resource's workspace-input copy and
    record what happened". Every caller that touches a ready resource outside
    a plain GET — the HTTP create/PATCH/copy routes, the agent's
    resource-listing/inspect tools (``resource_tools.py``), and per-turn
    attachment enrichment (``resource_enrichment.py``) — routes through here
    rather than calling :func:`materialize_resource_for_app` directly, or a
    per-resource failure breaks whatever iterated over many resources at once
    (S2 hardening).

    Skips the actual attempt once a resource is known-ready
    (``materialization.state == "ready"``) — that state is only ever set
    after THIS function confirmed the copy, so re-checking it here would just
    repeat ``materialize_resource``'s own cheap early-return. Otherwise
    (``pending`` — including every legacy record whose stored index predates
    this field, which defaults here — or ``failed``) it (re)attempts
    unconditionally: a legacy record with no ``workspace_path`` gets
    materialized for the first time, and a previously-failed one gets a
    fresh attempt (the failure may have been transient, or its custody
    original may since have become reachable again).

    A plain GET (list or single) must NEVER call this — that is precisely
    the bug this exists to fix: GET used to re-materialize every resource on
    every read, so it never sees a ``pending``/``failed`` resource move to
    ``ready`` on its own. The next ready-touch point (turn enrichment, the
    agent's own listing, or an explicit copy) is what retries it.

    NEVER raises: a failure is recorded as a typed ``materialization`` state
    on the resource and returned, so the caller can carry on to the next
    resource instead of one failure aborting everything.
    """

    if record.state != "ready":
        return record
    if record.materialization.state == "ready":
        return record
    try:
        materialize_resource_for_app(app, record)
    except (OSError, ValueError) as exc:
        logger.warning(
            "resource materialization failed reason=resource_materialization_failed "
            "workspace_id=%s resource_id=%s error=%s",
            record.workspace_id,
            record.id,
            exc,
        )
        return app.state.resource_store.set_materialization(
            record.id, ResourceMaterialization(state="failed", reason=str(exc))
        )
    return app.state.resource_store.set_materialization(
        record.id, ResourceMaterialization(state="ready")
    )


__all__ = [
    "MANAGED_INPUT_DIRECTORY",
    "managed_input_relative_path",
    "materialize_once",
    "materialize_resource",
    "materialize_resource_for_app",
    "remove_materialized_resource",
]
