"""Plugins Clio writes, kept in the person's private marketplace with who wrote each version.

``make_plugin`` writes a plugin into ``<store>/local/private-local/plugins/<name>``, the
same marketplace the UI's Create a skill, Upload and Compose a plugin write to
(:mod:`clio_agent.plugins.local`). Beside the store, ``<store>/authorship/<name>.json``
records every version Clio wrote: the conversation, the digest and file hashes of
exactly what it wrote, the plugin it is a variation of, and the diff from the version
before. A plugin counts as Clio-authored only while its folder still matches the last
version's digest: a file edited any other way makes it the person's.

* Writing the name of a plugin Clio wrote makes its next version.
* A name the person wrote or uploaded in ``private-local`` is ``name_taken_by_person``.
* A name another marketplace (or CLIO itself) provides is ``name_taken_by_marketplace``:
  a change to someone else's plugin is written as the person's variation, under a new
  name, with ``variation_of`` naming the original, which is never edited.

The review is read from the files without importing them (the plugin may not be
trusted yet): each flow's id, description and drawn ``shape``, each tool, hook and
script binding with what it says it does, the tool servers it starts, and what the code
reaches directly, from its imports and calls.

``migrate`` moves plugins an older CLIO wrote under ``<store>/authored/<name>`` (each its
own marketplace) into ``private-local`` once, at startup, and deletes ``authored/``.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from clio_agent.platform_paths import remove_tree
from clio_agent.plugins.loader import is_text_file
from clio_agent.plugins.marketplace import NATIVE_INDEX
from clio_agent.sdk.versions import valid_plugin_id

logger = logging.getLogger(__name__)

#: The marketplace every plugin the person owns lands in (UI and ``make_plugin``).
PRIVATE_MARKETPLACE = "private-local"
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
#: A recorded diff keeps this many characters; the rest is counted, not kept.
_DIFF_LIMIT = 200_000


class AuthoredError(ValueError):
    """A plugin that cannot be written as given; ``reason`` is the typed cause."""

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details


def private_root(store_root: Path) -> Path:
    return store_root / "local" / PRIVATE_MARKETPLACE


def plugin_folder(store_root: Path, name: str) -> Path:
    return private_root(store_root) / "plugins" / name


def _record_path(store_root: Path, name: str) -> Path:
    return store_root / "authorship" / f"{name}.json"


def _relative(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    # Checked as POSIX but written with the native path: a backslash, a drive or a
    # stream (``:``) would climb out of the folder on Windows.
    native_escape = "\\" in path or ":" in path
    if native_escape or relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise AuthoredError("path_outside_plugin", f"{path!r} is not a path inside the plugin")
    return relative


def _files(folder: Path) -> list[Path]:
    return sorted(
        p
        for p in folder.rglob("*")
        if p.is_file() and "__pycache__" not in p.relative_to(folder).parts
    )


def file_hashes(folder: Path) -> dict[str, str]:
    """Each file's sha256, by its path inside ``folder`` (caches excluded)."""

    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in _files(folder)
    }


def folder_digest(folder: Path) -> str:
    """The digest of every file under ``folder`` (names and contents), caches excluded."""

    digest = hashlib.sha256()
    for relative, sha in sorted(file_hashes(folder).items()):
        digest.update(relative.encode() + b"\0")
        digest.update(bytes.fromhex(sha))
    return digest.hexdigest()


