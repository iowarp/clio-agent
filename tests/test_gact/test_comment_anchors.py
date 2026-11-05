"""The comment anchor resolvers: what the agent is told is under each mark.

Text resolution is pure Python. PDF resolution needs poppler's ``pdftotext`` and
``pdftoppm``; HTML and SVG resolution needs the managed browser's Chromium. Those tests skip
with the reason when the tool is missing, as the browser tests do, rather than install it.
"""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from clio_agent.gact.documents.anchors import MarkTarget, mark_mode, resolver_for
from clio_agent.gact.documents.mark_images import MarkBox, labelled_overview_png
from clio_agent.gact.documents.models import DocumentAnchor
from clio_agent.gact.documents.profiles import document_format


def _target(path: Path, *, name: str | None = None, view: Path | None = None) -> MarkTarget:
    root = path.parent
    workspace = SimpleNamespace(root_path=str(root))
    app = SimpleNamespace(state=SimpleNamespace(workspaces={"ws": workspace}))
    record = SimpleNamespace(name=name or path.name, workspace_id="ws")
    version = SimpleNamespace(artifact_id="artifact_1", version=1, sha256="0" * 64)
    return MarkTarget(
        app=app,
        record=record,  # type: ignore[arg-type]
        version=version,  # type: ignore[arg-type]
        path=path,
        profile=document_format(name or path.name).profile,
        view_path=view or path,
    )


def _resolve(target: MarkTarget, anchor: DocumentAnchor) -> Any:
    resolver = resolver_for(target.profile, anchor.profile)
    assert resolver is not None, (target.profile, anchor.profile)
    return resolver.resolve(target, anchor)


def _png(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


@pytest.fixture()
def chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    """The managed browser's Chromium, allowed to run without its sandbox as the browser
    tests allow it (cloud and CI hosts run as root, where the sandbox cannot start)."""
    from clio_agent.gact.browser import install

    roots = [install.browsers_root()]
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        roots.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    found = next((path for root in roots if (path := install.chromium_executable(root))), None)
    if found is None:
        pytest.skip("no Chromium for the pinned Playwright release is installed")
    monkeypatch.setattr(install, "ensure_chromium", lambda: found)
    monkeypatch.setenv("CLIO_BROWSER_ALLOW_UNSANDBOXED", "1")


def _needs_poppler() -> None:
    if not (shutil.which("pdftotext") and shutil.which("pdftoppm")):
        pytest.skip("poppler's pdftotext and pdftoppm are not installed")


# --- text ---------------------------------------------------------------------------------

REPORT = """# Results

After enabling the **burst buffer**, throughput rose by 40% in all configurations.
The exception is the 4 KB case.
"""


def test_text_quote_names_its_source_lines_and_context(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    path.write_text(REPORT)
    anchor = DocumentAnchor(
        profile="text-quote",
        exact="throughput rose by 40% in all configurations",
        prefix="burst buffer, ",
        suffix=". The exception",
    )
    mark = _resolve(_target(path), anchor)
    lines = dict(mark.lines)
    assert mark.mode == mark_mode(anchor) == "text"
    assert lines["Selected"] == '"throughput rose by 40% in all configurations"'
    assert lines["Before"].endswith('burst buffer, "')
    assert lines["After"].startswith('". The exception')
    assert lines["Source"] == "line 3 of report.md"
    assert not mark.notes


def test_text_quote_moves_with_its_text_and_is_outdated_when_rewritten(tmp_path: Path) -> None:
    old = tmp_path / "v1.md"
    old.write_text(REPORT)
    moved = tmp_path / "v2.md"
    moved.write_text("Intro added.\n\n" + REPORT)
    rewritten = tmp_path / "v3.md"
    rewritten.write_text("# Results\n\nThroughput rose in most configurations.\n")
    anchor = DocumentAnchor(profile="text-quote", exact="rose by 40% in all configurations")
    resolver = resolver_for("markdown", "text-quote")
    assert resolver is not None
    state, carried = resolver.reanchor(
        _target(old, name="report.md"), _target(moved, name="report.md"), anchor
    )
    assert state == "moved" and carried.start is not None and carried.start > 0
    state, _same = resolver.reanchor(
        _target(old, name="report.md"), _target(rewritten, name="report.md"), anchor
    )
    assert state == "outdated"


def test_a_quote_missing_from_the_version_is_stated_not_dropped(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    path.write_text(REPORT)
    mark = _resolve(_target(path), DocumentAnchor(profile="text-quote", exact="not in the file"))
    assert mark.lines == [("Selected", '"not in the file"')]
    assert mark.notes and mark.notes[0].startswith("quote_not_found")


# --- images -------------------------------------------------------------------------------


def test_a_region_on_a_raster_image_is_its_box_and_crop(tmp_path: Path) -> None:
    path = tmp_path / "figure.png"
    image = Image.new("RGB", (400, 200), "white")
    image.paste((255, 0, 0), (0, 0, 200, 100))
    image.save(path)
    anchor = DocumentAnchor(profile="region", rect=[0.0, 0.0, 0.5, 0.5])
    mark = _resolve(_target(path), anchor)
    assert mark.mode == "region"
    assert dict(mark.lines)["Box"] == "x 0% to 50%, y 0% to 50% of the image"
    crop = _png(mark.crop)
    assert crop.size == (200, 100)
    assert crop.convert("RGB").getpixel((100, 50)) == (255, 0, 0)


def test_overview_draws_each_mark_and_stays_bounded(tmp_path: Path) -> None:
    image = Image.new("RGB", (5000, 1000), "white")
    overview = _png(
        labelled_overview_png(
            image,
            [MarkBox("S1", (0.1, 0.2, 0.2, 0.3)), MarkBox("S2", (0.6, 0.1, 0.3, 0.5))],
        )
    )
    assert max(overview.size) == 2048
    colours = {overview.convert("RGB").getpixel((x, 200)) for x in range(0, 2048, 4)}
    assert (37, 99, 235) in colours  # the mark's box colour is drawn


# --- PDF and Office renditions ------------------------------------------------------------


def _pdf(path: Path, lines: list[str]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure = plt.figure(figsize=(8.5, 11))
    for index, text in enumerate(lines):
        figure.text(0.1, 0.9 - index * 0.05, text, fontsize=14)
    figure.savefig(path, format="pdf")
    plt.close(figure)


def test_pdf_text_and_region_marks_read_the_page(tmp_path: Path) -> None:
    _needs_poppler()
    path = tmp_path / "paper.pdf"
    _pdf(path, ["Throughput rose by forty percent overall.", "The legend covers the bars."])
    text = _resolve(
        _target(path),
        DocumentAnchor(profile="pdf-quad", exact="legend covers", page_index=0),
    )
    lines = dict(text.lines)
    assert lines["Selected"] == '"legend covers"'
    assert lines["Before"].endswith('The "') and lines["Page"] == "1"
    assert text.rect is not None
    region = _resolve(
        _target(path),
        DocumentAnchor(profile="region", page_index=0, rect=[0.05, 0.05, 0.9, 0.08]),
    )
    assert "Throughput rose by forty percent overall." in region.under
    assert dict(region.lines)["Box"].endswith("of page 1")
    assert region.crop and _png(region.crop).size[0] > 1000  # 2 px per point, 0.9 of 612 pt


def test_a_region_on_a_word_rendition_names_the_paragraph(tmp_path: Path) -> None:
    _needs_poppler()
    from docx import Document

    source = tmp_path / "report.docx"
    document = Document()
    document.add_heading("Results", level=1)
    document.add_paragraph("Throughput rose by forty percent overall.")
    document.save(source)
    rendition = tmp_path / "report.docx.v1.pdf"
    _pdf(rendition, ["Results", "Throughput rose by forty percent overall."])
    mark = _resolve(
        _target(source, view=rendition),
        DocumentAnchor(profile="pdf-quad", exact="forty percent", page_index=0),
    )
    assert dict(mark.lines)["Source"] == 'paragraph 2, under heading "Results"'


def test_a_region_on_a_slide_rendition_names_the_slide_and_shapes(tmp_path: Path) -> None:
    _needs_poppler()
    from pptx import Presentation
    from pptx.util import Inches

    source = tmp_path / "deck.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    picture = tmp_path / "chart.png"
    Image.new("RGB", (40, 30), "white").save(picture)
    slide.shapes.add_picture(str(picture), Inches(5), Inches(2), width=Inches(4))
    deck.save(source)
    rendition = tmp_path / "deck.pptx.v1.pdf"
    _pdf(rendition, ["slide"])
    target = _target(source, view=rendition)
    _registry(target, {"chart.png": picture.read_bytes()})
    mark = _resolve(
        target, DocumentAnchor(profile="region", page_index=0, rect=[0.55, 0.25, 0.4, 0.5])
    )
    lines = dict(mark.lines)
    assert lines["Source"].startswith('slide 1, shape 2 "Picture 1"')
    assert lines["Picture"] == (
        'shape 2 "Picture 1": the workspace image chart.png (artifact art_chart.png, version 1)'
    )
    assert lines["Made by"].startswith("Producer unknown")
    assert "Then" not in lines  # no script to change, so no rebuild advice


def _registry(target: MarkTarget, images: dict[str, bytes]) -> None:
    """A registry holding one workspace image artifact per name, made by no recorded call."""
    import hashlib

    records = [
        SimpleNamespace(
            name=name,
            workspace_id="ws",
            versions=[
                SimpleNamespace(
                    artifact_id=f"art_{name}",
                    version=1,
                    sha256=hashlib.sha256(data).hexdigest(),
                    created_at="2026-11-01T00:00:00Z",
                )
            ],
        )
        for name, data in images.items()
    ]
    target.app.state.artifact_registry = SimpleNamespace(
        capture_released=None,
        list_for_workspace=lambda _ws: records,
        transforms_generating=lambda _id: [],
    )


def test_a_region_on_a_word_rendition_names_the_picture_it_covers(tmp_path: Path) -> None:
    _needs_poppler()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from docx import Document
    from docx.shared import Inches

    first, second = tmp_path / "first.png", tmp_path / "second.png"
    Image.new("RGB", (60, 40), "red").save(first)
    Image.new("RGB", (60, 40), "blue").save(second)
    source = tmp_path / "report.docx"
    document = Document()
    document.add_paragraph("Figures")
    document.add_picture(str(first), width=Inches(2))
    document.add_picture(str(second), width=Inches(2))
    document.save(source)
    rendition = tmp_path / "report.docx.v1.pdf"
    figure = plt.figure(figsize=(8.5, 11))
    for top, path in ((0.1, first), (0.6, second)):  # drawn in document order, one above
        axes = figure.add_axes((0.1, 1 - top - 0.2, 0.4, 0.2))
        axes.imshow(plt.imread(path))
        axes.axis("off")
    figure.savefig(rendition, format="pdf")
    plt.close(figure)
    target = _target(source, view=rendition)
    _registry(target, {"second.png": second.read_bytes()})
    mark = _resolve(
        target, DocumentAnchor(profile="region", page_index=0, rect=[0.05, 0.55, 0.5, 0.3])
    )
    lines = dict(mark.lines)
    assert lines["Picture"].startswith(
        "picture 2 of the document, in paragraph 3: the workspace image second.png"
    )


# --- HTML and SVG -------------------------------------------------------------------------

DASHBOARD = """<!doctype html><html><body style="margin:0;font:16px sans-serif">
<main><section class="controls" style="padding:96px">
<button id="run-sim" class="btn primary" disabled>Run simulation</button></section>
<img src="https://example.org/x.png" alt="remote"></main></body></html>"""


def test_an_element_on_an_html_artifact_is_found_by_its_selector(
    tmp_path: Path, chromium: None
) -> None:
    path = tmp_path / "dashboard.html"
    path.write_text(DASHBOARD)
    anchor = DocumentAnchor(profile="dom", selector="#run-sim", tag="button", viewport_width=1280)
    mark = _resolve(_target(path), anchor)
    lines = dict(mark.lines)
    assert mark.mode == "element"
    assert lines["Element"].startswith('<button id="run-sim">, role button "Run simulation"')
    assert lines["Text"] == '"Run simulation"'
    assert lines["HTML"].startswith('<button id="run-sim"')
    assert lines["Box"].endswith("in a 1280 px wide view")
    assert mark.crop and _png(mark.crop).size[1] > 30  # 2x of the button's height
    assert any(note.startswith("remote_resources_refused: 1") for note in mark.notes)


def test_the_server_render_is_offline_even_for_library_cdns(tmp_path: Path, chromium: None) -> None:
    """HTML opens in the shared browser now (H5): the server's render keeps no CDN list."""
    path = tmp_path / "chart.html"
    path.write_text(
        '<!doctype html><html><body><button id="go">Go</button>'
        '<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2"></script>'
        "</body></html>"
    )
    anchor = DocumentAnchor(profile="dom", selector="#go", tag="button", viewport_width=800)
    mark = _resolve(_target(path), anchor)
    assert dict(mark.lines)["Element"].startswith('<button id="go">, role button "Go"')
    assert any(note.startswith("remote_resources_refused: 1 ") for note in mark.notes)


def test_a_region_on_an_html_artifact_lists_what_it_covers(tmp_path: Path, chromium: None) -> None:
    path = tmp_path / "dashboard.html"
    path.write_text(DASHBOARD)
    anchor = DocumentAnchor(profile="region", rect=[0.0, 0.0, 0.5, 0.5], viewport_width=1280)
    mark = _resolve(_target(path), anchor)
    assert '<button id="run-sim"> button "Run simulation"' in mark.under
    assert mark.crop


def test_an_element_on_an_svg_names_the_series(tmp_path: Path, chromium: None) -> None:
    path = tmp_path / "throughput.svg"
    path.write_text(
        '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">'
        '<g id="legend"><text x="300" y="20">burst buffer</text>'
        '<text x="300" y="40">baseline</text></g>'
        '<polyline id="series-baseline" points="10,150 100,120 200,90" stroke="black" '
        'fill="none"/></svg>'
    )
    element = _resolve(
        _target(path),
        DocumentAnchor(profile="dom", selector="#series-baseline", tag="polyline"),
    )
    assert dict(element.lines)["Element"].startswith('<polyline id="series-baseline">')
    region = _resolve(_target(path), DocumentAnchor(profile="region", rect=[0.7, 0.0, 0.3, 0.3]))
    assert '<g id="legend">' in region.under and "burst buffer" in region.under
    # The image viewer's picker names elements by their path in the SVG (svg-element-layer).
    picked = _resolve(
        _target(path),
        DocumentAnchor(profile="dom", selector="svg > g > text:nth-of-type(2)", tag="text"),
    )
    lines = dict(picked.lines)
    assert lines["Element"].startswith("<text") and "baseline" in lines.get("Text", picked.under)
    assert not any(note.startswith("element_not_found") for note in picked.notes)
