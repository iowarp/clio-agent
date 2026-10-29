"""Plugins Clio writes in a conversation, and the facts a person needs to trust one.

``make_plugin`` writes a plugin's files here, under ``<store>/authored/<name>``,
and records the digest of exactly what it wrote with the conversation that wrote
it. A plugin counts as Clio-authored only while its folder still matches that
digest: a file edited any other way, or any other folder, is a third-party
plugin and takes the ordinary trust step.

The review is read from the files without importing them (the plugin is not
trusted yet): each flow's id, description and the railroad ``shape`` its author
drew, and what the code reaches directly, from its imports and calls.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from clio_agent.sdk.versions import valid_plugin_id

#: What a module reaches when it imports one of these (top-level names).
_REACH_IMPORTS = {
    "files": {"os", "pathlib", "shutil", "io", "tempfile", "glob", "fileinput"},
    # ``os`` can run commands too (os.system), so it counts as both.
    "commands": {"os", "subprocess", "pty", "multiprocessing"},
    "network": {"socket", "http", "urllib", "requests", "httpx", "aiohttp", "ftplib", "smtplib"},
    "dynamic code": {"importlib", "ctypes", "pickle", "marshal"},
}
#: Builtin calls that reach the same things without an import.
_REACH_CALLS = {"open": "files", "eval": "dynamic code", "exec": "dynamic code"}
_REACH_CALLS |= {"__import__": "dynamic code", "compile": "dynamic code"}
#: The review carries the source files up to this many characters in all.
_REVIEW_SOURCE_LIMIT = 100_000


class AuthoredError(ValueError):
    """A plugin that cannot be written as given; ``reason`` is the typed cause."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def authored_root(store_root: Path) -> Path:
    return store_root / "authored"


def _relative(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    # Checked as POSIX but written with the native path: a backslash, a drive or a
    # stream (``:``) would climb out of the folder on Windows.
    native_escape = "\\" in path or ":" in path
    if native_escape or relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise AuthoredError("path_outside_plugin", f"{path!r} is not a path inside the plugin")
    return relative


def folder_digest(folder: Path) -> str:
    """The digest of every file under ``folder`` (names and contents), caches excluded."""

    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        relative = path.relative_to(folder)
        if "__pycache__" in relative.parts:
            continue
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def write(store_root: Path, name: str, files: dict[str, str], session_id: str) -> Path:
    """Write the plugin ``name`` from ``files`` and record who wrote it; returns its folder.

    The folder is replaced as a whole, so a rewrite leaves no stale file behind.
    """

    if not valid_plugin_id(name):
        raise AuthoredError("invalid_plugin_name", f"{name!r} is not a valid plugin name")
    if "plugin.json" not in files:
        raise AuthoredError("manifest_missing", "a plugin needs a plugin.json")
    relatives = {_relative(path): text for path, text in files.items()}
    root = authored_root(store_root)
    folder = root / name
    staging = root / f".{name}.writing"
    shutil.rmtree(staging, ignore_errors=True)
    for relative, text in relatives.items():
        target = staging / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    shutil.rmtree(folder, ignore_errors=True)
    staging.rename(folder)
    record = {
        "name": name,
        "session_id": session_id,
        "digest": folder_digest(folder),
        "written_at": datetime.now(UTC).isoformat(),
    }
    (root / f"{name}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return folder


def authorship(store_root: Path, name: str) -> dict[str, Any] | None:
    """The record of the conversation that wrote ``name``, while its files are unchanged."""

    root = authored_root(store_root)
    try:
        record = json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    folder = root / name
    if not folder.is_dir() or folder_digest(folder) != record.get("digest"):
        return None
    return record


def _constant(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def _flows(tree: ast.Module) -> list[dict[str, str]]:
    """Flow classes declared in a module: their id, description and drawn shape."""

    flows = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        attrs: dict[str, Any] = {}
        for item in node.body:
            if isinstance(item, ast.Assign) and len(item.targets) == 1:
                target = item.targets[0]
                if isinstance(target, ast.Name):
                    attrs[target.id] = _constant(item.value)
        if isinstance(attrs.get("id"), str) and attrs["id"]:
            flows.append(
                {
                    "id": attrs["id"],
                    "description": str(attrs.get("description") or ""),
                    "shape": str(attrs.get("shape") or ""),
                }
            )
    return flows


def _reach(tree: ast.Module) -> set[str]:
    reached: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules = [node.module]
        for module in modules:
            top = module.split(".", 1)[0]
            reached |= {kind for kind, names in _REACH_IMPORTS.items() if top in names}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            kind = _REACH_CALLS.get(node.func.id)
            if kind:
                reached.add(kind)
    return reached


def review(folder: Path) -> dict[str, Any]:
    """What a person reviews before trusting the plugin in ``folder``, read without importing it."""

    manifest = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
    flows: list[dict[str, str]] = []
    reach: set[str] = set()
    unreadable: list[str] = []
    sources: dict[str, str] = {}
    budget = _REVIEW_SOURCE_LIMIT
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        relative = path.relative_to(folder).as_posix()
        if "__pycache__" in relative:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if budget > 0:
            sources[relative] = text[:budget]
            budget -= len(text)
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            unreadable.append(relative)
            continue
        flows.extend({**row, "file": relative} for row in _flows(tree))
        reach |= _reach(tree)
    return {
        "name": str(manifest.get("name") or folder.name),
        "version": str(manifest.get("version") or ""),
        "description": str(manifest.get("description") or ""),
        "flows": flows,
        # Read from the code: what it reaches directly, beyond asking Clio's agents.
        "reach": sorted(reach),
        "unreadable": unreadable,
        "files": sources,
        "files_truncated": budget < 0,
    }