def history(store_root: Path, name: str) -> dict[str, Any] | None:
    """Every version Clio wrote of ``name``, or ``None`` when Clio never wrote it."""

    try:
        record = json.loads(_record_path(store_root, name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthoredError(
            "authorship_unreadable", f"the authorship record of {name!r} cannot be read: {exc}"
        ) from exc
    return record if isinstance(record, dict) and record.get("versions") else None


def authorship(store_root: Path, name: str, folder: Path | None = None) -> dict[str, Any] | None:
    """The last version Clio wrote of ``name`` while ``folder`` (default: its private
    folder; an installed snapshot's copy to check what runs) still matches it."""

    record = history(store_root, name)
    folder = folder if folder is not None else plugin_folder(store_root, name)
    if record is None or not folder.is_dir():
        return None
    latest = dict(record["versions"][-1])
    if folder_digest(folder) != latest.get("digest"):
        return None
    return {"name": name, **latest, "versions": len(record["versions"])}


def _diff(before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    chunks: list[str] = []
    for path in [*changed, *added, *removed]:
        chunks.extend(
            difflib.unified_diff(
                before.get(path, "").splitlines(keepends=True),
                after.get(path, "").splitlines(keepends=True),
                fromfile=f"a/{path}" if path in before else "/dev/null",
                tofile=f"b/{path}" if path in after else "/dev/null",
            )
        )
    text = "".join(chunks)
    executable = sorted(p for p in [*added, *removed, *changed] if not is_text_file(p))
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "executable_changed": executable,
        "unified": text[:_DIFF_LIMIT],
        "unified_truncated": max(0, len(text) - _DIFF_LIMIT),
    }


def _read_texts(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in _files(folder)
    }


def _ensure_index(store_root: Path) -> None:
    index = private_root(store_root) / NATIVE_INDEX
    if index.is_file():
        return
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps(
            {"name": PRIVATE_MARKETPLACE, "description": "Your private local skills and plugins"}
        ),
        encoding="utf-8",
    )


def write(
    store_root: Path,
    name: str,
    files: dict[str, str],
    session_id: str,
    *,
    variation_of: str = "",
    taken_elsewhere: str = "",
) -> tuple[Path, dict[str, Any]]:
    """Write ``name`` into the private marketplace; returns its folder and the new version.

    ``taken_elsewhere`` names the marketplace that already provides a plugin ``name``
    (the caller reads the installed records); such a name is refused. The folder is
    replaced as a whole, so a new version leaves no stale file behind.
    """

    if not valid_plugin_id(name):
        raise AuthoredError("invalid_plugin_name", f"{name!r} is not a valid plugin name")
    if "plugin.json" not in files:
        raise AuthoredError("manifest_missing", "a plugin needs a plugin.json")
    if taken_elsewhere:
        raise AuthoredError(
            "name_taken_by_marketplace",
            f"{name!r} is a plugin of the {taken_elsewhere} marketplace. Its original is never "
            "edited: write the person's variation under a new name, with variation_of set to "
            f"{taken_elsewhere}/{name}",
            marketplace=taken_elsewhere,
        )
    relatives = {_relative(path): text for path, text in files.items()}
    folder = plugin_folder(store_root, name)
    record = history(store_root, name)
    before: dict[str, str] = {}
    if folder.exists():
        if authorship(store_root, name) is None:
            raise AuthoredError(
                "name_taken_by_person",
                f"{name!r} is a plugin the person wrote, uploaded or changed. Clio does not "
                "rewrite it: write a variation under a new name, with variation_of set to "
                f"{PRIVATE_MARKETPLACE}/{name}",
            )
        before = _read_texts(folder)
    staging = folder.parent / f".{name}.writing"
    remove_tree(staging)
    for relative, text in relatives.items():
        target = staging / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    remove_tree(folder)
    folder.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(folder)
    _ensure_index(store_root)
    versions = list(record["versions"]) if record else []
    version = {
        "session_id": session_id,
        "digest": folder_digest(folder),
        "files": file_hashes(folder),
        "written_at": datetime.now(UTC).isoformat(),
        "variation_of": variation_of or (versions[-1].get("variation_of", "") if versions else ""),
        "diff": _diff(before, _read_texts(folder)) if versions else None,
    }
    versions.append(version)
    # An authorship version is its position in the plugin's history, counting from 1.
    version["version"] = len(versions)
    path = _record_path(store_root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "versions": versions}, indent=2), encoding="utf-8")
    return folder, version


def note_installed(store_root: Path, name: str, executable_digest: str) -> None:
    """Record the executable digest the store computed for the last version."""

    record = history(store_root, name)
    if record is None:
        return
    record["versions"][-1]["executable_digest"] = executable_digest
    _record_path(store_root, name).write_text(json.dumps(record, indent=2), encoding="utf-8")


def changed_since(store_root: Path, name: str, trusted_digest: str) -> list[str] | None:
    """Executable files that differ from the last version whose executable parts were
    trusted (``trusted_digest``); ``None`` when no version Clio wrote was trusted."""

    record = history(store_root, name)
    if record is None or not trusted_digest:
        return None
    trusted = next(
        (v for v in reversed(record["versions"]) if v.get("executable_digest") == trusted_digest),
        None,
    )
    if trusted is None:
        return None
    now, then = record["versions"][-1].get("files") or {}, trusted.get("files") or {}
    return sorted(
        p for p in set(now) | set(then) if now.get(p) != then.get(p) and not is_text_file(p)
    )


# ------------------------------------------------------------------ the review


