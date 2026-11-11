"""The paper: the scientific document a LaTeX project builds (the paper artifact rows).

A build that makes a PDF mints a version of the project's **paper** record, the artifact the
person sees and comments on:

- its bytes are the built PDF; the record is named after the project (``paper-bb``), so a new
  title does not start a new chain;
- its producer (``designation: scientific-document``) carries the title from ``\\title``, the
  document type and venue of the template the paper's class matches, the entry, the build's
  report fields, and its **members**: every source file the build read, as the exact artifact
  version that was read;
- the build is a transform that used each member version and generated the paper, so the
  lineage reads sources, build, paper.

The entry ``.tex`` stays the artifact the build and the resolvers read; a paper resolves back
to it through :func:`paper_entry`.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
    from clio_agent.gact.artifacts.registry import MintOutcome
    from clio_agent.gact.documents.profiles import DocumentFormat

__all__ = [
    "DESIGNATION",
    "document_labels",
    "is_paper",
    "mint_paper",
    "paper_entry",
    "paper_format",
    "paper_name",
    "paper_title",
]

#: The producer designation of a paper version.
DESIGNATION = "scientific-document"
#: The document type a paper has when no template names one.
DEFAULT_DOCUMENT_TYPE = "Paper"

_TITLE = re.compile(r"\\title\s*(?:\[[^\]]*\])?\s*\{")
_DOCUMENTCLASS = re.compile(r"\\documentclass\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}")
_DROPPED = re.compile(r"\\(?:thanks|footnote|IEEEauthorrefmark)\s*\{")


def paper_format() -> DocumentFormat:
    """How a paper reads as a document: a LaTeX paper's profile and anchors, served as its PDF.

    A paper has no file extension (``paper-bb``); its marks are the LaTeX ones (text on either
    pane, PDF quads and regions, resolved through the source map), and its bytes are the PDF.
    """
    from dataclasses import replace  # noqa: PLC0415

    from clio_agent.gact.documents.profiles import document_format  # noqa: PLC0415

    return replace(
        document_format("paper.tex"),
        mime_type="application/pdf",
        native_open=False,
        rendition_formats=(),
    )


def is_paper(version: ArtifactVersion | None) -> bool:
    """Whether ``version`` is a paper version."""
    return version is not None and version.producer.get("designation") == DESIGNATION


def paper_name(workspace_root: Path, entry: Path) -> str:
    """The paper record's name: its project folder, or the entry's stem beside it.

    ``paper-bb/main.tex`` is ``paper-bb``; ``paper-bb/supplement.tex`` is
    ``paper-bb/supplement``; a ``main.tex`` at the workspace root is ``main``.
    """
    try:
        relative = PurePosixPath(entry.resolve().relative_to(workspace_root.resolve()).as_posix())
    except ValueError:
        relative = PurePosixPath(entry.name)
    folder = relative.parent.as_posix()
    if folder in {"", "."}:
        return relative.stem
    return folder if relative.stem == "main" else f"{folder}/{relative.stem}"


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in text.splitlines())


def _braced_end(text: str, start: int) -> int:
    """The index just past the group opening at ``text[start] == "{"``."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def paper_title(entry_text: str) -> str:
    """The paper's title as it reads, from ``\\title{...}``, or ``""`` when it has none."""
    code = _strip_comments(entry_text)
    found = _TITLE.search(code)
    if not found:
        return ""
    end = _braced_end(code, found.end() - 1)
    body = code[found.end() : end - 1]
    while dropped := _DROPPED.search(body):  # a footnote on the title is not the title
        body = body[: dropped.start()] + body[_braced_end(body, dropped.end() - 1) :]
    body = re.sub(r"\\\\(?:\[[^\]]*\])?", " ", body)
    body = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", body)
    flat = body.replace("{", " ").replace("}", " ").replace("~", " ")
    return " ".join(flat.split())[:300]


