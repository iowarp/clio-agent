"""``connected_data_release``: the agent frees the disk its older downloads hold.

Each download of a source publishes a new immutable snapshot; the previous ones stay,
and a transfer that failed or was interrupted can leave a staging folder. Nothing
released them, so in R9 a person reclaimed the disk by hand three times while runs
waited. ``connected_data_status`` shows the free bytes and each source's bytes on
disk (:mod:`.storage_usage`); this tool lets the agent act on that: it removes a
source's older snapshots and orphaned staging, and keeps the current downloaded
copy, the linked metadata and any transfer still queued or running.

A file hard-linked into the current copy (:func:`.transfers._unchanged_files`) frees
nothing when an older snapshot goes; the reported bytes are measured before and after,
so they are the bytes the disk actually got back. An older revision cannot be opened
once released. It is not read-only: the permission gate asks as for any change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool

_ACTIVE = frozenset({"queued", "running"})


def connected_data_release(source_id: str) -> dict[str, Any]:
    """Free the disk a source's older downloaded copies hold.

    Removes that source's earlier download snapshots and staging left by failed or
    interrupted transfers. Keeps the current downloaded copy, the linked folder's
    metadata and any transfer still running, so files you opened or downloaded last
    stay readable. An earlier revision cannot be opened afterwards. Returns what was
    released, the bytes freed and the free bytes now. connected_data_status shows
    each source's bytes on disk and the free space.
    """
    from clio_agent.gact.storage.storage_usage import bytes_on_disk, storage_usage
    from clio_agent.gact.storage.transfers import make_read_only, remove_owned_tree

    app = context.active_app()
    sid = context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    service = getattr(getattr(app, "state", None), "connected_storage", None)
    if session is None or service is None:
        raise ValueError("Releasing downloads requires an active CLIO workspace session")
    record = service.get(session.workspace_id, source_id, connected=False)
    owner = service.store.root / record.source.id
    keep = {name for name in (record.manifest_id, record.linked_manifest_id) if name}
    running = {
        operation.id
        for operation in service.store.operations_for_source(record.source.id)
        if operation.state in _ACTIVE
    }
    released: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    before = bytes_on_disk(owner) if owner.is_dir() else 0
    for child in sorted(owner.iterdir()) if owner.is_dir() else []:
        name = child.name
        if name.startswith("snapshot_"):
            if name in keep:
                kept.append({"copy": name, "kind": "current download"})
                continue
            remove_owned_tree(child, owner)
            released.append({"copy": name, "kind": "earlier download"})
        elif name.startswith("stage-"):
            if name.removeprefix("stage-") in running:
                kept.append({"copy": name, "kind": "transfer in progress"})
                continue
            remove_owned_tree(child, owner)
            released.append({"copy": name, "kind": "unfinished transfer"})
    for row in kept:  # cleanup made shared (hard-linked) files writable
        if row["kind"] == "current download":
            make_read_only(owner / row["copy"])
    after = bytes_on_disk(owner) if owner.is_dir() else 0
    usage = storage_usage(service.store.root, [record.source.id])
    return {
        "source_id": record.source.id,
        "label": record.source.label,
        "released": released,
        "kept": kept,
        "freed_bytes": max(0, before - after),
        "source_bytes": after,
        "free_bytes": usage["free_bytes"],
    }


def _size(value: Any) -> str:
    amount = float(value or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:,.0f} {unit}" if unit == "bytes" else f"{amount:,.1f} {unit}"
        amount /= 1024
    return f"{amount:,.1f} TB"


def release_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Row: what was freed from which source; expansion: copies released and kept."""
    from clio_agent.gact.storage.ndp_presentations import _row, _table, _text

    row = _row(result, structured)
    label = row.get("label") or args.get("source_id") or "source"
    if "freed_bytes" not in row:
        return {
            "summary": f"Release downloads of {label}",
            "blocks": [
                _text(
                    "unavailable",
                    "Result",
                    row.get("message") or "No release result was recorded.",
                    severity="warning",
                )
            ],
        }
    released, kept = row.get("released") or [], row.get("kept") or []
    blocks = [
        _text(
            "outcome",
            "Freed",
            f"{_size(row['freed_bytes'])} from {len(released)} copies; "
            f"{_size(row.get('source_bytes'))} kept on disk; {_size(row.get('free_bytes'))} free.",
        )
    ]
    if released or kept:
        rows = [[item["copy"], item["kind"], "released"] for item in released]
        rows += [[item["copy"], item["kind"], "kept"] for item in kept]
        blocks.append(
            {
                "id": "copies",
                "type": "markdown",
                "label": "Copies",
                "text": _table(["Copy", "What it was", "Now"], rows),
            }
        )
    return {
        "summary": f"Release older downloads of {label}: {_size(row['freed_bytes'])} freed",
        "blocks": blocks,
    }


def build_connected_data_release_tool() -> Any:
    """Build the tool that frees a source's older downloads."""
    return native_tool(
        connected_data_release,
        name="connected_data_release",
        desc=connected_data_release.__doc__,
        args={"source_id": {"type": "string"}},
        presentation=release_presentation,
        domain="resources",
        title="Release older downloads",
    )
