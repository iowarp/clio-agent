"""Review selected working-copy changes and reject stale local/upstream evidence."""

from __future__ import annotations

import difflib
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath

from clio_agent.gact.storage.adapters import SourceAdapter, file_hash, safe_child
from clio_agent.gact.storage.filesystem import walk
from clio_agent.gact.storage.models import (
    ChangeReview,
    FileEntry,
    Manifest,
    ReviewedChange,
    SourceRecord,
)
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.transfers import (
    TransferCancelled,
    check_cancel,
    local_hashes,
    make_read_only,
    remove_owned_tree,
    snapshot_revision,
)
from clio_agent.platform_paths import copytree_extended, win_extended_path


def _advance_baseline(
    store: SourceStore,
    record: SourceRecord,
    row: ReviewedChange,
    content: Path | None,
    revision: str | None,
) -> None:
    """Retain every baseline and advance only the file successfully applied."""
    previous = store.get("manifest", record.manifest_id or "", Manifest)
    owner = store.root / record.source.id
    identifier = "snapshot_" + uuid.uuid4().hex
    next_root = owner / identifier
    copytree_extended(owner / previous.id, next_root)
    for directory, _, files in walk(next_root):
        os.chmod(win_extended_path(directory), 0o700)
        for name in files:
            os.chmod(win_extended_path(directory / name), 0o600)
    destination = safe_child(next_root, row.path)
    entries = {entry.path: entry for entry in previous.entries}
    hashes = dict(previous.hashes)
    if content is None:
        os.unlink(win_extended_path(destination))
        entries.pop(row.path, None)
        hashes.pop(row.path, None)
    else:
        os.makedirs(win_extended_path(destination.parent), exist_ok=True)
        shutil.copyfile(win_extended_path(content), win_extended_path(destination))
        digest = file_hash(destination)
        hashes[row.path] = digest
        entries[row.path] = FileEntry(
            path=row.path,
            kind="file",
            revision=revision or "",
            size=os.stat(win_extended_path(destination)).st_size,
            sha256=digest,
        )
    for entry in list(entries.values()):
        for parent in PurePosixPath(entry.path).parents:
            if str(parent) != ".":
                entries.setdefault(str(parent), FileEntry(path=str(parent), kind="directory"))
    make_read_only(next_root)
    rows = sorted(entries.values(), key=lambda entry: entry.path)
    manifest = Manifest(
        id=identifier,
        source_id=record.source.id,
        revision=snapshot_revision(rows),
        entries=rows,
        hashes=hashes,
    )
    store.put("manifest", identifier, manifest)
    record.manifest_id = identifier
    record.source = record.source.model_copy(update={"revision": manifest.revision})
    store.put("source", record.source.id, record)


def review_changes(
    store: SourceStore, record: SourceRecord, adapter: SourceAdapter
) -> ChangeReview:
    """Compare the working tree with its immutable baseline and current source."""
    if (
        record.source.mode != "working_copy"
        or not record.manifest_id
        or not record.source.local_path
    ):
        raise ValueError("This source has no materialized working copy")
    manifest = store.get("manifest", record.manifest_id, Manifest)
    baseline = {row.path: row.revision for row in manifest.entries if row.kind == "file"}
    upstream = {row.path: row.revision for row in adapter.entries() if row.kind == "file"}
    current = local_hashes(Path(record.source.local_path))
    changes: list[ReviewedChange] = []
    for path in sorted(manifest.hashes.keys() | current.keys()):
        before, after = manifest.hashes.get(path), current.get(path)
        if before == after:
            continue
        preview, note = (
            _preview_change(
                safe_child(store.root / record.source.id / manifest.id, path),
                safe_child(Path(record.source.local_path), path),
                path,
            )
            if len(changes) < 100
            else ("", "Open this file to inspect it; the review preview limit was reached.")
        )
        changes.append(
            ReviewedChange(
                path=path,
                kind="delete" if after is None else "add" if before is None else "modify",
                local_hash=after,
                baseline_revision=baseline.get(path),
                upstream_revision=upstream.get(path),
                conflict=baseline.get(path) != upstream.get(path),
                preview=preview,
                preview_note=note,
            )
        )
    review = ChangeReview(
        id="review_" + uuid.uuid4().hex,
        source_id=record.source.id,
        manifest_id=manifest.id,
        changes=changes,
    )
    store.put("review", review.id, review)
    return review