def document_labels(entry_text: str, roots: list[Path]) -> dict[str, str]:
    """The document type and venue of the template the entry's ``\\documentclass`` matches.

    A template matches when its ``class`` is the paper's and all its ``options`` are among
    the paper's; the one matching most options wins. Without a match the paper is a
    ``Paper`` with no venue.
    """
    import yaml  # noqa: PLC0415 - only when a paper is built

    found = _DOCUMENTCLASS.search(_strip_comments(entry_text))
    if not found:
        return {"document_type": DEFAULT_DOCUMENT_TYPE, "venue": "", "template": ""}
    options = {item.strip() for item in (found.group(1) or "").split(",") if item.strip()}
    cls = found.group(2).strip()
    best: tuple[int, str, dict[str, Any]] | None = None
    for root in roots:
        for manifest in sorted(root.glob("*/template.yaml")):
            try:
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(data, dict) or str(data.get("class") or "") != cls:
                continue
            wanted = {str(item) for item in data.get("options") or []}
            if not wanted <= options:
                continue
            if best is None or len(wanted) > best[0]:
                best = (len(wanted), manifest.parent.name, data)
    if best is None:
        return {"document_type": DEFAULT_DOCUMENT_TYPE, "venue": "", "template": ""}
    _score, name, data = best
    return {
        "document_type": str(data.get("document_type") or DEFAULT_DOCUMENT_TYPE),
        "venue": str(data.get("venue") or data.get("title") or ""),
        "template": name,
    }


