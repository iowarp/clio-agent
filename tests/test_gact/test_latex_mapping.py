"""SyncTeX, the outline and the ``source-map`` resolver (LaTeX ledger L3, L7, Phase 3).

The SyncTeX fixture follows Tectonic 0.17.0's format as recorded on a real build: the paragraph's
line boxes carry the line where the paragraph ended, and the glue records carry each word's
line. The live tests build a real paper and resolve marks on its PDF.
"""

from __future__ import annotations

import gzip
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.documents.latex.outline import build_outline, node_at
from clio_agent.gact.documents.latex.synctex import SP_PER_BP, SyncIndex


def _sp(points: float) -> int:
    return round(points * SP_PER_BP)


_SYNCTEX = "\n".join(
    [
        "SyncTeX Version:1",
        "Input:1:/tmp/latex-x/src/main.tex",
        "Input:2:",
        "Output:pdf",
        "Content:",
        "{1",
        # The page's column box: from the paragraph's end line, main.tex 6.
        f"(1,6:{_sp(49)},{_sp(130)}:{_sp(514)},{_sp(585)},0",
        "Input:3:/tmp/latex-x/src/sections/eval.tex",
        f"(3,1:{_sp(49)},{_sp(234)}:{_sp(251)},{_sp(7)},0",
        f"g3,1:{_sp(152)},{_sp(241)}",
        # The paragraph's first row: its words came from sections/eval.tex line 2.
        f"h3,2:{_sp(49)},{_sp(256)}:{_sp(10)},0,0",
        f"g3,2:{_sp(87)},{_sp(256)}",
        f"g3,2:{_sp(119)},{_sp(256)}",
        # A row in a bundle file, which is not the project's.
        f"g2,40:{_sp(60)},{_sp(300)}",
        "}1",
        "Postamble:",
    ]
).encode()


def test_a_point_maps_to_the_words_line_not_the_paragraphs_end() -> None:
    index = SyncIndex.parse(gzip.compress(_SYNCTEX), ["main.tex", "sections/eval.tex"])
    assert index.files() == ["main.tex", "sections/eval.tex"]
    # "Burst" sits just above the baseline at 256; the column box would say main.tex 6.
    assert index.source_at(0, 70, 252) == ("sections/eval.tex", 2)
    assert index.source_at(0, 160, 238) == ("sections/eval.tex", 1)
    # Away from text, the nearest record decides.
    assert index.source_at(0, 60, 290) == ("sections/eval.tex", 2)
    assert index.source_at(3, 0, 0) is None


def test_a_rectangle_gives_line_ranges_and_a_line_gives_its_places() -> None:
    index = SyncIndex.parse(_SYNCTEX, ["main.tex", "sections/eval.tex"])
    assert index.lines_in(0, 40, 230, 300, 262) == {"sections/eval.tex": (1, 2)}
    [(page, (x, top, width, height))] = index.places("sections/eval.tex", 2, 2)
    assert page == 0 and round(x) == 49 and round(width) >= 70
    assert 240 < top < 256 < top + height


_TEXTS = {
    "main.tex": "\n".join(
        [
            "\\documentclass{article}",  # 1
            "\\begin{document}",  # 2
            "\\begin{abstract}",  # 3
            "We study burst buffers.",  # 4
            "\\end{abstract}",  # 5
            "\\input{sections/eval}",  # 6
            "\\bibliography{refs}",  # 7
            "\\end{document}",  # 8
        ]
    ),
    "sections/eval.tex": "\n".join(
        [
            "\\section{Evaluation}\\label{sec:eval}",  # 1
            "We compare~\\cite{knuth84,missing2020}.",  # 2
            "\\subsection{Scaling}",  # 3
            "\\begin{figure}[t]",  # 4
            "\\includegraphics{figs/tput}",  # 5
            "\\caption{Throughput by block size}\\label{fig:tput}",  # 6
            "\\end{figure}",  # 7
            "% \\section{Commented out}",  # 8
            "\\begin{equation}a=b\\label{eq:ab}\\end{equation}",  # 9
        ]
    ),
    "refs.bib": "@article{knuth84, title={T}}\n",
}


def test_the_outline_follows_inputs_and_attaches_labels_and_citations() -> None:
    roots = build_outline("main.tex", _TEXTS)
    assert [node.kind for node in roots] == ["abstract", "section", "bibliography"]
    section = roots[1]
    assert (section.title, section.labels, section.file, section.start_line) == (
        "Evaluation",
        ["sec:eval"],
        "sections/eval.tex",
        1,
    )
    assert section.citations == [
        {"key": "knuth84", "resolved": True},
        {"key": "missing2020", "resolved": False},
    ]
    [scaling] = section.children
    assert scaling.title == "Scaling" and scaling.level == 3
    figure, equation = scaling.children
    assert (figure.kind, figure.title, figure.labels) == (
        "figure",
        "Throughput by block size",
        ["fig:tput"],
    )
    assert (figure.start_line, figure.end_line) == (4, 7)
    assert equation.kind == "equation" and equation.labels == ["eq:ab"]
    # A section that ends in another file ends at its own file's last line read.
    assert section.end_line == 9
    assert node_at(roots, "sections/eval.tex", 5) is figure
    assert node_at(roots, "sections/eval.tex", 2) is section


# --- live: a real build, and marks on its PDF -----------------------------------------------


def _tectonic_available() -> bool:
    from clio_agent.runtime.tectonic import installed_tectonic

    override = os.environ.get("CLIO_DOCUMENT_TECTONIC", "").strip()
    return bool(override and Path(override).is_file()) or installed_tectonic() is not None


live = pytest.mark.skipif(
    not _tectonic_available(), reason="no pinned Tectonic is installed (CLIO_DOCUMENT_TECTONIC)"
)


def _png(path: Path, width: int = 120, height: int = 60) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (40, 90, 160)).save(path)


@pytest.fixture
def built(tmp_path: Path):  # noqa: ANN201 - the built paper and its map
    from clio_agent.gact.documents.latex.build import build_project
    from clio_agent.gact.documents.latex.mapping import paper_map

    paper = tmp_path / "paper"
    (paper / "sections").mkdir(parents=True)
    (paper / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n"
        "\\input{sections/eval}\n\\end{document}\n"
    )
    (paper / "sections" / "eval.tex").write_text(
        "\\section{Scaling}\\label{sec:scaling}\n"
        "We observe a 3.2x speedup over the baseline\n"
        "once the cache is warm.\n\n"
        "\\begin{figure}[h]\n\\centering\n\\includegraphics[width=4cm]{figs/tput.png}\n"
        "\\caption{Throughput}\\label{fig:tput}\n\\end{figure}\n"
    )
    _png(paper / "figs" / "tput.png")
    result = build_project(paper / "main.tex", workspace_root=tmp_path)
    assert result.ok, result.summary()
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(result.pdf)
    synctex = tmp_path / "main.synctex.gz"
    synctex.write_bytes(result.synctex)
    producer = {"entry": "main.tex", "source_files": result.read, "synctex": str(synctex)}
    return paper, pdf, paper_map(paper / "main.tex", producer, pdf)


@live
def test_a_real_build_maps_both_ways(built) -> None:  # noqa: ANN001
    import re
    import subprocess

    paper, pdf, mapped = built
    words = subprocess.run(
        ["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True, check=True
    ).stdout
    found = re.search(
        r'xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">speedup<', words
    )
    assert found
    x = (float(found.group(1)) + float(found.group(3))) / 2
    y = (float(found.group(2)) + float(found.group(4))) / 2
    assert mapped.index.source_at(0, x, y) == ("sections/eval.tex", 2)
    assert mapped.source_line("sections/eval.tex", 2) == (
        'sections/eval.tex, line 2, in Section "Scaling" (sec:scaling)'
    )
    [(page, _rect)] = mapped.index.places("sections/eval.tex", 3, 3)
    assert page == 0


@live
def test_a_region_on_the_figure_names_its_environment_and_graphics(built) -> None:  # noqa: ANN001
    from clio_agent.gact.documents.anchors import MarkTarget
    from clio_agent.gact.documents.latex.anchor_source_map import SourceMapResolver
    from clio_agent.gact.documents.models import DocumentAnchor

    paper, pdf, mapped = built
    figure = node_at(mapped.outline, "sections/eval.tex", 7)
    assert figure is not None and figure.kind == "figure" and figure.places
    place = figure.places[0]
    from pypdf import PdfReader

    box = PdfReader(str(pdf)).pages[0].mediabox
    width, height = float(box.width), float(box.height)
    x, top, w, h = place["rect"]
    rect = [x / width, top / height, w / width, h / height]

    registry = SimpleNamespace(find_version_by_path=lambda workspace, path: None)
    app = SimpleNamespace(state=SimpleNamespace(artifact_registry=registry, workspaces={}))
    target = MarkTarget(
        app=app,
        record=SimpleNamespace(name="main.tex", workspace_id="ws"),
        version=SimpleNamespace(path=str(paper / "main.tex"), artifact_id="a", version=1),
        path=paper / "main.tex",
        profile="latex",
        view_path=pdf,
    )
    target.cache["latex_paper"] = mapped
    import clio_agent.gact.artifacts.registry as registry_module

    original = registry_module.get_registry
    registry_module.get_registry = lambda _app: registry
    try:
        mark = SourceMapResolver().resolve(
            target, DocumentAnchor(profile="region", page_index=0, rect=rect)
        )
    finally:
        registry_module.get_registry = original
    lines = dict(mark.lines)
    assert lines["Source"].startswith("sections/eval.tex, line")
    assert lines["Figure"] == 'figure "Throughput" (fig:tput), sections/eval.tex lines 5 to 9'
    assert lines["Graphics"] == "figs/tput.png; it is not a registered artifact"
    assert lines["Made by"].startswith("Producer unknown")
    assert mark.crop is not None
