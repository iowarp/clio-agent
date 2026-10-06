"""Deterministic, local document-to-PDF rendition pipeline."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.gact.artifacts.cas import ingest_identity, sha256_file
from clio_agent.gact.artifacts.minting import mint_artifact_outcome
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.runtime.document_stack.process import (
    DocumentError,
    find_native,
    office_convert,
    run,
    scratch_root,
)
from clio_agent.runtime.sandbox import SandboxCompositionError

if TYPE_CHECKING:
    from fastapi import FastAPI

_RENDER_WORKSPACE: ContextVar[Path | None] = ContextVar("document_render_workspace", default=None)


class RenditionError(RuntimeError):
    """A typed document rendition failure."""


class RenditionUnavailableError(RenditionError):
    """No supported local converter is installed."""


@dataclass(frozen=True)
class RenditionResult:
    """One derived PDF artifact."""

    record: ArtifactRecord
    version: ArtifactVersion
    converter: str
    workspace_path: Path | None = None


def _workspace_root(app: "FastAPI", workspace_id: str) -> Path:
    workspace = app.state.workspaces.get(workspace_id)
    root = str(getattr(workspace, "root_path", "") or "") if workspace else ""
    if not root:
        raise RenditionError(f"workspace root is unavailable: {workspace_id}")
    return Path(root).expanduser().resolve(strict=False)


def _source_path(
    workspace_root: Path,
    version: ArtifactVersion,
    temporary_root: Path,
    name: str,
) -> Path:
    if version.sha256:
        from clio_agent.gact.artifacts.cas import CASStore

        candidate = CASStore(workspace_root).blob_path(version.sha256)
        if candidate.is_file():
            if sha256_file(candidate) != version.sha256:
                raise RenditionError("artifact bytes failed their immutable hash check")
            target = temporary_root / Path(name).name
            shutil.copyfile(candidate, target)
            return target
    if version.path:
        candidate = Path(version.path)
        if candidate.is_file():
            target = temporary_root / Path(name).name
            shutil.copyfile(candidate, target)
            if version.sha256 and sha256_file(target) != version.sha256:
                raise RenditionError("artifact bytes failed their immutable hash check")
            return target
    raise RenditionError("artifact bytes are unavailable")


def _find_executable(*names: str) -> str | None:
    for name in names:
        found = find_native(name)
        if found:
            return found
    return None


def _run(command: list[str], *, cwd: Path, timeout_seconds: float = 120.0) -> None:
    try:
        root = _RENDER_WORKSPACE.get()
        env = None
        if root is not None:
            from clio_agent.runtime import sandbox

            confined = sandbox.wrap_confined(
                command[0],
                command[1:],
                write_roots=sandbox.effective_write_roots(
                    sandbox.PROFILE_SHELL, workspace_root=str(root)
                ),
                net_policy=sandbox.NET_DENY,
                profile=sandbox.PROFILE_SHELL,
                pdeathsig=False,
            )
            command = [confined.command, *confined.args]
            env = {**os.environ, **confined.env_overlay}
        run(command, cwd=cwd, timeout=timeout_seconds, env=env)
    except (DocumentError, SandboxCompositionError) as exc:
        raise RenditionError(str(exc)) from exc


def _convert_to_pdf(source: Path, output_dir: Path) -> tuple[Path, str]:
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        return source, "identity"
    if suffix == ".tex":
        tectonic = _find_executable("tectonic")
        if tectonic is not None:
            _run(
                [
                    tectonic,
                    "--only-cached",
                    "--keep-logs",
                    "--outdir",
                    str(output_dir),
                    str(source),
                ],
                cwd=source.parent,
            )
            output = output_dir / f"{source.stem}.pdf"
            if not output.is_file():
                raise RenditionError("tectonic completed without producing a PDF")
            return output, "tectonic"
    if suffix in {".md", ".markdown", ".html", ".htm", ".tex"}:
        pandoc = _find_executable("pandoc")
        if pandoc is not None:
            output = output_dir / f"{source.stem}.pdf"
            command = [pandoc, str(source), "--output", str(output)]
            typst = _find_executable("typst")
            if typst is not None:
                typst_font = os.environ.get("CLIO_DOCUMENT_TYPST_FONT", "").strip()
                if not typst_font:
                    typst_font = "Arial" if os.name == "nt" else "DejaVu Serif"
                command.extend(["--pdf-engine", typst, "--variable", f"mainfont={typst_font}"])
            _run(command, cwd=source.parent)
            if not output.is_file():
                raise RenditionError("pandoc completed without producing a PDF")
            return output, "pandoc+typst" if typst is not None else "pandoc"
    soffice = _find_executable("soffice", "libreoffice")
    if soffice is None:
        from clio_agent.runtime.document_runtime import prepare_office_runtime

        try:
            soffice = prepare_office_runtime()
        except DocumentError as exc:
            raise RenditionUnavailableError(str(exc)) from exc
    try:
        return office_convert(
            source,
            output_dir,
            "pdf",
            execute=lambda command, cwd: _run(command, cwd=cwd),
            executable_path=soffice,
            workspace=_RENDER_WORKSPACE.get(),
        ), "libreoffice"
    except DocumentError as exc:
        raise RenditionError(str(exc)) from exc


def find_pdf_rendition(
    app: "FastAPI", record: ArtifactRecord, version: ArtifactVersion
) -> RenditionResult | None:
    """Find a retained PDF bound to this exact source version and checksum."""
    registry = get_registry(app)
    candidate = registry.get(record.workspace_id, f"{record.name}.v{version.version}.pdf")
    if candidate is None:
        return None
    workspace_root = _workspace_root(app, record.workspace_id)
    from clio_agent.gact.artifacts.cas import CASStore

    for derived in reversed(candidate.versions):
        producer = derived.producer
        if (
            producer.get("designation") != "document-rendition"
            or producer.get("source_artifact_id") != version.artifact_id
            or producer.get("source_sha256") != version.sha256
        ):
            continue
        retained = CASStore(workspace_root).blob_path(derived.sha256) if derived.sha256 else None
        if retained is None or not retained.is_file():
            retained = Path(derived.path) if derived.path else None
        if (
            retained is not None
            and retained.is_file()
            and derived.sha256
            and sha256_file(retained) == derived.sha256
        ):
            return RenditionResult(candidate, derived, str(producer.get("converter", "")))
    return None


def render_pdf(
    app: "FastAPI",
    session_id: str,
    record: ArtifactRecord,
    version: ArtifactVersion,
) -> RenditionResult:
    """Render an immutable source version to a derived immutable PDF artifact."""
    try:
        return _render_pdf(app, session_id, record, version)
    except (OSError, DocumentError) as exc:
        raise RenditionError(str(exc)) from exc


def _render_pdf(
    app: "FastAPI", session_id: str, record: ArtifactRecord, version: ArtifactVersion
) -> RenditionResult:
    workspace_root = _workspace_root(app, record.workspace_id)
    rendition_root = (
        workspace_root / "artifacts" / "document-previews" / version.artifact_id
    ).resolve()
    if not rendition_root.is_relative_to(workspace_root):
        raise RenditionError("PDF preview output escapes the active workspace")
    rendition_root.mkdir(parents=True, exist_ok=True)
    target = rendition_root / f"{Path(record.name).stem}.pdf"
    if not target.resolve().is_relative_to(workspace_root):
        raise RenditionError("PDF preview output escapes the active workspace")
    existing = find_pdf_rendition(app, record, version)
    if existing is not None:
        retained_hash = existing.version.sha256 or ""
        if not retained_hash:
            raise RenditionError("Retained PDF preview has no immutable checksum")
        if not target.is_file() or sha256_file(target) != retained_hash:
            from clio_agent.gact.artifacts.cas import CASStore

            retained = CASStore(workspace_root).blob_path(retained_hash)
            if not retained.is_file():
                retained = Path(existing.version.path)
            _copy_preview(retained, target, retained_hash)
        return RenditionResult(
            existing.record, existing.version, existing.converter, workspace_path=target
        )
    # Each conversion owns a disposable run under workspace-local .tmp.
    with tempfile.TemporaryDirectory(prefix="render-", dir=scratch_root(workspace_root)) as raw_tmp:
        temporary_root = Path(raw_tmp)
        source = _source_path(workspace_root, version, temporary_root, record.name)
        token = _RENDER_WORKSPACE.set(workspace_root)
        try:
            try:
                rendered, converter = _convert_to_pdf(source, temporary_root)
            except (DocumentError, OSError) as exc:
                raise RenditionError(str(exc)) from exc
        finally:
            _RENDER_WORKSPACE.reset(token)
        if version.sha256 and sha256_file(source) != version.sha256:
            raise RenditionError("source changed during PDF conversion")
        _copy_preview(rendered, target)
    ingested = ingest_identity(target, workspace_root=workspace_root)
    output_name = f"{record.name}.v{version.version}.pdf"
    outcome = mint_artifact_outcome(
        app,
        session_id,
        name=output_name,
        workspace_id=record.workspace_id,
        evidence=ingested.evidence,
        kind=ArtifactKind.REPORT,
        mechanism=Mechanism.HARNESS,
        producer={
            "designation": "document-rendition",
            "source_artifact_id": version.artifact_id,
            "source_sha256": version.sha256,
            "converter": converter,
        },
        custody=ingested.custody,
        path=str(target),
        annotation=f"PDF rendition of {record.name} v{version.version}",
        turn_id=f"document-rendition:{version.artifact_id}",
        not_ingested_size=ingested.not_ingested_size,
    )
    if outcome is None:
        raise RenditionError("artifact mint returned no outcome")
    rendered_record = app.state.artifact_registry.get(record.workspace_id, output_name)
    if rendered_record is None:
        raise RenditionError("rendered artifact record was not indexed")
    return RenditionResult(
        record=rendered_record,
        version=outcome.version,
        converter=converter,
        workspace_path=target,
    )


def _copy_preview(source: Path, target: Path, expected_sha256: str = "") -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        if expected_sha256 and sha256_file(temporary) != expected_sha256:
            raise RenditionError("retained preview changed while restoring its workspace copy")
        os.replace(temporary, target)
    except OSError as exc:
        raise RenditionError(f"Could not save the workspace PDF preview: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "RenditionError",
    "RenditionResult",
    "RenditionUnavailableError",
    "render_pdf",
]
