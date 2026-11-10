"""Incremental vault indexing in CLIO-owned storage, never inside the source vault."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import yaml
from clio_schemas.knowledge import (
    KnowledgeGraphSnapshot,
    KnowledgeLink,
    KnowledgeNote,
    KnowledgeSnapshot,
)

from clio_agent.gact.storage.models import FileEntry, LinkedEdits, SourceRecord
from clio_agent.gact.storage.obsidian import ObsidianSource, vault_path
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.vault_parser import parse_note, resolve_links
from clio_agent.platform_paths import atomic_replace


class KnowledgeIndex:
    """Persist content-addressed note metadata and rebuild relations after changes."""

    def __init__(self, root: Path, store: SourceStore | None = None) -> None:
        self.root = root
        self.store = store
        self._lock = threading.RLock()
        self._texts: dict[str, str] = {}

    def snapshot(
        self,
        record: SourceRecord,
        *,
        query: str = "",
        focus: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> KnowledgeSnapshot:
        """Refresh changed notes and return a bounded query or link neighborhood."""
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("Knowledge pages require offset >= 0 and limit 1..500")
        selected, links, warnings, revision = self._selection(record, query=query, focus=focus)
        page = selected[offset : offset + limit]
        paths = {row.path for row in page}
        page_links = [
            link
            for link in links
            if link.source in paths
            and (link.status != "resolved" or link.target in paths)
            and (
                (link.source == focus or link.target == focus)
                if focus
                else link.status != "external"
            )
        ]
        return KnowledgeSnapshot(
            source_id=record.source.id,
            revision=revision,
            notes=page,
            links=page_links[:2000],
            total=len(selected),
            offset=offset,
            next_offset=offset + limit if offset + limit < len(selected) else None,
            truncated=len(page) < len(selected),
            relationships_truncated=len(page_links) > 2000,
            warnings=warnings,
        )

    def graph(
        self, record: SourceRecord, *, query: str = "", focus: str = "", attachments: bool = False
    ) -> KnowledgeGraphSnapshot:
        """Return bounded source topology including connections across file pages."""
        selected, links, warnings, revision = self._selection(record, query=query, focus=focus)
        selected = [note for note in selected if attachments or note.kind == "note"]
        notes = [
            note.model_copy(update={"excerpt": "", "headings": [], "blocks": [], "aliases": []})
            for note in selected[:5000]
        ]
        paths = {note.path for note in notes}
        unique: dict[tuple[str, str], KnowledgeLink] = {}
        for link in links:
            if (
                link.status == "resolved"
                and link.source in paths
                and link.target in paths
                and link.source != link.target
                and (not focus or focus in (link.source, link.target))
            ):
                unique.setdefault((link.source, link.target), link)
        return KnowledgeGraphSnapshot(
            source_id=record.source.id,
            revision=revision,
            notes=notes,
            links=list(unique.values())[:20000],
            total=len(selected),
            truncated=len(notes) < len(selected),
            relationships_truncated=len(unique) > 20000,
            warnings=warnings,
        )

    def _selection(
        self, record: SourceRecord, *, query: str, focus: str
    ) -> tuple[list[KnowledgeNote], list[KnowledgeLink], list[str], str]:
        """Use the same policy, index and search semantics for pages and topology."""
        if record.source.source_kind != "knowledge" or not record.connected:
            raise PermissionError("Connect a knowledge source before using knowledge operations")
        with self._lock:
            rows, links, warnings, revision = self._refresh(record)
            texts = self._texts.copy()
        selected = rows
        if focus:
            if focus not in {row.path for row in rows}:
                raise KeyError(focus)
            paths = {focus}
            paths.update(
                link.target for link in links if link.source == focus and link.status == "resolved"
            )
            paths.update(
                link.source for link in links if link.target == focus and link.status == "resolved"
            )
            selected = [row for row in rows if row.path in paths]
            selected.sort(key=lambda row: row.path != focus)
        if query.strip():
            terms = query.casefold().split()
            selected = [
                row
                for row in selected
                if all(
                    term in row.model_dump_json().casefold() + texts.get(row.path, "")
                    for term in terms
                )
            ]
        return selected, links, warnings, revision

    def note(self, record: SourceRecord, path: str) -> tuple[KnowledgeNote, str, dict[str, Any]]:
        """Read a current note under the same vault boundary and content limit."""
        adapter = ObsidianSource(
            record.source.root, excluded=record.configuration.obsidian_excluded_folders
        )
        entry = adapter.metadata(path)
        if entry is None or not path.lower().endswith(".md"):
            raise KeyError(path)
        return self._read_note(adapter, entry)

    def _read_note(
        self, adapter: ObsidianSource, entry: FileEntry
    ) -> tuple[KnowledgeNote, str, dict[str, Any]]:
        if entry.size is not None and entry.size > 2 * 1024 * 1024:
            raise ValueError("Note exceeds the 2 MiB reader limit")
        with adapter.open_read(entry) as reader:
            data = reader.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Note exceeds the 2 MiB reader limit")
        text = data.decode("utf-8-sig")
        note, _, properties = parse_note(entry.path, text, hashlib.sha256(data).hexdigest())
        return note, text, properties

    def _refresh(
        self, record: SourceRecord
    ) -> tuple[list[KnowledgeNote], list[KnowledgeLink], list[str], str]:
        adapter = ObsidianSource(
            record.source.root, excluded=record.configuration.obsidian_excluded_folders
        )
        entries = [row for row in adapter.entries() if row.kind == "file"]
        edits = None
        if self.store is not None and record.linked_access == "publish_later":
            try:
                edits = self.store.get("linked-edits", record.source.id, LinkedEdits)
            except KeyError:
                pass
        if edits is not None:
            upstream = {entry.path for entry in entries}
            for path, change in edits.changes.items():
                if path in upstream or change.content_path is None:
                    continue
                try:
                    vault_path(path, adapter.excluded)
                except PermissionError:
                    continue
                if len(entries) >= 100_000:
                    raise ValueError("Select a smaller vault (limit 100,000 entries)")
                entries.append(FileEntry(path=path, kind="file"))
            entries.sort(key=lambda entry: entry.path)
        location = self.root / record.source.id / "knowledge-index.json"
        old: dict[str, Any] = {}
        if location.is_file():
            try:
                old = json.loads(location.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                old = {}
        cache: dict[str, Any] = {}
        notes: list[KnowledgeNote] = []
        authored: list[KnowledgeLink] = []
        warnings: list[str] = []
        text_bytes = 0
        for entry in entries:
            edit = edits.changes.get(entry.path) if edits is not None else None
            if edit is not None:
                if edit.content_path is None:
                    continue
                entry = entry.model_copy(update={"revision": "local:" + str(edit.after_hash)})
            if not entry.path.lower().endswith(".md"):
                notes.append(
                    KnowledgeNote(
                        path=entry.path,
                        title=Path(entry.path).name,
                        kind="attachment",
                        revision=entry.revision,
                    )
                )
                continue
            previous = old.get(entry.path, {})
            if previous.get("stat") == entry.revision:
                cached = previous
            else:
                try:
                    if edit is not None and edit.content_path:
                        staged = Path(edit.content_path)
                        staged.resolve().relative_to((self.root / record.source.id).resolve())
                        if staged.stat().st_size > 2 * 1024 * 1024:
                            raise ValueError("Note exceeds the 2 MiB index limit")
                        data = staged.read_bytes()
                        text = data.decode("utf-8-sig")
                        note, _, _ = parse_note(entry.path, text, hashlib.sha256(data).hexdigest())
                    else:
                        note, text, _ = self._read_note(adapter, entry)
                    _, links, _ = parse_note(entry.path, text, note.revision)
                    cached = {
                        "stat": entry.revision,
                        "note": note.model_dump(),
                        "links": [link.model_dump() for link in links],
                        "text": text.casefold(),
                    }
                except (ValueError, UnicodeError, OSError, yaml.YAMLError) as exc:
                    warnings.append(f"{entry.path}: {type(exc).__name__}")
                    continue
            text_bytes += len(cached["text"].encode())
            if text_bytes > 128 * 1024 * 1024:
                warnings.append(
                    "Vault text exceeds the 128 MiB index limit; exclude folders to narrow it"
                )
                break
            cache[entry.path] = cached
            note = KnowledgeNote.model_validate(cached["note"])
            notes.append(note)
            authored.extend(KnowledgeLink.model_validate(link) for link in cached["links"])
        location.parent.mkdir(parents=True, exist_ok=True)
        if cache != old:
            temporary = location.with_suffix(".tmp")
            temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            atomic_replace(temporary, location)
        revision = hashlib.sha256(
            "\n".join(f"{note.path}\0{note.revision}" for note in notes).encode()
        ).hexdigest()
        self._texts = {path: row["text"] for path, row in cache.items()}
        prefix = adapter.root.relative_to(adapter.vault_root).as_posix()
        return (
            notes,
            resolve_links(notes, authored, vault_prefix=prefix if prefix != "." else ""),
            warnings,
            revision,
        )
