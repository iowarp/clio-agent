"""Portable, user-requested backups of local plugins and saved agent variations."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from typing import TYPE_CHECKING

from clio_agent.plugins.local import MAX_BYTES, PRIVATE_MARKETPLACE
from clio_agent.plugins.marketplace import NATIVE_INDEX
from clio_agent.sdk.versions import PluginRef, valid_plugin_id

if TYPE_CHECKING:
    from clio_agent.plugins.service import PluginService


class LocalExportError(ValueError):
    """An export refusal with the status the existing download route exposes."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


def agent_source(service: PluginService, ref: str) -> str:
    """Export a variation as ordinary SDK source, preserving selection precedence."""
    entry = service.lookup(ref)
    resolution = service.resolve(ref, pin=False)
    spec = entry.spec
    active = {row["id"]: row for row in resolution.plugins}
    added = [PluginRef.parse(value).id for value in resolution.delta["add"]]
    groups: dict[str, list[str]] = {layer: [] for layer in spec.layers}

    def portable(plugin_id: str) -> str:
        row = active[plugin_id]
        # A local package remains relative when the backup gets a different name.
        market = row["marketplace"]
        name = plugin_id if market == PRIVATE_MARKETPLACE else f"{market}/{plugin_id}"
        return f"{name}@{row['version']}"

    for selected in spec.plugins:
        plugin_id = selected.ref.id
        if plugin_id in active and plugin_id not in added:
            groups.setdefault(selected.layer, []).append(portable(plugin_id))
    lines = [
        '"""Saved local agent; generated from its current plugin composition."""',
        "from clio_agent.sdk import Agent, Flow, Prefers, Requires",
        "",
    ]
    parent = "Agent"
    carried: list[str] = []
    for index, selected_refs in enumerate(groups.values()):
        if not selected_refs:
            continue
        carried.extend(selected_refs)
        name = f"_Selection{index}"
        # No own id: intermediate classes preserve depth without becoming agents.
        lines.extend([f"class {name}({parent}):", f"    plugins = {carried!r}", ""])
        parent = name
    carried.extend(portable(plugin_id) for plugin_id in added if plugin_id in active)
    agent = resolution.agent
    flow = spec.flow
    lines.extend(
        [
            f"class SavedAgent({parent}):",
            '    """Reusable saved agent exported from the local collection."""',
            f"    id = {agent['id']!r}",
            f"    name = {agent['name']!r}",
            f"    description = {agent['description']!r}",
            f"    role = {spec.role!r}",
            f"    version = {spec.version!r}",
            f"    flow = Flow({flow['id']!r}, **{flow.get('options', {})!r})",
            f"    requires = Requires.from_dict({spec.requires!r})",
            f"    prefers = Prefers.from_dict({spec.prefers!r})",
            f"    plugins = {carried!r}",
            "",
        ]
    )
    return "\n".join(lines)


def export_local_marketplace(service: PluginService) -> bytes:
    """Export local source and portable agents without changing the live collection."""
    root = service.store.root / "local" / PRIVATE_MARKETPLACE
    files: dict[str, tuple[bytes, int]] = {}
    limit = 16 * MAX_BYTES
    size = 0

    def add(path: str, data: bytes, mode: int = 0o100644) -> None:
        nonlocal size
        if any(existing.casefold() == path.casefold() for existing in files):
            raise LocalExportError(409, f"Two saved entries use the export path {path!r}")
        size += len(data)
        if size > limit:
            raise LocalExportError(
                413, "Use the marketplace folder to back up this larger collection"
            )
        files[path] = (data, mode)

    with service.store.lock:
        if root.is_symlink():
            raise LocalExportError(409, "The local marketplace contains an external link")
        if root.is_dir():
            resolved = root.resolve()
            for path in sorted(root.rglob("*")):
                if path.is_symlink() or not path.resolve().is_relative_to(resolved):
                    raise LocalExportError(409, "The local marketplace contains an external link")
                if path.is_file():
                    if path.stat().st_size > limit - size:
                        raise LocalExportError(
                            413, "Use the marketplace folder to back up this larger collection"
                        )
                    add(path.relative_to(root).as_posix(), path.read_bytes(), path.stat().st_mode)
        for ref, saved in sorted(service.store.user_agents().items()):
            if not valid_plugin_id(saved.id):
                raise LocalExportError(409, "A saved agent has an invalid export name")
            stem = re.sub(r"[^a-z0-9_]", "_", saved.id)[:48]
            suffix = hashlib.sha256(saved.id.encode()).hexdigest()[:8]
            add(f"agents/{stem}_{suffix}.py", agent_source(service, ref).encode())
            add(f"user-agents/{saved.id}.json", json.dumps(saved.to_dict(), indent=2).encode())
        if not files:
            raise LocalExportError(404, "Save an agent, skill or plugin first")
        index_path = NATIVE_INDEX.as_posix()
        if index_path not in files:
            add(index_path, json.dumps({"name": PRIVATE_MARKETPLACE}).encode())
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_path, (data, mode) in sorted(files.items()):
            entry = zipfile.ZipInfo(archive_path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (mode & 0xFFFF) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    return output.getvalue()
