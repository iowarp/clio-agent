"""Deterministic, local document-to-PDF rendition pipeline."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent import paths
from clio_agent.gact.artifacts.cas import ingest_identity, sha256_file
from clio_agent.gact.artifacts.minting import mint_artifact_outcome
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Mechanism,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


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
    if version.path:
        candidate = Path(version.path)
        if candidate.is_file():
            if version.sha256 and sha256_file(candidate) != version.sha256:
                raise RenditionError("artifact bytes failed their immutable hash check")
            return candidate
    if version.sha256:
        from clio_agent.gact.artifacts.cas import CASStore

        candidate = CASStore(workspace_root).blob_path(version.sha256)
        if candidate.is_file():
            target = temporary_root / Path(name).name
            shutil.copyfile(candidate, target)
            return target
    raise RenditionError("artifact bytes are unavailable")


def _find_executable(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        program_files = [
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
        ]
        candidates = {
            "soffice": [
                Path(root) / "LibreOffice" / "program" / "soffice.exe" for root in program_files
            ],
            "libreoffice": [
                Path(root) / "LibreOffice" / "program" / "soffice.exe" for root in program_files
            ],
        }
        for name in names:
            for candidate in candidates.get(name, []):
                if candidate.is_file():
                    return str(candidate)
    return None


def _run(command: list[str], *, cwd: Path, timeout_seconds: float = 120.0) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "converter failed").strip()
        raise RenditionError(message[-4_096:])


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
        raise RenditionUnavailableError(
            "PDF rendition requires LibreOffice, Tectonic, or Pandoc for this format"
        )
    user_profile = output_dir / "libreoffice-profile"
    user_profile.mkdir()
    _run(
        [
            soffice,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation={user_profile.as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(source),
        ],
        cwd=source.parent,
    )
    output = output_dir / f"{source.stem}.pdf"
    if not output.is_file():
        raise RenditionError("LibreOffice completed without producing a PDF")
    return output, "libreoffice"


def render_pdf(
    app: "FastAPI",
    session_id: str,
    record: ArtifactRecord,
    version: ArtifactVersion,
) -> RenditionResult:
    """Render an immutable source version to a derived immutable PDF artifact."""

    workspace_root = _workspace_root(app, record.workspace_id)
    rendition_root = (
        paths.workspace_agent_dir(workspace_root) / "documents" / "renditions" / version.artifact_id
    )
    rendition_root.mkdir(parents=True, exist_ok=True)
    target = rendition_root / f"{Path(record.name).stem}.pdf"
    with tempfile.TemporaryDirectory(prefix="clio-document-rendition-") as raw_tmp:
        temporary_root = Path(raw_tmp)
        source = _source_path(workspace_root, version, temporary_root, record.name)
        rendered, converter = _convert_to_pdf(source, temporary_root)
        temporary_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(rendered, temporary_target)
        os.replace(temporary_target, target)
    ingested = ingest_identity(target, workspace_root=workspace_root)
    output_name = f"{record.name}.pdf"
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
    )


__all__ = [
    "RenditionError",
    "RenditionResult",
    "RenditionUnavailableError",
    "render_pdf",
]