def _preview_change(before: Path, after: Path, name: str) -> tuple[str, str]:
    """Return a bounded text diff for human review; never guess a binary representation."""
    texts = []
    for path in (before, after):
        if not os.path.exists(win_extended_path(path)):
            texts.append("")
            continue
        if os.stat(win_extended_path(path)).st_size > 65536:
            return "", "File is larger than 64 KB; inspect the file before applying it."
        try:
            with open(win_extended_path(path), encoding="utf-8") as reader:
                text = reader.read()
        except UnicodeDecodeError:
            return "", "Binary file; inspect the file before applying it."
        if "\0" in text:
            return "", "Binary file; inspect the file before applying it."
        texts.append(text)
    lines = list(
        difflib.unified_diff(
            texts[0].splitlines(),
            texts[1].splitlines(),
            fromfile="baseline/" + name,
            tofile="working-copy/" + name,
            lineterm="",
        )
    )
    return "\n".join(lines[:200])[
        :16000
    ], "Preview truncated; inspect the full file before applying it." if len(lines) > 200 or sum(
        map(len, lines)
    ) > 16000 else ""


def apply_review(
    store: SourceStore,
    record: SourceRecord,
    adapter: SourceAdapter,
    review_id: str,
    selected: list[str],
) -> str:
    """Apply only reviewed paths and persist partial success if a later file conflicts.

    Revisions are checked before the operation and again per file by the adapter.
    Providers without conditional writes expose that fact in their capabilities;
    this operation never promises directory-wide atomicity.
    """
    if not record.connected or record.source.mode != "working_copy":
        raise PermissionError("Connect a working-copy source before applying changes")
    review = store.get("review", review_id, ChangeReview)
    if review.source_id != record.source.id or review.manifest_id != record.manifest_id:
        raise ValueError("This review belongs to a different source revision")
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("Select distinct reviewed files to apply")
    rows = {row.path: row for row in review.changes}
    if any(path not in rows for path in selected):
        raise ValueError("Only files included in this review may be applied")
    if any(rows[path].conflict for path in selected):
        raise ValueError("Resolve upstream conflicts before applying these changes")
    root = Path(record.source.local_path or "")
    current = local_hashes(root)
    upstream = {row.path: row.revision for row in adapter.entries() if row.kind == "file"}
    for path in selected:
        if (
            current.get(path) != rows[path].local_hash
            or upstream.get(path) != rows[path].upstream_revision
        ):
            raise ValueError("Files changed after review; review again before applying")
    operation = store.begin_operation(record.source.id, "apply")
    store.update_operation(operation.id, state="running")
    applied: list[str] = []
    staging = store.root / record.source.id / ("apply-" + operation.id)
    try:
        staging.mkdir(parents=True)
        for path in selected:
            check_cancel(store, operation.id)
            row = rows[path]
            content = None
            if row.local_hash is not None:
                content = safe_child(staging, path)
                os.makedirs(win_extended_path(content.parent), exist_ok=True)
                shutil.copyfile(
                    win_extended_path(safe_child(root, path)), win_extended_path(content)
                )
                if file_hash(content) != row.local_hash:
                    raise ValueError("The working copy changed while applying; review again")
                os.chmod(win_extended_path(content), 0o444)
            revision = adapter.apply(path, content, row.upstream_revision)
            applied.append(path)
            store.update_operation(operation.id, applied_paths=applied)
            _advance_baseline(store, record, row, content, revision)
        store.update_operation(operation.id, state="completed")
    except (OSError, ValueError, RuntimeError) as exc:
        store.update_operation(
            operation.id,
            state="cancelled" if isinstance(exc, TransferCancelled) else "failed",
            error=str(exc),
            applied_paths=applied,
        )
        raise
    finally:
        remove_owned_tree(staging, store.root / record.source.id)
    return operation.id
