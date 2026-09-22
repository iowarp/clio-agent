#!/usr/bin/env python3
"""Create bounded textual and page-image representations of one PDF."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_DPI = 144

Converter = Callable[[Path, Path, Path, int], None]
Renderer = Callable[[Path, Path, int, int], list[Path]]


def _atomic_text(path: Path, text: str) -> None:
    """Write UTF-8 ``text`` atomically to ``path``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def convert_with_docling(
    source: Path,
    markdown_path: Path,
    json_path: Path,
    max_pages: int,
) -> None:
    """Convert ``source`` with Docling and save Markdown plus structured JSON."""

    from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]

    result = DocumentConverter().convert(str(source), max_num_pages=max_pages)
    document = result.document
    _atomic_text(markdown_path, f"{document.export_to_markdown()}\n")
    _atomic_text(
        json_path,
        f"{json.dumps(document.export_to_dict(), indent=2, ensure_ascii=False)}\n",
    )


def render_pages(source: Path, pages_dir: Path, max_pages: int, dpi: int) -> list[Path]:
    """Render up to ``max_pages`` PDF pages as PNGs with PyMuPDF."""

    import pymupdf  # type: ignore[import-not-found]

    pages_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    with pymupdf.open(source) as document:
        if document.needs_pass:
            raise ValueError("encrypted PDF requires a password")
        if document.page_count > max_pages:
            raise ValueError(
                f"PDF has {document.page_count} pages, above the --max-pages limit {max_pages}"
            )
        scale = dpi / 72.0
        matrix = pymupdf.Matrix(scale, scale)
        for index, page in enumerate(document):
            output = pages_dir / f"page-{index + 1:04d}.png"
            page.get_pixmap(matrix=matrix, alpha=False).save(output)
            outputs.append(output)
    return outputs


def _failure(exc: Exception) -> dict[str, str]:
    """Return a bounded, typed failure payload for a preparation stage."""

    return {
        "status": "failed",
        "error_type": type(exc).__name__,
        "message": str(exc)[:2_000],
    }


def _write_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    """Persist the current preparation state and return its manifest path."""

    manifest_path = output_dir / "manifest.json"
    _atomic_text(
        manifest_path,
        f"{json.dumps(manifest, indent=2, ensure_ascii=False)}\n",
    )
    return manifest_path


def prepare_pdf(
    source: Path,
    output_dir: Path,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    dpi: int = DEFAULT_DPI,
    visual_only: bool = False,
    converter: Converter = convert_with_docling,
    renderer: Renderer = render_pages,
) -> dict[str, Any]:
    """Prepare bounded Docling and page-image derivatives and return a manifest."""

    source = source.expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("source must be an existing .pdf file")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if not 72 <= dpi <= 300:
        raise ValueError("dpi must be between 72 and 300")
    size = source.stat().st_size
    if size > max_bytes:
        raise ValueError(f"PDF size {size} exceeds the --max-bytes limit {max_bytes}")

    output_dir = output_dir.expanduser().resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / f"{source.stem}.md"
    json_path = output_dir / f"{source.stem}.docling.json"
    pages_dir = output_dir / "pages"

    manifest: dict[str, Any] = {
        "source": str(source),
        "source_bytes": size,
        "limits": {"max_pages": max_pages, "max_bytes": max_bytes, "dpi": dpi},
        "docling": {"status": "skipped", "reason": "visual_only"}
        if visual_only
        else {"status": "pending"},
        "pages": {"status": "pending"},
        "status": "processing",
    }
    try:
        page_paths = renderer(source, pages_dir, max_pages, dpi)
        manifest["pages"] = {
            "status": "complete",
            "count": len(page_paths),
            "files": [str(path) for path in page_paths],
        }
    except Exception as exc:  # noqa: BLE001 - stage failure is preserved in the manifest
        manifest["pages"] = _failure(exc)

    # Pixels are the time-sensitive fallback for scans and layout-dependent
    # questions. Persist them before the potentially slower Docling stage so a
    # bounded caller still receives honest visual evidence if conversion times out.
    manifest_path = _write_manifest(output_dir, manifest)

    if not visual_only:
        try:
            converter(source, markdown_path, json_path, max_pages)
            manifest["docling"] = {
                "status": "complete",
                "markdown": str(markdown_path),
                "structured_json": str(json_path),
            }
        except Exception as exc:  # noqa: BLE001 - stage failure is preserved in manifest
            manifest["docling"] = _failure(exc)

    if visual_only:
        manifest["status"] = "complete" if manifest["pages"]["status"] == "complete" else "failed"
    else:
        manifest["status"] = (
            "complete"
            if manifest["docling"]["status"] == manifest["pages"]["status"] == "complete"
            else "partial"
            if "complete" in {manifest["docling"]["status"], manifest["pages"]["status"]}
            else "failed"
        )
    _write_manifest(output_dir, manifest)
    manifest["manifest"] = str(manifest_path)
    if manifest["status"] == "failed":
        raise RuntimeError(f"both PDF preparation stages failed; inspect {manifest_path}")
    return manifest


def main() -> int:
    """Run the PDF preparation command."""

    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--visual-only", action="store_true")
    args = parser.parse_args()
    result = prepare_pdf(
        args.source,
        args.output_dir,
        max_pages=args.max_pages,
        max_bytes=args.max_bytes,
        dpi=args.dpi,
        visual_only=args.visual_only,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
