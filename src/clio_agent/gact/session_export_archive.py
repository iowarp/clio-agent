"""Session-scoped effect selection and streamed, portable ZIP archives."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact.artifacts.export import register_export_gc_roots
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.storage import resolve_owned_artifact_path
from clio_agent.gact.session_export_viewer import write_archive_review

if TYPE_CHECKING:
    from fastapi import FastAPI

ExportMode = Literal["transcript", "effects", "full"]
_EXCLUDED_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".clio"})


def _segment(value: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in value).strip(".") or "file"


def _roots(app: FastAPI, wid: str) -> list[Path]:
    ws = app.state.workspaces.get(wid)
    if ws is None:
        return []
    values = [ws.root_path, *(ws.config.get("granted_write_roots", []) or [])]
    return list(dict.fromkeys(Path(p).resolve() for p in values if str(p).strip()))


def _artifact_ids(value: Any) -> set[str]:
    if isinstance(value, str) and value.startswith("artifact://artifact_"):
        return {value.removeprefix("artifact://").split("/")[0]}
    if isinstance(value, dict):
        return {
            str(item)
            for key, item in value.items()
            if key == "artifact_id" and isinstance(item, str) and item
        }.union(*(_artifact_ids(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_artifact_ids(item) for item in value))
    return set()


def _effects(app: FastAPI, transcript: dict[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
    registry = get_registry(app)
    ids = _artifact_ids(transcript)
    transforms: dict[str, dict[str, Any]] = {}
    for row in [transcript, *transcript["children"]]:
        sid = row["session"]["id"]
        ids.update(registry.used_artifact_ids_for_session(sid))
        for transform in registry.transforms_for_session(sid):
            transforms[transform.call_id] = transform.to_payload()
            ids.update(
                edge.artifact_id
                for edge in [*transform.used, *transform.generated]
                if edge.artifact_id
            )
        # Minted versions without a transform still have a producer session.
        for record in registry.list_for_workspace(row["session"]["workspace_id"]):
            ids.update(
                v.artifact_id for v in record.versions if v.producer.get("session_id") == sid
            )
    # Close only the producing inputs, not unrelated versions or sibling outputs.
    visited: set[str] = set()
    pending = list(ids)
    while pending:
        aid = pending.pop()
        if aid in visited:
            continue
        visited.add(aid)
        found = registry.get_by_artifact_id(aid)
        if found is None:
            continue
        _, version = found
        call_id = str(version.producer.get("call_id") or "")
        transform = registry.get_transform(call_id)
        if transform is not None:
            transforms[call_id] = transform.to_payload()
            for edge in transform.used:
                if edge.artifact_id and edge.artifact_id not in ids:
                    ids.add(edge.artifact_id)
                    pending.append(edge.artifact_id)
    return ids, list(transforms.values())


def _write_file(
    archive: zipfile.ZipFile, source: Path, name: str, *, expected_sha: str | None = None
) -> dict[str, Any]:
    before = source.stat()
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as stream, archive.open(name, "w", force_zip64=True) as target:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            target.write(chunk)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"file changed during export: {source}")
    sha = digest.hexdigest()
    if expected_sha and sha != expected_sha:
        raise ValueError(f"recorded artifact bytes changed: {source}")
    return {"archive_path": name, "bytes": size, "sha256": sha}


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _walk_error(error: OSError) -> None:
    raise error


def build_archive(
    app: FastAPI,
    transcript: dict[str, Any],
    mode: ExportMode,
    visual_review: dict[str, Any] | None = None,
) -> Path:
    """Write an archive to a temporary file using bounded-memory file copies.

    Effects includes exact recorded versions and consumed inputs. Full adds each
    registered source folder at export time. Links are recorded, never followed.
    A read failure or a changed hash fails the download, rather than shipping bad bytes.
    """
    registry = get_registry(app)
    ids, transforms = _effects(app, transcript) if mode != "transcript" else (set(), [])
    transcript = {**transcript, "mode": mode, "transforms": transforms}
    manifest: dict[str, Any] = {
        "schema": "clio.session-export.manifest.v1",
        "mode": mode,
        "session_id": transcript["session"]["id"],
        "files": [],
        "artifacts": [],
        "skill_files": [],
        "workspace_folders": [],
        "omissions": [],
        "workspace_snapshot": "export_time" if mode == "full" else "not_included",
        "excluded_directories": sorted(_EXCLUDED_DIRS) if mode == "full" else [],
    }
    fd, raw_path = tempfile.mkstemp(prefix="clio-session-export-", suffix=".zip")
    os.close(fd)
    path = Path(raw_path)
    shas: dict[str, set[str]] = {}
    try:
        with zipfile.ZipFile(
            path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            for aid in sorted(ids):
                found = registry.get_by_artifact_id(aid)
                if found is None:
                    manifest["omissions"].append(
                        {"artifact_id": aid, "reason": "artifact_unavailable"}
                    )
                    continue
                record, version = found
                roots = _roots(app, record.workspace_id)
                owned = resolve_owned_artifact_path(
                    app, version, workspace_root=roots[0] if roots else None
                )
                candidate = Path(version.path).resolve() if version.path else None
                source = owned or (
                    candidate
                    if candidate and any(candidate.is_relative_to(root) for root in roots)
                    else None
                )
                item = {
                    "name": record.name,
                    "workspace_id": record.workspace_id,
                    **version.model_dump(mode="json"),
                }
                if source is None or not source.is_file():
                    item["archive_path"] = None
                    manifest["omissions"].append(
                        {"artifact_id": aid, "reason": "recorded_bytes_unavailable"}
                    )
                else:
                    name = f"effects/{_segment(aid)}/{_segment(Path(record.name).name)}"
                    entry = _write_file(archive, source, name, expected_sha=version.sha256)
                    manifest["files"].append(entry)
                    item["archive_path"] = name
                    if version.sha256:
                        shas.setdefault(record.workspace_id, set()).add(version.sha256)
                manifest["artifacts"].append(item)
            # Unregistered source files can still be precise consumed inputs:
            # the transform/context ledgers name them explicitly. Capture only
            # those contained paths, never strings guessed out of shell commands.
            inputs: dict[str, str | None] = {}
            for transform in transforms:
                for edge in transform.get("used", []):
                    if edge.get("path") and not edge.get("artifact_id"):
                        inputs[edge["path"]] = edge.get("sha256")
            if mode != "transcript":
                for row in [transcript, *transcript["children"]]:
                    for context in row["context_files"]:
                        if context.get("path"):
                            inputs.setdefault(context["path"], None)
            allowed = [
                root
                for row in [transcript, *transcript["children"]]
                for root in _roots(app, row["session"]["workspace_id"])
            ]
            for original, sha in sorted(inputs.items()):
                source = Path(original).resolve()
                if not any(source.is_relative_to(root) for root in allowed):
                    manifest["omissions"].append(
                        {"path": original, "reason": "input_outside_workspace"}
                    )
                    continue
                if not source.is_file():
                    manifest["omissions"].append(
                        {"path": original, "reason": "consumed_input_unavailable"}
                    )
                    continue
                name = f"effects/inputs/{hashlib.sha256(original.encode()).hexdigest()[:16]}/{_segment(source.name)}"
                entry = _write_file(archive, source, name, expected_sha=sha)
                entry["source_path"] = original
                entry["snapshot"] = "recorded_hash" if sha else "export_time"
                manifest["files"].append(entry)
            if mode == "full":
                wids = {
                    row["session"]["workspace_id"] for row in [transcript, *transcript["children"]]
                }
                for wid in sorted(wids):
                    roots = _roots(app, wid)
                    if not roots:
                        manifest["omissions"].append(
                            {"workspace_id": wid, "reason": "workspace_unavailable"}
                        )
                    for index, root in enumerate(roots):
                        prefix = f"workspace/{_segment(wid)}/{index}"
                        manifest["workspace_folders"].append(
                            {"workspace_id": wid, "source_path": str(root), "archive_path": prefix}
                        )
                        if not root.is_dir():
                            raise OSError(f"workspace folder unavailable: {root}")
                        for directory, dirs, files in os.walk(
                            root, followlinks=False, onerror=_walk_error
                        ):
                            base = Path(directory)
                            for name in list(dirs):
                                candidate = base / name
                                if name in _EXCLUDED_DIRS or _is_link(candidate):
                                    dirs.remove(name)
                                    manifest["omissions"].append(
                                        {
                                            "path": str(candidate),
                                            "reason": "excluded_directory"
                                            if name in _EXCLUDED_DIRS
                                            else "link_not_followed",
                                        }
                                    )
                            for name in files:
                                source = base / name
                                if _is_link(source):
                                    manifest["omissions"].append(
                                        {"path": str(source), "reason": "link_not_followed"}
                                    )
                                    continue
                                if not stat.S_ISREG(source.stat().st_mode):
                                    manifest["omissions"].append(
                                        {"path": str(source), "reason": "not_regular_file"}
                                    )
                                    continue
                                if not source.resolve().is_relative_to(root):
                                    raise ValueError(f"workspace file escaped its root: {source}")
                                entry = _write_file(
                                    archive,
                                    source,
                                    f"{prefix}/{source.relative_to(root).as_posix()}",
                                )
                                manifest["files"].append(entry)
            for name, data in {
                "transcript.json": json.dumps(transcript, ensure_ascii=False, indent=2).encode(),
                **{
                    f"traces/{_segment(row['session']['id'])}.semantic.jsonl": (
                        "".join(
                            json.dumps(event, ensure_ascii=False) + "\n"
                            for event in row["semantic_events"]
                        )
                    ).encode()
                    for row in [transcript, *transcript["children"]]
                },
            }.items():
                archive.writestr(name, data)
                manifest["files"].append(
                    {
                        "archive_path": name,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            for row in [transcript, *transcript["children"]]:
                for skill in row["loaded_skills"]:
                    if not isinstance(skill.get("content"), str):
                        continue
                    name = (
                        f"skills/{_segment(row['session']['id'])}/{_segment(skill['event_id'])}.md"
                    )
                    content = skill["content"].encode("utf-8")
                    archive.writestr(name, content)
                    manifest["skill_files"].append(
                        {"event_id": skill["event_id"], "archive_path": name}
                    )
                    manifest["files"].append(
                        {
                            "archive_path": name,
                            "bytes": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    )
            readme = (
                "Extract this ZIP and open index.html for offline review. transcript.json contains the raw recorded history, including children, tools and loaded skill content. traces/ holds the full semantic events and skills/ holds the recorded loaded bodies. manifest.json maps original IDs and paths to portable payloads and checksums. Effects contains recorded artifact versions and consumed inputs; Full also contains current workspace source folders, excluding the listed runtime/VCS directories and links. Missing historical records and unavailable versions are declared, never reconstructed from today's skill packages. Importing transcript.json into CLIO restores messages/context references; the archive is a review record, not an executable replay or automatic workspace restore.\n"
            ).encode()
            archive.writestr("README.txt", readme)
            manifest["files"].append(
                {
                    "archive_path": "README.txt",
                    "bytes": len(readme),
                    "sha256": hashlib.sha256(readme).hexdigest(),
                }
            )
            write_archive_review(archive, transcript, manifest, visual_review)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for wid, hashes in shas.items():
            register_export_gc_roots(app, wid, hashes)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise
