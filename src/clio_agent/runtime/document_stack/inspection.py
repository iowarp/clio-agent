"""Bounded format-native document inspection, including formula/cache evidence."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from process import DocumentError

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_CELLS = 10_000


def validate_source(source: Path) -> Path:
    """Reject oversized inputs and Office packages before loading format libraries."""
    source = source.expanduser().resolve(strict=True)
    if not source.is_file() or source.stat().st_size > MAX_SOURCE_BYTES:
        raise DocumentError("Source must be a file no larger than 256 MiB")
    if source.suffix.lower() in {".docx", ".pptx", ".xlsx", ".xlsm", ".odt", ".ods", ".odp"}:
        with ZipFile(source) as archive:
            entries = archive.infolist()
            if (
                len(entries) > 10_000
                or sum(entry.file_size for entry in entries) > MAX_PACKAGE_BYTES
            ):
                raise DocumentError("Expanded document package exceeds the inspection limit")
    return source


def select_pages(pages: str, total: int, *, limit: int = 40) -> list[int]:
    """Resolve a strict 1-based range; refuse unbounded long-document requests."""
    if total < 1:
        raise DocumentError("Document has no pages to inspect")
    selected: set[int] = set()
    if not pages:
        if total > limit:
            raise DocumentError(
                f"Document has {total} pages; select a range of at most {limit} pages"
            )
        return list(range(1, total + 1))
    try:
        for part in pages.split(","):
            bounds = [int(value.strip()) for value in part.split("-")]
            if len(bounds) not in {1, 2}:
                raise ValueError
            start, end = bounds[0], bounds[-1]
            if start < 1 or end < start or end > total or end - start + 1 > limit:
                raise ValueError
            selected.update(range(start, end + 1))
            if len(selected) > limit:
                raise ValueError
    except ValueError as exc:
        raise DocumentError(
            f"Invalid page range {pages!r}; choose 1-{total}, at most {limit} pages"
        ) from exc
    return sorted(selected)


def _pdf(source: Path, pages: str) -> dict[str, Any]:
    import pymupdf

    with pymupdf.open(source) as document:
        if document.needs_pass:
            raise DocumentError("Encrypted PDF requires a password")
        selected = select_pages(pages, document.page_count)
        rows = [
            {
                "page": number,
                "text": document[number - 1].get_text(),
                "image_count": len(document[number - 1].get_images()),
            }
            for number in selected
        ]
        return {
            "page_count": document.page_count,
            "selected_pages": selected,
            "pages": rows,
            "text_evidence": "embedded text; image-only pages require visual inspection or OCR",
        }


def _word(source: Path) -> dict[str, Any]:
    from docx import Document
    from docx.table import Table

    document = Document(str(source))
    blocks: list[dict[str, Any]] = []
    for index, block in enumerate(document.iter_inner_content()):
        if index >= 10_000:
            raise DocumentError("Word document exceeds 10,000 body blocks")
        if isinstance(block, Table):
            cells = sum(len(row.cells) for row in block.rows)
            if cells > MAX_CELLS:
                raise DocumentError("Word table exceeds the cell inspection limit")
            blocks.append(
                {"type": "table", "rows": [[cell.text for cell in row.cells] for row in block.rows]}
            )
        else:
            blocks.append(
                {
                    "type": "paragraph",
                    "style": block.style.name if block.style else "",
                    "text": block.text,
                }
            )
    return {
        "blocks": blocks,
        "inline_images": len(document.inline_shapes),
        "scope": "body paragraphs and tables; render for pagination, floating objects, headers, footers, comments and revisions",
    }


def _slides(source: Path, pages: str) -> dict[str, Any]:
    from pptx import Presentation

    document = Presentation(str(source))
    selected = select_pages(pages, len(document.slides))

    def shapes(items: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for shape in items:
            row: dict[str, Any] = {
                "id": shape.shape_id,
                "name": shape.name,
                "x": shape.left,
                "y": shape.top,
                "width": shape.width,
                "height": shape.height,
            }
            if shape.has_text_frame:
                row["text"] = shape.text
            if shape.has_table:
                row["table"] = [[cell.text for cell in line.cells] for line in shape.table.rows]
            if hasattr(shape, "shapes"):
                row["children"] = shapes(shape.shapes)
            rows.append(row)
        return rows

    rows = []
    for number in selected:
        slide = document.slides[number - 1]
        notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ""
        rows.append({"slide": number, "shapes": shapes(slide.shapes), "notes": notes})
    return {
        "slide_count": len(document.slides),
        "selected_slides": selected,
        "slides": rows,
        "slide_width": document.slide_width,
        "slide_height": document.slide_height,
    }


def inspect_workbook(source: Path, *, sheet: str = "", cell_range: str = "") -> dict[str, Any]:
    """Read bounded cells twice so formulas are distinct from cached values."""
    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries

    formulas = load_workbook(source, read_only=True, data_only=False, keep_links=True)
    values = load_workbook(source, read_only=True, data_only=True, keep_links=True)
    try:
        if sheet and sheet not in formulas.sheetnames:
            raise DocumentError(f"Unknown sheet {sheet!r}; available: {formulas.sheetnames}")
        selected = [sheet] if sheet else formulas.sheetnames
        results: list[dict[str, Any]] = []
        remaining = MAX_CELLS
        for name in selected:
            tab = formulas[name]
            if cell_range:
                first_col, first_row, last_col, last_row = range_boundaries(cell_range)
                if any(value is None for value in (first_col, first_row, last_col, last_row)):
                    raise DocumentError("Use a bounded rectangular range such as A1:D20")
            else:
                first_col, first_row, last_col, last_row = (
                    1,
                    1,
                    tab.max_column or 1,
                    tab.max_row or 1,
                )
            if not all(
                isinstance(value, int) and value > 0
                for value in (first_col, first_row, last_col, last_row)
            ):
                raise DocumentError("Cell range coordinates must be positive integers")
            size = (last_col - first_col + 1) * (last_row - first_row + 1)
            if size < 1 or size > remaining:
                raise DocumentError(
                    f"Workbook inspection exceeds {MAX_CELLS} cells; select a sheet and cell range"
                )
            remaining -= size
            cells: list[dict[str, Any]] = []
            formula_rows = tab.iter_rows(
                min_row=first_row, max_row=last_row, min_col=first_col, max_col=last_col
            )
            value_rows = values[name].iter_rows(
                min_row=first_row, max_row=last_row, min_col=first_col, max_col=last_col
            )
            for line, cached_line in zip(formula_rows, value_rows, strict=True):
                for cell, cached in zip(line, cached_line, strict=True):
                    if cell.value is not None:
                        cells.append(
                            {
                                "cell": cell.coordinate,
                                "formula": cell.value if cell.data_type == "f" else None,
                                "value": cached.value,
                                "error": cached.data_type == "e",
                                "number_format": cell.number_format,
                            }
                        )
            results.append(
                {"sheet": name, "rows": tab.max_row, "columns": tab.max_column, "cells": cells}
            )
        with ZipFile(source) as archive:
            external_links = any(
                name.startswith("xl/externalLinks/") for name in archive.namelist()
            )
        return {
            "sheet_names": formulas.sheetnames,
            "sheets": results,
            "external_links": external_links,
            "scope": "selected cell formulas and cached values; cached values may be stale and are not recalculation evidence",
        }
    finally:
        formulas.close()
        values.close()


def workbook_calculation_evidence(source: Path) -> dict[str, Any]:
    """Inspect formula caches directly, including cached empty-string results."""
    from defusedxml.ElementTree import fromstring

    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    total = 0
    issues: list[dict[str, str]] = []
    with ZipFile(source) as archive:
        for name in archive.namelist():
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            for cell in fromstring(archive.read(name)).findall(".//s:c", namespace):
                formula = cell.find("s:f", namespace)
                cached = cell.find("s:v", namespace)
                if formula is not None:
                    total += 1
                    if cached is None or (cached.text is None and cell.get("t") != "str"):
                        issues.append(
                            {
                                "part": name,
                                "cell": cell.get("r", ""),
                                "error": "missing_formula_cache",
                            }
                        )
                if cell.get("t") == "e":
                    issues.append(
                        {
                            "part": name,
                            "cell": cell.get("r", ""),
                            "error": cached.text
                            if cached is not None and cached.text
                            else "cell_error",
                        }
                    )
    return {
        "formula_count": total,
        "issue_count": len(issues),
        "issues": issues[:100],
        "issues_truncated": len(issues) > 100,
        "status": "failed" if issues else "complete",
    }


def inspect_document(
    source: Path, *, pages: str = "", sheet: str = "", cell_range: str = ""
) -> dict[str, Any]:
    """Extract format-native content; refuse unsupported formats explicitly."""
    source = validate_source(source)
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        return _pdf(source, pages)
    if suffix == ".docx":
        return _word(source)
    if suffix == ".pptx":
        return _slides(source, pages)
    if suffix in {".xlsx", ".xlsm"}:
        return inspect_workbook(source, sheet=sheet, cell_range=cell_range)
    if suffix in {".csv", ".tsv"}:
        with source.open(encoding="utf-8-sig", newline="") as stream:
            rows: list[list[str]] = []
            count = 0
            for row in csv.reader(stream, delimiter="\t" if suffix == ".tsv" else ","):
                count += len(row)
                if count > MAX_CELLS:
                    raise DocumentError("Delimited file exceeds the cell inspection limit")
                rows.append(row)
        return {"rows": rows}
    if suffix in {".txt", ".md", ".markdown", ".html", ".htm", ".rtf"}:
        if source.stat().st_size > MAX_TEXT_CHARS:
            raise DocumentError("Text source exceeds 1 MiB; use bounded file reads")
        return {
            "text": source.read_text(encoding="utf-8"),
            "scope": "source text, not rendered layout",
        }
    raise DocumentError(
        f"No native extraction for {suffix}; convert legacy or OpenDocument files to DOCX/PPTX/XLSX with LibreOffice, preserving the original"
    )
