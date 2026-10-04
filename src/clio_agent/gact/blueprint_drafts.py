"""Separate blueprint authoring drafts from installed runtime revisions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Collection
from pathlib import Path
from typing import Any

from clio_agent import paths
from clio_agent.gact.agent_blueprint_files import (
    _BLUEPRINT_TEXT_FILE_LIMIT_BYTES,
    BlueprintFileNotTextError,
    BlueprintFileTooLargeError,
    is_textual_blueprint_file,
    resolve_blueprint_file_path,
    write_blueprint_text_file,
)
from clio_agent.gact.agent_blueprints import (
    _install_candidates,
    parse_agent_blueprint_root,
    read_install_metadata,
    validate_agent_blueprint_path,
)
from clio_agent.gact.blueprint_identity import identity_fields
from clio_agent.gact.blueprint_install_files import blueprint_files, copy_blueprint_tree
from clio_agent.gact.blueprint_ledgers import write_json_atomic
from clio_agent.gact.blueprint_mutations import BLUEPRINT_MUTATION_LOCK
from clio_agent.platform_paths import (
    rename_extended,
    win_extended_path,
)


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def draft_directory(root: Path) -> Path:
    """Return namespaced authoring storage for this exact installed identity."""
    identity = identity_fields(parse_agent_blueprint_root(root, scope="session"))["identity"]
    key = f"{identity}\n{root.resolve()}"
    return paths.user_state_dir() / "blueprint-drafts" / _hash(key.encode())


def draft_view(root: Path) -> Path:
    """Prefer the user's saved draft, falling back to the installed revision."""
    draft = draft_directory(root) / "tree"
    return draft if draft.is_dir() else root


def read_draft(root: Path, relative: str) -> dict[str, str]:
    """Return editable content and its byte hash for optimistic concurrency."""
    with BLUEPRINT_MUTATION_LOCK:
        view = draft_view(root)
        if view != root:
            _refresh_clean_files(root, view)
        else:
            try:
                view = authoring_root(root)
            except ValueError:
                view = root
        target = resolve_blueprint_file_path(view, relative)
    with target.open("rb") as handle:
        raw = handle.read(_BLUEPRINT_TEXT_FILE_LIMIT_BYTES + 1)
    if len(raw) > _BLUEPRINT_TEXT_FILE_LIMIT_BYTES:
        raise BlueprintFileTooLargeError(relative)
    if not is_textual_blueprint_file(target.name, raw):
        raise BlueprintFileNotTextError(relative)
    return {"content": raw.decode("utf-8"), "content_hash": _hash(raw)}


def _hashes(root: Path) -> dict[str, str]:
    return {
        relative.as_posix(): _hash(Path(filename).read_bytes())
        for relative, filename in blueprint_files(root)
        if ".git" not in relative.parts and relative.name != ".clio-install.md"
    }


def _manifest(owner: Path) -> dict[str, Any]:
    value = json.loads((owner / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("baseline"), dict):
        raise ValueError("The draft manifest is invalid; retain it for recovery")
    return value


def _refresh_clean_files(root: Path, draft: Path) -> None:
    """Refresh only unedited files; dirty files keep their original conflict baseline."""
    try:
        source = authoring_root(root)
    except ValueError:
        return
    owner = draft.parent
    manifest = _manifest(owner)
    baseline = dict(manifest["baseline"])
    for name, checksum in baseline.items():
        target = resolve_blueprint_file_path(draft, name)
        if not target.is_file() or _hash(target.read_bytes()) != checksum:
            continue
        upstream = resolve_blueprint_file_path(source, name)
        if not upstream.is_file():
            continue  # Publication detects a removed upstream file; never resurrect it.
        raw = upstream.read_bytes()
        if _hash(raw) == checksum:
            continue
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            pending = Path(temporary.name)
            temporary.write(raw)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)
        baseline[name] = _hash(raw)
    write_json_atomic(owner / "manifest.json", {**manifest, "baseline": baseline})


def authoring_state(root: Path) -> dict[str, Any]:
    """Describe saved authoring work separately from the applied runtime tree."""
    with BLUEPRINT_MUTATION_LOCK:
        owner = draft_directory(root)
        draft = owner / "tree"
        install = read_install_metadata(root)
        changed: list[str] = []
        if draft.is_dir():
            baseline = _manifest(owner)["baseline"]
            changed = [
                name for name, checksum in _hashes(draft).items() if baseline.get(name) != checksum
            ]
        try:
            source = authoring_root(root)
        except ValueError:
            source = None
        return {
            "source": str(install.get("source") or root),
            "git_source": bool(install.get("commit") or install.get("source_kind") == "git"),
            "scope": str(install.get("scope") or "session"),
            "installed_revision": str(install.get("commit") or install.get("checksum") or ""),
            "has_draft": draft.is_dir(),
            "unpublished_files": changed,
            "checkout_required": source is None,
            "reload_required": _hashes(source) != _hashes(root) if source else False,
        }


