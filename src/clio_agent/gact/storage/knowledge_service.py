"""One knowledge boundary for the workbench and capability-gated agent tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clio_schemas.knowledge import KnowledgeNote

from clio_agent.gact.storage.adapters import file_hash
from clio_agent.gact.storage.knowledge_index import KnowledgeIndex
from clio_agent.gact.storage.linked import link_folder, linked_file
from clio_agent.gact.storage.models import Manifest, SourceRecord
from clio_agent.gact.storage.obsidian import ObsidianSource
from clio_agent.gact.storage.references import source_resource
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.vault_parser import parse_note


class KnowledgeService:
    """Own indexed views and immutable note captures within approved workspace sources."""

    def __init__(self, storage: StorageService, resources: Any) -> None:
        self.storage, self.resources = storage, resources
        self.index = KnowledgeIndex(storage.store.root, storage.store)

    def record(self, workspace_id: str, source_id: str) -> SourceRecord:
        """Check source identity, ownership, connectivity, and knowledge capabilities."""
        record = self.storage.get(workspace_id, source_id)
        if record.source.source_kind != "knowledge" or not record.source.capabilities.note_read:
            raise PermissionError("Select an approved knowledge source")
        return record

    def open(
        self, workspace_id: str, source_id: str, path: str, revision: str = ""
    ) -> dict[str, Any]:
        """Capture a selected note and return its exact bytes, metadata and provenance."""
        record = self.record(workspace_id, source_id)
        from clio_agent.gact.storage.linked_changes import staged_file

        if not record.linked_manifest_id:
            record = link_folder(self.storage, record)
        elif staged_file(self.storage, record, path) is None:
            current = ObsidianSource(
                record.source.root, excluded=record.configuration.obsidian_excluded_folders
            ).metadata(path)
            manifest = self.storage.store.get("manifest", record.linked_manifest_id, Manifest)
            previous = next((entry for entry in manifest.entries if entry.path == path), None)
            if current is None:
                raise KeyError(path)
            if previous is None or (current.revision, current.size) != (
                previous.revision,
                previous.size,
            ):
                # Refresh only through the existing no-pending-edits boundary. Old
                # captures keep their original manifest and immutable resource ID.
                record = link_folder(self.storage, record)
        selected = linked_file(self.storage, record, path)
        digest = file_hash(selected)
        if revision and revision != digest:
            # Attachments are indexed by provider revision without reading their
            # bodies. Accept that exact manifest revision, then capture the bytes
            # with their SHA256 just as for notes.
            manifest_id = record.linked_manifest_id
            if not manifest_id:
                raise ValueError("The knowledge source has no linked folder revision")
            manifest = self.storage.store.get("manifest", manifest_id, Manifest)
            indexed = next((entry for entry in manifest.entries if entry.path == path), None)
            if path.lower().endswith(".md") or indexed is None or revision != indexed.revision:
                raise ValueError("The note changed since this view; refresh it before opening")
        if selected.stat().st_size > 2 * 1024 * 1024 and path.lower().endswith(".md"):
            raise ValueError("Note exceeds the 2 MiB reader limit")
        resource = source_resource(
            self.storage.store, self.resources, record, path, linked_path=selected
        )
        if resource.sha256 != digest:
            raise ValueError("The note changed while opening; refresh the knowledge view")
        if path.lower().endswith(".md"):
            text = selected.read_text(encoding="utf-8-sig")
            parsed, _, properties = parse_note(path, text, resource.sha256 or "")
            properties = json.loads(json.dumps(properties, default=str))
        else:
            text, properties = "", {}
            parsed = KnowledgeNote(
                path=path, title=Path(path).name, kind="attachment", revision=digest
            )
        snapshot = self.index.snapshot(record, focus=path)
        return {
            "source_id": source_id,
            "note": parsed.model_dump(mode="json"),
            "text": text,
            "properties": properties,
            "resource": resource.to_wire(),
            "links": [link.model_dump(mode="json") for link in snapshot.links],
            "relationships_truncated": snapshot.truncated or snapshot.relationships_truncated,
            "link_revision": record.linked_manifest_id,
            "access": record.linked_access,
        }

    def create(self, workspace_id: str, source_id: str, path: str, content: str) -> dict[str, Any]:
        """Create a new Markdown note without overwriting existing source contents."""
        record = self.record(workspace_id, source_id)
        if record.linked_access == "read_only" or not record.source.capabilities.note_write:
            raise PermissionError("This knowledge source is read only")
        if not path.lower().endswith(".md"):
            raise PermissionError("Knowledge notes must be Markdown files")
        encoded = content.encode("utf-8")
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("Note exceeds the 2 MiB reader limit")
        if not record.linked_manifest_id:
            record = link_folder(self.storage, record)
        from clio_agent.gact.storage.source_filesystem import SourceFileSystem

        fs = SourceFileSystem(
            self.storage,
            workspace_id,
            source_id,
            self.resources,
            revision=record.linked_manifest_id or "",
        )
        with fs.open(path, "xb") as writer:
            writer.write(encoded)
        return {
            "source_id": source_id,
            "path": path,
            "revision": fs.revision,
            "writes_propagate": record.linked_access == "write_through",
        }

    def edit(
        self, workspace_id: str, source_id: str, path: str, content: str, revision: str
    ) -> dict[str, Any]:
        """Reuse staged/direct edits after checking the current note content revision."""
        record = self.record(workspace_id, source_id)
        if record.linked_access == "read_only" or not record.source.capabilities.note_write:
            raise PermissionError("This knowledge source is read only")
        if not path.lower().endswith(".md"):
            raise PermissionError("Knowledge edits support Markdown notes only")
        opened = self.open(workspace_id, source_id, path, revision)
        from clio_agent.gact.storage.source_filesystem import SourceFileSystem

        fs = SourceFileSystem(
            self.storage, workspace_id, source_id, self.resources, revision=opened["link_revision"]
        )
        with fs.open(path, "wb") as writer:
            writer.write(content.encode("utf-8"))
        return {
            "source_id": source_id,
            "path": path,
            "revision": fs.revision,
            "writes_propagate": record.linked_access == "write_through",
        }
