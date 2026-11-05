"""The LaTeX build: entry detection, staging, diagnostics and the read set (LaTeX ledger L4–L5c).

The diagnostic fixtures are Tectonic 0.17.0's own output and logs, captured from the
Phase 0 probes. The live builds run when a pinned Tectonic is available (``CLIO_DOCUMENT_TECTONIC``
or the managed install) and skip otherwise, as the Chromium-backed tests do.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.gact.documents.latex.build import build_project, read_dependencies
from clio_agent.gact.documents.latex.diagnostics import build_failed, parse_diagnostics
from clio_agent.gact.documents.latex.project import (
    ProjectError,
    StagedProject,
    find_entry,
    stage_project,
)

# --- entry and staging -------------------------------------------------------------------


def test_the_one_file_with_documentclass_is_the_entry(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}x\\end{document}"
    )
    (tmp_path / "intro.tex").write_text("\\section{Intro}\n% \\documentclass{commented}\n")
    assert find_entry(tmp_path).name == "main.tex"
    (tmp_path / "other.tex").write_text("\\documentclass{article}\n")
    with pytest.raises(ProjectError) as caught:
        find_entry(tmp_path)
    assert caught.value.reason == "latex_entry_ambiguous"
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ProjectError) as missing:
        find_entry(empty)
    assert missing.value.reason == "latex_entry_missing"


def test_staging_copies_the_projects_sources_with_their_paths(tmp_path: Path) -> None:
    project = tmp_path / "paper"
    (project / "sections").mkdir(parents=True)
    (project / "figs").mkdir()
    (project / ".git").mkdir()
    (project / "main.tex").write_text("\\documentclass{article}")
    (project / "sections" / "eval.tex").write_text("\\section{Eval}")
    (project / "figs" / "a.png").write_bytes(b"png")
    (project / "refs.bib").write_text("@misc{k,title={t}}")
    (project / "notes.docx").write_bytes(b"not a source")
    (project / ".git" / "config.tex").write_text("never staged")
    staged = stage_project(project / "main.tex", tmp_path / "build")
    assert sorted(staged.sources) == ["figs/a.png", "main.tex", "refs.bib", "sections/eval.tex"]
    assert (tmp_path / "build" / "sections" / "eval.tex").read_text() == "\\section{Eval}"
    assert set(staged.texts()) == {"main.tex", "refs.bib", "sections/eval.tex"}


def test_a_project_over_its_bounds_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.documents.latex import project as project_module

    (tmp_path / "main.tex").write_text("\\documentclass{article}")
    (tmp_path / "b.tex").write_text("x")
    monkeypatch.setattr(project_module, "MAX_FILES", 1)
    with pytest.raises(ProjectError) as caught:
        stage_project(tmp_path / "main.tex", tmp_path / "build")
    assert caught.value.reason == "latex_project_too_large"


# --- diagnostics from Tectonic 0.17.0's own output ---------------------------------------

_TEXTS = {
    "main.tex": (
        "\\documentclass{article}\n\\begin{document}\n\\section{Intro}\\label{sec:intro}\n"
        "% \\ref{sec:missing} in a comment does not count\n"
        "See Section~\\ref{sec:missing} and~\\cite{nokey}.\n\\input{sections/eval}\n"
    ),
    "sections/eval.tex": "\\section{Eval}\nThe figure:\n\\includegraphics{figs/missing.png}\n",
}


def test_located_errors_overfull_boxes_and_undefined_keys_are_typed() -> None:
    output = "\n".join(
        [
            "warning: main.tex:9: Overfull \\hbox (282.65489pt too wide) in paragraph at lines 2--9",
            "warning: main.tex:9: Underfull \\hbox (badness 10000) in paragraph at lines 2--9",
            "warning: warnings were issued by the TeX engine; use --print and/or --keep-logs.",
            "error: sections/eval:3: Unable to load picture or PDF file 'figs/missing.png'",
            "error: something bad happened inside XeTeX; its output follows:",
            "error: the XeTeX engine had an unrecoverable error",
        ]
    )
    log = "\n".join(
        [
            "LaTeX Warning: Reference `sec:missing' on page 1 undefined on input line 5.",
            "LaTeX Warning: Citation `nokey' on page 1 undefined on input line 5.",
            "LaTeX Font Warning: Font shape `TU/ptm/b/it' undefined",
        ]
    )
    found = parse_diagnostics(output, log=log, texts=_TEXTS)
    kinds = {item.kind: item for item in found}
    assert kinds["overfull_box"].file == "main.tex" and kinds["overfull_box"].line == 9
    assert kinds["overfull_box"].detail == "282.65489pt"
    # Tectonic drops the .tex of an \input file; the diagnostic names the project file.
    assert kinds["missing_file"].file == "sections/eval.tex"
    assert kinds["missing_file"].detail == "figs/missing.png"
    reference = kinds["undefined_reference"]
    assert (reference.file, reference.line, reference.page) == ("main.tex", 5, 1)
    assert kinds["undefined_citation"].detail == "nokey"
    assert kinds["font_warning"].message.startswith("Font shape")
    # The engine's summary lines are not diagnoses of their own.
    assert not any("XeTeX" in item.message for item in found)
    assert build_failed(found)


def test_a_missing_package_says_network_is_needed_only_when_the_bundle_was_unreachable() -> None:
    offline = "\n".join(
        [
            'warning: failure fetching "siunitx.sty" from network (3/3)',
            "error: main.tex:2: ! LaTeX Error: File `siunitx.sty' not found.",
        ]
    )
    [item] = parse_diagnostics(offline, texts=_TEXTS)
    assert item.kind == "missing_package" and item.detail == "siunitx.sty"
    assert (item.file, item.line) == ("main.tex", 2)
    assert "needs network access the first time" in item.message
    online = "error: main.tex:2: ! LaTeX Error: File `nosuchpkg.sty' not found."
    [absent] = parse_diagnostics(online, texts=_TEXTS)
    assert absent.message == "nosuchpkg.sty is not in the TeX bundle."
    cold = (
        "error: this bundle isn't cached, and we couldn't get it from the internet. Error: "
        "error sending request for url (https://relay.fullyjustified.net/x.index.gz)"
    )
    [bundle] = parse_diagnostics(cold)
    assert bundle.kind == "missing_package" and bundle.detail == "bundle"


def test_shell_escape_biber_and_bibtex_styles_are_named() -> None:
    minted = (
        "error: main.tex:3: Package minted Error: You must invoke LaTeX with the "
        "-shell-escape flag."
    )
    [shell] = parse_diagnostics(minted)
    assert shell.kind == "shell_escape_unsupported" and shell.detail == "minted"
    biber = "note: Running external tool biber ...\nerror: No such file or directory (os error 2)"
    [backend] = parse_diagnostics(biber)
    assert backend.kind == "unsupported_bibliography_backend"
    blg = "I couldn't open style file plain.bst\n---line 4 of file main.aux"
    [style] = parse_diagnostics("", blg=blg)
    assert style.kind == "missing_package" and style.detail == "plain.bst"


def test_a_clean_build_with_only_warnings_is_not_failed() -> None:
    found = parse_diagnostics(
        "warning: main.tex:4: Overfull \\hbox (11.99588pt too wide) in paragraph at lines 4--4"
    )
    assert [item.kind for item in found] == ["overfull_box"]
    assert not build_failed(found)


def test_the_read_set_comes_from_tectonics_dependency_list(tmp_path: Path) -> None:
    staged = StagedProject(
        root=tmp_path / "src",
        entry="main.tex",
        sources={"main.tex": "a", "body.tex": "b", "refs.bib": "c", "unused.tex": "d"},
    )
    # Tectonic 0.17 prefixes the inputs after the first with the output folder.
    out = tmp_path / "out"
    rules = f"{out}/main.pdf : main.tex \\\n  {out}/body.tex \\\n  {out}/refs.bib\n"
    assert read_dependencies(rules, staged, out_dir=out) == {
        "main.tex": "a",
        "body.tex": "b",
        "refs.bib": "c",
    }


# --- live builds ---------------------------------------------------------------------------


def _tectonic_available() -> bool:
    from clio_agent.runtime.tectonic import installed_tectonic

    override = os.environ.get("CLIO_DOCUMENT_TECTONIC", "").strip()
    return bool(override and Path(override).is_file()) or installed_tectonic() is not None


live = pytest.mark.skipif(
    not _tectonic_available(), reason="no pinned Tectonic is installed (CLIO_DOCUMENT_TECTONIC)"
)


@live
def test_a_multi_file_paper_builds_with_its_citation_and_read_set(tmp_path: Path) -> None:
    paper = tmp_path / "paper"
    (paper / "sections").mkdir(parents=True)
    (paper / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\input{sections/eval}\n"
        "\\bibliographystyle{plain}\\bibliography{refs}\n\\end{document}\n"
    )
    (paper / "sections" / "eval.tex").write_text("Burst buffers help~\\cite{knuth84}.\n")
    (paper / "refs.bib").write_text(
        "@article{knuth84, author={Donald E. Knuth}, title={Literate Programming},"
        " journal={The Computer Journal}, year={1984}}\n"
    )
    (paper / "unused.tex").write_text("never read")
    result = build_project(paper / "main.tex", workspace_root=tmp_path)
    assert result.ok, result.summary()
    assert result.page_count == 1 and result.pdf.startswith(b"%PDF")
    assert result.synctex
    assert set(result.read) == {"main.tex", "sections/eval.tex", "refs.bib"}
    assert "unused.tex" in result.sources and "unused.tex" not in result.read


@live
def test_a_broken_reference_fails_the_build_although_tectonic_exits_cleanly(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nSee~\\ref{nowhere}.\n\\end{document}\n"
    )
    result = build_project(tmp_path / "main.tex", workspace_root=tmp_path)
    assert result.exit_code == 0 and result.pdf
    assert not result.ok
    [undefined] = [item for item in result.diagnostics if item.kind == "undefined_reference"]
    assert (undefined.file, undefined.line, undefined.detail) == ("main.tex", 3, "nowhere")


# --- the PDF rendition ---------------------------------------------------------------------


def _version(entry: Path):  # noqa: ANN202 - a minimal stand-in for ArtifactVersion
    from types import SimpleNamespace

    from clio_agent.gact.documents.latex.project import _sha256

    return SimpleNamespace(path=str(entry), sha256=_sha256(entry))


def test_a_rendition_records_the_files_its_build_read_and_goes_stale_when_one_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.documents.latex import rendition
    from clio_agent.gact.documents.latex.build import BuildResult
    from clio_agent.gact.documents.latex.project import _sha256

    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("\\documentclass{article}")
    (paper / "refs.bib").write_text("@misc{a,title={one}}")
    read = {name: _sha256(paper / name) for name in ("main.tex", "refs.bib")}

    def fake_build(entry: Path, *, workspace_root: Path) -> BuildResult:
        del workspace_root
        assert entry == paper / "main.tex"
        return BuildResult(
            entry="main.tex",
            ok=True,
            exit_code=0,
            diagnostics=[],
            sources=read,
            read=read,
            pdf=b"%PDF-1.7",
            synctex=b"sync",
            page_count=1,
            network="bundle_hosts",
        )

    monkeypatch.setattr(rendition, "build_project", fake_build)
    version = _version(paper / "main.tex")
    target = tmp_path / "preview" / "main.pdf"
    target.parent.mkdir()
    converter, producer = rendition.build_rendition(version, workspace_root=tmp_path, target=target)
    assert converter == "tectonic" and target.read_bytes() == b"%PDF-1.7"
    assert (tmp_path / "preview" / "main.synctex.gz").read_bytes() == b"sync"
    assert producer["source_files"] == read and producer["build_ok"] is True
    assert rendition.sources_current(version, producer)
    # Editing only the bibliography makes the retained rendition stale (L6).
    (paper / "refs.bib").write_text("@misc{a,title={two}}")
    assert not rendition.sources_current(version, producer)


def test_a_changed_entry_or_a_build_without_a_pdf_is_a_worded_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.documents.latex import rendition
    from clio_agent.gact.documents.latex.build import BuildResult
    from clio_agent.gact.documents.latex.diagnostics import Diagnostic

    entry = tmp_path / "main.tex"
    entry.write_text("\\documentclass{article}")
    version = _version(entry)
    monkeypatch.setattr(
        rendition,
        "build_project",
        lambda *a, **k: BuildResult(
            entry="main.tex",
            ok=False,
            exit_code=1,
            sources={},
            read={},
            diagnostics=[
                Diagnostic(
                    "missing_file",
                    "figs/a.png is not in the project.",
                    "main.tex",
                    4,
                    detail="figs/a.png",
                )
            ],
        ),
    )
    with pytest.raises(rendition.LatexRenditionError, match=r"missing_file.*main.tex:4"):
        rendition.build_rendition(version, workspace_root=tmp_path, target=tmp_path / "o.pdf")
    entry.write_text("\\documentclass{report}")
    with pytest.raises(rendition.LatexRenditionError, match="changed since this version"):
        rendition.build_rendition(version, workspace_root=tmp_path, target=tmp_path / "o.pdf")