def _members(
    app: FastAPI,
    session_id: str,
    workspace_id: str,
    workspace_root: Path,
    project: Path,
    source_files: dict[str, str],
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Each file the build read as the artifact version it read, and the used edge to it.

    A file nothing tracked yet (a figure a script wrote, a file the person added) is
    registered here, as a comment target is; a tracked file whose bytes changed gets its
    next version first. A file whose bytes moved on during the build has no version with
    the read hash, and its edge says so (``external`` with the hash read).
    """
    from clio_agent.gact.artifacts.transform_types import (  # noqa: PLC0415
        EdgeEvidence,
        EdgeRole,
        ProvEdge,
    )
    from clio_agent.gact.documents.comment_targets import (  # noqa: PLC0415
        CommentTargetError,
        register_target,
    )

    members: list[dict[str, Any]] = []
    edges: list[ProvEdge] = []
    for relative, digest in sorted(source_files.items()):
        path = (project / relative).resolve()
        try:
            workspace_path = path.relative_to(workspace_root.resolve()).as_posix()
        except ValueError:
            continue  # a bundle file outside the workspace is the TeX distribution's
        try:
            record, version = register_target(
                app, session_id, workspace_id, workspace_path, designation="paper-member"
            )
            registered = version.sha256 == digest
        except CommentTargetError:
            registered = False  # not a file the workspace serves: named by its path only
        if registered:
            members.append(
                {
                    "path": relative,
                    "artifact_id": version.artifact_id,
                    "name": record.name,
                    "version": version.version,
                    "sha256": digest,
                }
            )
            edges.append(
                ProvEdge(
                    role=EdgeRole.USED,
                    evidence=EdgeEvidence.HASH_PAIR,
                    artifact_id=version.artifact_id,
                    sha256=digest,
                    name=record.name,
                    version=version.version,
                    path=str(path),
                )
            )
        else:
            members.append({"path": relative, "artifact_id": "", "sha256": digest})
            edges.append(
                ProvEdge(
                    role=EdgeRole.USED,
                    evidence=EdgeEvidence.SCHEMA_ARG,
                    external_ref=f"external:{path}",
                    sha256=digest,
                    name=workspace_path,
                    path=str(path),
                    note="changed_after_build_read",
                )
            )
    return members, edges


def mint_paper(
    app: FastAPI,
    session_id: str,
    *,
    entry_record: ArtifactRecord,
    entry_version: ArtifactVersion,
    workspace_root: Path,
    target: Path,
    converter: str,
    build: dict[str, Any],
) -> tuple[ArtifactRecord, MintOutcome]:
    """Mint the paper version for a build of ``entry_version`` whose PDF is ``target``.

    ``build`` is the build's producer fields (``source_files``, ``diagnostics``,
    ``page_count``, ``synctex`` ...). The paper's type and venue come from the session's
    templates; a build outside a session keeps the ones the paper had.

    Raises:
        RuntimeError: the mint or its index returned nothing.
    """
    from clio_agent.gact.artifacts.cas import ingest_identity  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import mint_artifact_outcome  # noqa: PLC0415
    from clio_agent.gact.artifacts.records import ArtifactKind, Mechanism  # noqa: PLC0415
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415
    from clio_agent.gact.artifacts.transforms import record_transform  # noqa: PLC0415
    from clio_agent.gact.documents.latex.venue import session_template_roots  # noqa: PLC0415

    entry = Path(entry_version.path)
    project = entry.parent
    name = paper_name(workspace_root, entry)
    registry = get_registry(app)
    previous = registry.get(entry_record.workspace_id, name)
    before = previous.head.producer if previous is not None and previous.head else {}
    text = entry.read_text(encoding="utf-8", errors="replace")
    roots = session_template_roots(app, session_id)
    labels = (
        document_labels(text, roots)
        if roots
        else {key: str(before.get(key) or "") for key in ("document_type", "venue", "template")}
    )
    labels["document_type"] = labels.get("document_type") or DEFAULT_DOCUMENT_TYPE
    members, edges = _members(
        app,
        session_id,
        entry_record.workspace_id,
        workspace_root,
        project,
        dict(build.get("source_files") or {}),
    )
    title = paper_title(text)
    try:
        entry_path = entry.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        entry_path = entry.name
    ingested = ingest_identity(target, workspace_root=workspace_root)
    call_id = f"{DESIGNATION}:{entry_version.artifact_id}:{ingested.evidence.sha256}"
    outcome = mint_artifact_outcome(
        app,
        session_id,
        name=name,
        workspace_id=entry_record.workspace_id,
        evidence=ingested.evidence,
        kind=ArtifactKind.REPORT,
        mechanism=Mechanism.HARNESS,
        producer={
            **build,  # the build's report fields; the paper's own fields below win
            "designation": DESIGNATION,
            "format": "latex",
            "title": title,
            **labels,
            "entry": entry_path,
            "call_id": call_id,
            "source_artifact_id": entry_version.artifact_id,
            "source_sha256": entry_version.sha256,
            "converter": converter,
            "members": members,
        },
        custody=ingested.custody,
        path=str(target),
        annotation=f"{labels['document_type']} built from {entry_path}",
        turn_id=f"{DESIGNATION}:{entry_version.artifact_id}",
        not_ingested_size=ingested.not_ingested_size,
    )
    if outcome is None:
        raise RuntimeError("the paper mint returned no outcome")
    record = registry.get(entry_record.workspace_id, name)
    if record is None:
        raise RuntimeError("the paper record was not indexed")
    record_transform(
        app,
        session_id,
        tool_name="Build paper",
        args={"entry": entry_path, "converter": converter},
        call_id=call_id,
        ok=True,
        result=None,
        minted=[outcome.version],
        workspace_id=entry_record.workspace_id,
        turn_id=f"{DESIGNATION}:{entry_version.artifact_id}",
        used_edges=edges,
    )
    return record, outcome


def paper_entry(
    app: FastAPI, session_id: str, version: ArtifactVersion
) -> tuple[ArtifactRecord, ArtifactVersion] | None:
    """The entry ``.tex`` a paper version builds from, at its current version.

    ``None`` when ``version`` is not a paper or its entry is no longer in the workspace.
    """
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415
    from clio_agent.gact.documents.comment_targets import (  # noqa: PLC0415
        CommentTargetError,
        register_target,
    )

    if not is_paper(version):
        return None
    found = get_registry(app).get_by_artifact_id(str(version.producer.get("source_artifact_id")))
    workspace_id = found[0].workspace_id if found is not None else ""
    entry = str(version.producer.get("entry") or "")
    if not workspace_id or not entry:
        return found
    try:
        return register_target(app, session_id, workspace_id, entry, designation="latex-source")
    except CommentTargetError:
        return found
