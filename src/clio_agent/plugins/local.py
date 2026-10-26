"""Create local extensions without modifying their installed upstream sources."""

from __future__ import annotations

import io
import json
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from clio_agent.plugins.manifest import NAMESPACE, PLUGIN_SCHEMA
from clio_agent.plugins.marketplace import NATIVE_INDEX, read_marketplace
from clio_agent.plugins.store import PluginStore
from clio_agent.sdk.versions import valid_plugin_id

MAX_BYTES = 2 * 1024 * 1024
MAX_FILES = 128
PRIVATE_MARKETPLACE = "private-local"


def archive_files(raw: bytes) -> dict[str, str]:
    """Read a bounded UTF-8 extension archive, rejecting links and unsafe paths."""
    if len(raw) > MAX_BYTES:
        raise ValueError("Upload a ZIP smaller than 2 MiB, or install its repository")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = [row for row in archive.infolist() if not row.is_dir()]
            if len(entries) > MAX_FILES or sum(row.file_size for row in entries) > MAX_BYTES:
                raise ValueError("The uncompressed extension exceeds 128 files or 2 MiB")
            if any(stat.S_ISLNK(row.external_attr >> 16) for row in entries):
                raise ValueError("Extension archives cannot contain symbolic links")
            files = {}
            seen: set[str] = set()
            for row in entries:
                key = _relative(row.orig_filename)
                if key.casefold() in seen:
                    raise ValueError("The archive contains duplicate file paths")
                seen.add(key.casefold())
                files[key] = archive.read(row).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, RuntimeError) as exc:
        raise ValueError(
            "Upload a valid ZIP of UTF-8 source files, or install its repository"
        ) from exc
    # Common downloaded skill archives contain one enclosing folder.
    roots = {PurePosixPath(key).parts[0] for key in files}
    if len(roots) == 1 and all(len(PurePosixPath(key).parts) > 1 for key in files):
        files = {
            str(PurePosixPath(*PurePosixPath(key).parts[1:])): value for key, value in files.items()
        }
    return files


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or char in '<>"|?*' for char in value)
        or any(
            part.endswith((".", " "))
            or part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *{f"COM{i}" for i in range(1, 10)},
                *{f"LPT{i}" for i in range(1, 10)},
            }
            for part in path.parts
        )
        or any(part in {".git", ".venv", "node_modules"} for part in path.parts)
    ):
        raise ValueError("Every extension file must stay inside its own folder")
    return path.as_posix()


def create_local(store: PluginStore, body: dict[str, Any]) -> dict[str, Any]:
    """Install a new user-owned extension through the ordinary store and trust lifecycle."""
    name = body.get("name")
    if not isinstance(name, str) or not valid_plugin_id(name):
        raise ValueError("Choose a lowercase name using letters, numbers, dots or hyphens")
    _relative(name)
    files = body.get("files")
    if files is None:
        manifest = {
            "$schema": PLUGIN_SCHEMA,
            "name": name,
            "version": "1.0.0",
            "description": str(body.get("description") or ""),
            "extensions": {
                NAMESPACE: {
                    "requires": body.get("requires") or [],
                    "connections": body.get("connections") or {},
                }
            },
        }
        files = {"plugin.json": json.dumps(manifest, indent=2)}
        if body.get("skill"):
            files[f"skills/{name}/SKILL.md"] = str(body["skill"])
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
        raise ValueError("Provide between 1 and 128 source files")
    clean: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in files.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Source files must have text paths and UTF-8 text contents")
        relative = _relative(key)
        if relative.casefold() in seen:
            raise ValueError("Source files must have unique portable paths")
        seen.add(relative.casefold())
        clean[relative] = value
    if sum(len(value.encode("utf-8")) for value in clean.values()) > MAX_BYTES:
        raise ValueError("Local extensions must be smaller than 2 MiB")
    if "plugin.json" in clean:
        manifest = json.loads(clean["plugin.json"])
        if not isinstance(manifest, dict):
            raise ValueError("plugin.json must contain a plugin object")
        manifest["name"] = name
        clean["plugin.json"] = json.dumps(manifest, indent=2)
    if ".claude-plugin/plugin.json" in clean:
        manifest = json.loads(clean[".claude-plugin/plugin.json"])
        if not isinstance(manifest, dict):
            raise ValueError(".claude-plugin/plugin.json must contain a plugin object")
        manifest["name"] = name
        clean[".claude-plugin/plugin.json"] = json.dumps(manifest, indent=2)
    with store.lock:
        root = store.root / "local" / PRIVATE_MARKETPLACE
        folder = root / "plugins" / name
        existing = store.marketplaces().get(PRIVATE_MARKETPLACE)
        if existing and Path(str(existing.get("source", ""))).resolve() != root.resolve():
            raise ValueError("The private-local marketplace name belongs to another source")
        if folder.exists() or (store.root / "local" / name).exists():
            raise ValueError("That name already exists. Save this as a new variation instead")
        try:
            for relative, text in clean.items():
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            contents = read_marketplace(folder, trusted=False, name_hint=PRIVATE_MARKETPLACE)
            if len(contents.plugins) != 1 or any(
                row.severity == "error" for row in contents.diagnostics
            ):
                raise ValueError("Upload one complete skill or plugin folder")
            index = root / NATIVE_INDEX
            index.parent.mkdir(parents=True, exist_ok=True)
            index.write_text(
                json.dumps(
                    {
                        "name": PRIVATE_MARKETPLACE,
                        "description": "Your private local skills and plugins",
                    }
                ),
                encoding="utf-8",
            )
            return store.install(str(root), name=PRIVATE_MARKETPLACE)
        except Exception:
            # Only this call's newly created directory is owned by the operation.
            if folder.resolve().is_relative_to((store.root / "local").resolve()):
                shutil.rmtree(folder, ignore_errors=True)
            raise
