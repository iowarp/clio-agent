"""Portable document extraction, rendering and recalculation with durable evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from contact_sheets import make_contact_sheets
from inspection import (
    MAX_TEXT_CHARS,
    inspect_document,
    select_pages,
    validate_source,
    workbook_calculation_evidence,
)
from process import DocumentError, office_convert


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def render(source: Path, output: Path, *, pages: str = "", dpi: int = 120) -> dict[str, Any]:
    """Render selected PDF pages, converting Office documents through LibreOffice."""
    import pymupdf

    if not 72 <= dpi <= 200:
        raise DocumentError("dpi must be between 72 and 200")
    if source.suffix.lower() == ".pdf":
        pdf = source
    elif source.suffix.lower() in {
        ".docx",
        ".pptx",
        ".xlsx",
        ".odt",
        ".ods",
        ".odp",
        ".doc",
        ".ppt",
        ".xls",
        ".rtf",
    }:
        pdf = office_convert(source, output, "pdf")
    else:
        raise DocumentError(f"Rendering is unsupported for {source.suffix}")
    images: list[dict[str, Any]] = []
    with pymupdf.open(pdf) as document:
        if document.needs_pass:
            raise DocumentError("Encrypted PDF requires a password")
        selected = select_pages(pages, document.page_count)
        # Preflight the WHOLE requested render before allocating a pixel buffer.
        pixels = [
            math.ceil(document[number - 1].rect.width * dpi / 72)
            * math.ceil(document[number - 1].rect.height * dpi / 72)
            for number in selected
        ]
        if any(value > 20_000_000 for value in pixels) or sum(pixels) > 80_000_000:
            raise DocumentError("Render exceeds pixel budget; select fewer pages or lower dpi")
        written = 0
        for number in selected:
            page = document[number - 1]
            path = output / f"page-{number:04d}.png"
            page.get_pixmap(dpi=dpi, alpha=False).save(path)
            written += path.stat().st_size
            if written > 128 * 1024 * 1024:
                raise DocumentError("Rendered image output exceeds 128 MiB")
            images.append({"page": number, "path": str(path), "sha256": _hash(path)})
        return {
            "pdf": str(pdf),
            "pdf_sha256": _hash(pdf),
            "page_count": document.page_count,
            "selected_pages": selected,
            "images": images,
            "contact_sheets": make_contact_sheets(images, output),
            "dpi": dpi,
            "visual_review": (
                "pending: inspect contact sheets, then choose individual pages for closer review "
                "where the overview leaves text or layout unclear; revise and render again after fixes"
            ),
        }


def recalculate(source: Path, output: Path) -> dict[str, Any]:
    """Recalculate an XLSX copy, then check every formula cache and cell error."""
    if source.suffix.lower() != ".xlsx":
        raise DocumentError(
            "Recalculation accepts XLSX only; macro and legacy files require a preservation-aware workflow"
        )
    with ZipFile(source) as archive:
        if any(name.startswith("xl/externalLinks/") for name in archive.namelist()):
            raise DocumentError(
                "Workbook has external links; automatic recalculation could destroy cached values or links"
            )
    result = office_convert(source, output, "xlsx")
    evidence = workbook_calculation_evidence(result)
    evidence.update(
        {
            "path": str(result),
            "sha256": _hash(result),
            "semantic_review": "pending: verify formulas and expected results against the task",
        }
    )
    return evidence


def prepare(
    source: Path,
    output_dir: Path,
    *,
    action: str = "inspect",
    pages: str = "",
    sheet: str = "",
    cell_range: str = "",
    dpi: int = 120,
) -> dict[str, Any]:
    """Write a unique run manifest, recording failures and immutable source identity."""
    source = validate_source(source)
    if action not in {"inspect", "render", "recalculate"}:
        raise DocumentError(f"Unknown document action: {action}")
    output_dir = output_dir.expanduser().resolve()
    if output_dir == source or output_dir.is_relative_to(source):
        raise DocumentError("Output directory cannot be the source file")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="document-", dir=output_dir)).resolve()
    manifest: dict[str, Any] = {
        "schema": "clio.document-preparation.v1",
        "action": action,
        "source": str(source),
        "source_sha256": _hash(source),
        "source_bytes": source.stat().st_size,
        "status": "processing",
        "run_directory": str(run_dir),
        "manifest": str(run_dir / "manifest.json"),
    }
    try:
        if action == "inspect":
            result = inspect_document(source, pages=pages, sheet=sheet, cell_range=cell_range)
            content = json.dumps(result, indent=2, ensure_ascii=False, default=str)
            if len(content) > MAX_TEXT_CHARS:
                raise DocumentError(
                    "Extracted content exceeds 1 million characters; narrow the selection"
                )
            derivative = run_dir / "content.json"
            derivative.write_text(content + "\n", encoding="utf-8")
            manifest.update(
                {
                    "content": str(derivative),
                    "content_sha256": _hash(derivative),
                    "status": "complete",
                    "visual_review": "not performed",
                }
            )
            for name in (
                "page_count",
                "selected_pages",
                "slide_count",
                "selected_slides",
                "sheet_names",
            ):
                if name in result:
                    manifest[name] = result[name]
        elif action == "render":
            manifest.update(render(source, run_dir, pages=pages, dpi=dpi))
            manifest["status"] = "complete"
        else:
            manifest.update(recalculate(source, run_dir))
        if _hash(source) != manifest["source_sha256"]:
            raise DocumentError(
                "Source changed during preparation; derivatives are not verified against its current revision"
            )
    except (
        OSError,
        ValueError,
        RuntimeError,
        LookupError,
        TypeError,
        SyntaxError,
        BadZipFile,
    ) as exc:
        manifest.update(
            {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:4000]}
        )
    target = Path(manifest["manifest"])
    temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return manifest


def main() -> int:
    """Run a preparation stage; failure is reflected in JSON and exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "render", "recalculate"))
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--pages", default="")
    parser.add_argument("--sheet", default="")
    parser.add_argument("--cell-range", default="")
    parser.add_argument("--dpi", type=int, default=120)
    args = parser.parse_args()
    try:
        result = prepare(
            args.source,
            args.output_dir,
            action=args.action,
            pages=args.pages,
            sheet=args.sheet,
            cell_range=args.cell_range,
            dpi=args.dpi,
        )
    except (DocumentError, OSError, ValueError, BadZipFile) as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