def save_draft(
    root: Path,
    relative: str,
    content: str,
    *,
    expected_hash: str | None = None,
    runtime_tool_names: Collection[str] = (),
) -> dict[str, Any]:
    """Save and validate a draft without modifying the installed or author source.

    Invalid drafts can be saved for repair. The publish operation independently
    requires a valid complete tree. An expected hash protects concurrent editors.
    """
    with BLUEPRINT_MUTATION_LOCK:
        if Path(relative).name == ".clio-install.md":
            raise ValueError("Installation provenance is managed by CLIO, not the draft editor")
        owner = draft_directory(root)
        owner.mkdir(parents=True, exist_ok=True)
        draft = owner / "tree"
        if not draft.exists():
            with tempfile.TemporaryDirectory(dir=owner, prefix="prepare-") as temporary:
                staged = Path(temporary) / "tree"
                copy_blueprint_tree(root, staged)
                for directory, _, files in os.walk(win_extended_path(staged), followlinks=False):
                    os.chmod(directory, 0o700)
                    for name in files:
                        os.chmod(os.path.join(directory, name), 0o600)
                baseline = _hashes(staged)
                write_json_atomic(owner / "manifest.json", {"baseline": baseline, "published": []})
                rename_extended(staged, draft)
        _refresh_clean_files(root, draft)
        target = resolve_blueprint_file_path(draft, relative)
        if expected_hash is not None and _hash(target.read_bytes()) != expected_hash:
            raise ValueError("draft_conflict: another editor changed this file; reload and review")
        entry = write_blueprint_text_file(draft, relative, content)
        validation = validate_agent_blueprint_path(
            draft, scope="draft", runtime_tool_names=runtime_tool_names
        )
        return {
            "entry": entry,
            "validation": validation,
            "draft": True,
            "content_hash": _hash(content.encode("utf-8")),
        }


def authoring_root(root: Path, *, checkout: str = "") -> Path:
    """Resolve a folder source or an explicitly configured working checkout."""
    install = read_install_metadata(root)
    from clio_agent.gact.blueprint_source_configuration import configured_install

    install = configured_install(install)
    source = Path(
        checkout or install.get("working_checkout") or install.get("source") or str(root)
    ).expanduser()
    if not source.is_dir():
        raise ValueError("Configure a working checkout on this CLIO to publish a Git source")
    blueprint = parse_agent_blueprint_root(root, scope="install")
    candidates = _install_candidates(source, blueprint_id=blueprint.id)
    if len(candidates) != 1:
        raise ValueError("The source must contain exactly one matching blueprint")
    return candidates[0]


def publish_draft(
    root: Path,
    *,
    checkout: str = "",
    runtime_tool_names: Collection[str] = (),
    commit_message: str = "",
    push: bool = False,
) -> dict[str, Any]:
    """Publish validated file edits with optimistic upstream conflict detection.

    Each file is atomically replaced. The receipt reports per-file progress; no
    directory-wide atomicity is claimed. Installed runtime files are untouched.
    A retry accepts this operation's already-written bytes and resumes remaining
    files, so a partial disk failure cannot silently lose the draft.
    """
    with BLUEPRINT_MUTATION_LOCK:
        if push and not commit_message.strip():
            raise ValueError("Select Git publication and provide a commit message before pushing")
        owner = draft_directory(root)
        draft = owner / "tree"
        if not draft.is_dir():
            raise ValueError("Save a draft before publishing")
        _refresh_clean_files(root, draft)
        manifest = _manifest(owner)
        validation = validate_agent_blueprint_path(
            draft, scope="draft", runtime_tool_names=runtime_tool_names
        )
        if not validation["enabled"]:
            raise ValueError("Invalid draft: " + "; ".join(validation["validation_errors"]))
        original = parse_agent_blueprint_root(root, scope="install")
        if validation["agent_blueprint"]["id"] != original.id:
            raise ValueError("Renaming the blueprint requires registering a new blueprint")
        source = authoring_root(root, checkout=checkout)
        if source.resolve() == root.resolve():
            raise ValueError("Register this authoring folder as a marketplace before publishing")
        baseline: dict[str, str] = manifest["baseline"]
        current = _hashes(draft)
        changed = [name for name, checksum in current.items() if baseline.get(name) != checksum]
        if set(baseline) - set(current):
            raise ValueError("Draft file deletions require explicit source-file removal")
        # Check every file before writing any. External writers can still race;
        # each file is checked again immediately before its atomic replacement.
        for name in changed:
            target = resolve_blueprint_file_path(source, name)
            observed = _hash(target.read_bytes()) if target.is_file() else None
            accepted = {baseline.get(name)}
            if name in manifest.get("published", []):
                accepted.add(current[name])
            if observed not in accepted:
                raise ValueError(f"upstream_conflict: {name} changed since this draft was created")
        published = list(manifest.get("published", []))
        for name in changed:
            target = resolve_blueprint_file_path(source, name)
            observed = _hash(target.read_bytes()) if target.is_file() else None
            if observed == current[name] and name in published:
                continue
            if observed != baseline.get(name):
                raise ValueError(f"upstream_conflict: {name} changed while publishing")
            text = resolve_blueprint_file_path(draft, name).read_bytes().decode("utf-8")
            # Persist intent before writing so interrupted retries can recognize
            # their own exact bytes, while still refusing unrelated edits.
            if name not in published:
                published.append(name)
            write_json_atomic(owner / "manifest.json", {**manifest, "published": published})
            write_blueprint_text_file(source, name, text)
        last_published = changed or list(manifest.get("last_published", []))
        write_json_atomic(
            owner / "manifest.json",
            {"baseline": current, "published": [], "last_published": last_published},
        )
        result: dict[str, Any] = {
            "published": changed,
            "source": str(source),
            "reload_required": bool(changed),
        }
        if commit_message:
            from clio_agent.gact.blueprint_git_publish import publish_git_changes

            result["git"] = publish_git_changes(
                source, last_published, message=commit_message, push=push
            )
        return result
