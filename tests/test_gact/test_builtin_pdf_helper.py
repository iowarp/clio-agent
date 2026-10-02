"""The built-in ``work-with-pdfs`` skill's ``prepare_pdf.py`` helper does real work.

Moved from the marketplace's Factorio Flat pack, which now declares the built-in
instead of shipping a copy. The Docling converter and page renderer are injected
seams of the script itself; every manifest field asserted is what the script wrote.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

import clio_agent.gact as gact_pkg

_SKILL_DIR = Path(gact_pkg.__file__).resolve().parent / "builtin_skills" / "work-with-pdfs"


@pytest.fixture(scope="module")
def helper() -> ModuleType:
    """Import the shipped ``prepare_pdf.py`` script."""

    script = _SKILL_DIR / "scripts" / "prepare_pdf.py"
    spec = importlib.util.spec_from_file_location("builtin_prepare_pdf", script)
    assert spec is not None and spec.loader is not None, script
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pdf(root: Path, name: str) -> Path:
    """Write a minimal PDF-signed file."""

    source = root / name
    source.write_bytes(b"%PDF-1.7\n")
    return source


def _renderer(count: int) -> Callable[[Path, Path, int, int], list[Path]]:
    """A page renderer seam that writes ``count`` page images."""

    def render(source: Path, pages: Path, max_pages: int, dpi: int) -> list[Path]:
        pages.mkdir(parents=True)
        rendered = [pages / f"page-{index:04d}.png" for index in range(1, count + 1)]
        for path in rendered:
            path.write_bytes(b"png")
        return rendered

    return render


def test_skill_requires_pixels_for_engineering_drawing_geometry() -> None:
    """Detached text labels cannot prove drawing geometry or units."""

    body = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "always layout-dependent" in body
    assert "call `view_image` before answering" in body
    assert "Do not infer drawing units" in body


def test_helper_records_real_outputs_from_both_stages(helper: ModuleType, tmp_path: Path) -> None:
    """A successful preparation records concrete text and page artifacts."""

    seen: dict[str, object] = {}

    def converter(source: Path, markdown: Path, structured: Path, max_pages: int) -> None:
        seen["max_pages"] = max_pages
        markdown.write_text("# Converted\n", encoding="utf-8")
        structured.write_text("{}\n", encoding="utf-8")

    def renderer(source: Path, pages: Path, max_pages: int, dpi: int) -> list[Path]:
        seen["render"] = (max_pages, dpi)
        return _renderer(2)(source, pages, max_pages, dpi)

    result = helper.prepare_pdf(
        _pdf(tmp_path, "paper.pdf"),
        tmp_path / "prepared",
        max_pages=5,
        dpi=96,
        converter=converter,
        renderer=renderer,
    )

    assert seen == {"max_pages": 5, "render": (5, 96)}
    recorded = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert recorded["docling"]["status"] == "complete"
    assert recorded["pages"]["count"] == 2


def test_helper_preserves_a_docling_failure_when_pages_render(
    helper: ModuleType, tmp_path: Path
) -> None:
    """A fallback image set is useful without hiding the failed text conversion."""

    def converter(source: Path, markdown: Path, structured: Path, max_pages: int) -> None:
        raise RuntimeError("conversion unavailable")

    result = helper.prepare_pdf(
        _pdf(tmp_path, "scan.pdf"),
        tmp_path / "prepared",
        converter=converter,
        renderer=_renderer(1),
    )

    assert result["status"] == "partial"
    assert result["docling"]["status"] == "failed"
    assert result["pages"]["status"] == "complete"


def test_visual_only_renders_without_calling_docling(helper: ModuleType, tmp_path: Path) -> None:
    """Drawing questions can reach pixels without waiting for conversion."""

    def converter(source: Path, markdown: Path, structured: Path, max_pages: int) -> None:
        raise AssertionError("visual-only preparation must not call Docling")

    result = helper.prepare_pdf(
        _pdf(tmp_path, "drawing.pdf"),
        tmp_path / "prepared",
        visual_only=True,
        converter=converter,
        renderer=_renderer(1),
    )

    assert result["status"] == "complete"
    assert result["docling"] == {"status": "skipped", "reason": "visual_only"}
    assert result["pages"]["count"] == 1


def test_helper_rejects_non_pdf_inputs_before_running_stages(
    helper: ModuleType, tmp_path: Path
) -> None:
    """The script must not send an arbitrary workspace file into PDF tooling."""

    source = tmp_path / "notes.txt"
    source.write_text("not a pdf", encoding="utf-8")
    with pytest.raises(ValueError, match="existing .pdf"):
        helper.prepare_pdf(source, tmp_path / "prepared")