def _constant(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def _decorator(node: ast.AST) -> tuple[str, list[Any], dict[str, Any]]:
    call = node if isinstance(node, ast.Call) else None
    target = call.func if call else node
    name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
    args = [_constant(a) for a in call.args] if call else []
    kwargs = {k.arg: _constant(k.value) for k in call.keywords if k.arg} if call else {}
    return str(name), args, kwargs


def _parts(tree: ast.Module, relative: str) -> dict[str, list[dict[str, Any]]]:
    """Flows, tools, hooks and subagents a module declares, read without importing it."""

    found: dict[str, list[dict[str, Any]]] = {
        "flows": [],
        "tools": [],
        "hooks": [],
        "subagents": [],
    }
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            attrs: dict[str, Any] = {}
            for item in node.body:
                if isinstance(item, ast.Assign) and len(item.targets) == 1:
                    target = item.targets[0]
                    if isinstance(target, ast.Name):
                        attrs[target.id] = _constant(item.value)
            if isinstance(attrs.get("id"), str) and attrs["id"]:
                found["flows"].append(
                    {
                        "id": attrs["id"],
                        "description": str(attrs.get("description") or ""),
                        "shape": str(attrs.get("shape") or ""),
                        "file": relative,
                    }
                )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            doc = (ast.get_docstring(node) or "").strip().split("\n\n")[0]
            for decorator in node.decorator_list:
                name, args, kwargs = _decorator(decorator)
                if name == "tool":
                    found["tools"].append(
                        {
                            "name": str(kwargs.get("name") or node.name),
                            "description": str(kwargs.get("description") or doc),
                            "read_only": bool(kwargs.get("read_only")),
                            "file": relative,
                        }
                    )
                elif name == "hook":
                    found["hooks"].append(
                        {
                            "id": str(kwargs.get("id") or node.name),
                            "event": str(args[0] if args else kwargs.get("event") or ""),
                            "tools": str(kwargs.get("tools") or ""),
                            "description": doc,
                            "file": relative,
                        }
                    )
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            name, args, kwargs = _decorator(node.value)
            if name == "Subagent" and args and isinstance(args[0], str):
                found["subagents"].append(
                    {
                        "id": args[0],
                        "description": str(kwargs.get("description") or ""),
                        "file": relative,
                    }
                )
    return found


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


def _json(folder: Path, relative: str) -> dict[str, Any]:
    try:
        value = json.loads((folder / relative).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def review(folder: Path) -> dict[str, Any]:
    """What a person reviews before trusting the plugin in ``folder``, read without importing it."""

    manifest = _json(folder, "plugin.json")
    parts: dict[str, list[dict[str, Any]]] = {
        "flows": [],
        "tools": [],
        "hooks": [],
        "subagents": [],
    }
    reach: set[str] = set()
    unreadable: list[str] = []
    sources: dict[str, str] = {}
    executable: list[str] = []
    budget = _REVIEW_SOURCE_LIMIT
    for path in _files(folder):
        relative = path.relative_to(folder).as_posix()
        if not is_text_file(relative):
            executable.append(relative)
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
        for kind, rows in _parts(tree, relative).items():
            parts[kind].extend(rows)
        reach |= _reach(tree)
    clio = (manifest.get("extensions") or {}).get("ai.iowarp.clio") or {}
    bindings = [
        {
            "name": name,
            "source": str(row.get("source") or ""),
            "requires": row.get("requires") or [],
        }
        for name, row in (clio.get("kernel_bindings") or {}).items()
        if isinstance(row, dict)
    ]
    servers = sorted(
        {
            *_json(folder, "mcp.json").get("mcpServers", {}),
            *_json(folder, ".mcp.json").get("mcpServers", {}),
        }
    )
    return {
        "name": str(manifest.get("name") or folder.name),
        "version": str(manifest.get("version") or ""),
        "description": str(manifest.get("description") or ""),
        **parts,
        "bindings": bindings,
        "servers": servers,
        "executable_files": [p for p in executable if p != "plugin.json"],
        # Read from the code: what it reaches directly, beyond asking Clio's agents.
        "reach": sorted(reach),
        "unreadable": unreadable,
        "files": sources,
        "files_truncated": budget < 0,
    }


# ------------------------------------------------------------------ migration


def migrate(store: Any) -> list[dict[str, Any]]:
    """Move plugins an older CLIO wrote under ``<store>/authored`` into ``private-local``.

    Each keeps its authorship (one version, the conversation that wrote it) when its files
    still match what was recorded; its own marketplace is removed, and ``authored/`` with
    it. A name the private marketplace already has is kept where it is and reported.
    """

    old = store.root / "authored"
    if not old.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for folder in sorted(p for p in old.iterdir() if p.is_dir() and not p.name.startswith(".")):
        name = folder.name
        try:
            recorded = json.loads((old / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recorded = {}
        unchanged = recorded.get("digest") == folder_digest(folder)
        target = plugin_folder(store.root, name)
        if target.exists():
            results.append({"plugin": name, "moved": False, "reason": "name_taken_by_person"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        remove_tree(target)
        import shutil  # noqa: PLC0415

        shutil.copytree(folder, target, ignore=shutil.ignore_patterns("__pycache__"))
        if unchanged:
            version = {
                "version": 1,
                "session_id": str(recorded.get("session_id") or ""),
                "digest": folder_digest(target),
                "files": file_hashes(target),
                "written_at": str(recorded.get("written_at") or ""),
                "variation_of": "",
                "diff": None,
                "migrated_from": "authored",
            }
            path = _record_path(store.root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"name": name, "versions": [version]}, indent=2), encoding="utf-8"
            )
        if name in store.marketplaces():
            store.uninstall(name)
        results.append(
            {
                "plugin": name,
                "moved": True,
                "authored_by_clio": unchanged,
                # Not "unchanged": its files were edited after Clio wrote them.
                **({} if unchanged else {"reason": "changed_after_writing"}),
            }
        )
    _ensure_index(store.root)
    if any(row["moved"] for row in results):
        store.install(str(private_root(store.root)), name=PRIVATE_MARKETPLACE)
    remove_tree(old)
    for row in results:
        logger.info("authored plugin migrated to private-local: %s", row)
    return results
