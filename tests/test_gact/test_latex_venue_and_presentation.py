"""Venue checks from template rules, and the readable ``latex_build`` result (LaTeX ledger).

The presentation cases are the tool's actual result shapes: a small success, a large result,
an empty one, a failed build with and without a PDF, a missing result and the running row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.agents.native_presenters_latex import (
    MAX_LISTED,
    latex_build_presentation,
    latex_build_start,
)
from clio_agent.gact.documents.latex.venue import check_venue
from clio_agent.gact.tool_result_presentation import ToolPresentation

# --- venue rules ---------------------------------------------------------------------------


def _template(root: Path, name: str, rules: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "template.yaml").write_text(f"name: {name}\ntitle: {name}\nrules:\n{rules}")
    return root


def _pdf(path: Path, pages: int, size: tuple[float, float] = (612, 792)) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=size[0], height=size[1])
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _paper(root: Path) -> Path:
    paper = root / "paper"
    (paper / "sections").mkdir(parents=True)
    (paper / "main.tex").write_text(
        "\\documentclass[sigconf,review,anonymous]{acmart}\n\\usepackage{booktabs}\n"
        "\\begin{document}\n\\title{T}\n\\author{Ada Lovelace}\n"
        "\\affiliation{\\institution{Analytical Engines Ltd}\\country{UK}}\n\\maketitle\n"
        "\\input{sections/eval}\n\\end{document}\n"
    )
    (paper / "sections" / "eval.tex").write_text(
        "\\section{Evaluation}\nAs Ada Lovelace showed in our earlier work,\n"
        "% Ada Lovelace in a comment does not count\n"
        "code is at \\url{https://github.com/ada/engine}.\n"
        "\\section*{Acknowledgments}\nThanks.\n"
    )
    return paper / "main.tex"


def test_a_planted_author_name_acknowledgments_and_urls_are_found(tmp_path: Path) -> None:
    roots = [
        _template(
            tmp_path / "templates",
            "acm-sigconf",
            "  page_limit: 12\n  references_count: false\n  double_blind: true\n"
            "  required_packages: [booktabs]\n  forbidden_packages: [times]\n"
            "  paper_size: letter\n",
        )
    ]
    entry = _paper(tmp_path)
    report = check_venue(
        "acm-sigconf", entry=entry, pdf=_pdf(tmp_path / "p.pdf", 3), page_count=3, roots=roots
    )
    checks = {check["rule"]: check for check in report["checks"]}
    assert report["status"] == "failed"
    planted = checks["Double-blind: author names"]
    assert planted["status"] == "failed"
    assert (planted["file"], planted["line"]) == ("sections/eval.tex", 2)
    assert checks["Double-blind: acknowledgments"]["status"] == "failed"
    assert checks["Double-blind: URLs"]["status"] == "to_check"
    assert checks["Requires booktabs"]["status"] == "passed"
    assert checks["Forbids times"]["status"] == "passed"
    assert checks["Page limit"]["status"] == "passed"
    assert checks["Paper size"]["status"] == "passed"


def test_one_page_over_the_limit_and_the_wrong_paper_fail_with_the_numbers(
    tmp_path: Path,
) -> None:
    roots = [
        _template(
            tmp_path / "templates",
            "lncs",
            "  page_limit: 4\n  references_count: true\n  paper_size: a4\n",
        )
    ]
    entry = tmp_path / "main.tex"
    entry.write_text("\\documentclass{llncs}\n")
    report = check_venue(
        "lncs", entry=entry, pdf=_pdf(tmp_path / "p.pdf", 5), page_count=5, roots=roots
    )
    checks = {check["rule"]: check for check in report["checks"]}
    assert checks["Page limit"] == {
        "rule": "Page limit",
        "status": "failed",
        "message": "5 pages; the limit is 4.",
    }
    assert checks["Paper size"]["status"] == "failed"
    assert "a4 is 595 x 842 pt" in checks["Paper size"]["message"]


def test_a_venue_no_plugin_ships_is_a_failed_check_not_a_guess(tmp_path: Path) -> None:
    report = check_venue(
        "nowhere",
        entry=tmp_path / "main.tex",
        pdf=tmp_path / "p.pdf",
        page_count=1,
        roots=[tmp_path],
    )
    assert report["status"] == "failed"
    assert "No plugin in this session has a template" in report["checks"][0]["message"]


# --- presentation --------------------------------------------------------------------------

_PDF = {"artifact_id": "art_pdf", "name": "main.tex.v2.pdf", "path": "/ws/paper/main.pdf"}


def _valid(view: dict) -> dict:
    return ToolPresentation.model_validate(view).model_dump(exclude_none=True)


def test_a_clean_build_names_the_pdf_and_its_pages() -> None:
    row = {
        "status": "complete",
        "entry": "paper/main.tex",
        "pdf": _PDF,
        "page_count": 8,
        "diagnostics": [],
        "counts": {},
        "network": "bundle_hosts",
    }
    view = _valid(latex_build_presentation({"entry": "paper/main.tex"}, row, None))
    assert view["action"] == "Build LaTeX paper" and view["summary"] == "8 pages"
    assert "status" not in view
    ids = [block["id"] for block in view["blocks"]]
    assert ids == ["entry", "pdf"]  # a bundle-only network is the quiet default


def test_a_failed_build_lists_each_problem_at_its_file_and_line() -> None:
    row = {
        "status": "failed",
        "entry": "paper/main.tex",
        "pdf": _PDF,
        "page_count": 9,
        "diagnostics": [
            {
                "kind": "overfull_box",
                "message": "Overfull \\hbox (3pt too wide)",
                "file": "main.tex",
                "line": 40,
                "detail": "3pt",
            },
            {
                "kind": "undefined_citation",
                "message": "Citation nokey is undefined.",
                "file": "sections/eval.tex",
                "line": 12,
                "page": 4,
                "detail": "nokey",
            },
        ],
        "counts": {"overfull_box": 1, "undefined_citation": 1},
        "venue": {
            "venue": "ieee-conference",
            "status": "failed",
            "checks": [
                {"rule": "Page limit", "status": "failed", "message": "9 pages; the limit is 8."}
            ],
        },
        "network": "unrestricted: no egress channel (sandbox_not_installed)",
        "error": "1 problem(s) make this build fail",
    }
    view = _valid(latex_build_presentation({"entry": "paper/main.tex"}, row, None))
    assert view["status"] == "failed"
    assert view["summary"] == "9 pages, 1 undefined citation, 1 overfull box"
    blocks = {block["id"]: block for block in view["blocks"]}
    # Breaking problems come first; each opens its file at its line.
    citation = blocks["problem-0"]
    assert citation["uri"] == "paper/sections/eval.tex" and citation["line"] == 12
    assert citation["label"].startswith("Undefined citation at sections/eval.tex:12")
    assert blocks["problem-1"]["severity"] == "warning"
    assert blocks["venue-0"]["type"] == "check" and blocks["venue-0"]["status"] == "failed"
    assert "sandbox_not_installed" in blocks["network"]["text"]


def test_a_build_without_a_pdf_says_so_and_why() -> None:
    row = {
        "status": "failed",
        "entry": "main.tex",
        "pdf": None,
        "page_count": 0,
        "diagnostics": [
            {
                "kind": "missing_package",
                "message": "siunitx.sty could not be downloaded, so the "
                "build needs network access the first time this package is used.",
                "file": "main.tex",
                "line": 2,
                "detail": "siunitx.sty",
            }
        ],
        "counts": {"missing_package": 1},
        "error": "LaTeX build produced no PDF",
    }
    view = _valid(latex_build_presentation({"entry": "main.tex"}, row, None))
    assert view["summary"] == "No PDF: 1 missing package"
    assert not any(block["id"] == "pdf" for block in view["blocks"])
    assert "needs network access" in view["blocks"][1]["label"]


def test_a_large_result_lists_a_bounded_number_and_counts_the_rest() -> None:
    diagnostics = [
        {
            "kind": "overfull_box",
            "message": f"Overfull \\hbox ({n}pt too wide) in a very long "
            "paragraph of the evaluation section that wraps",
            "file": f"sections/s{n}.tex",
            "line": n,
        }
        for n in range(MAX_LISTED + 25)
    ]
    row = {
        "status": "complete",
        "entry": "main.tex",
        "pdf": _PDF,
        "page_count": 30,
        "diagnostics": diagnostics,
        "counts": {"overfull_box": len(diagnostics)},
    }
    view = _valid(latex_build_presentation({"entry": "main.tex"}, row, None))
    problems = [block for block in view["blocks"] if block["id"].startswith("problem-")]
    assert len(problems) == MAX_LISTED
    more = next(block for block in view["blocks"] if block["id"] == "problems-more")
    assert more["text"] == "25 more are in Technical details."
    assert view["summary"] == f"30 pages, {len(diagnostics)} overfull boxes"


@pytest.mark.parametrize("row", [{}, None])
def test_a_missing_result_is_disclosed(row: dict | None) -> None:
    view = _valid(latex_build_presentation({"entry": "main.tex"}, row, None))
    assert view["status"] == "degraded" and view["summary"] == "Result unavailable"


def test_the_running_row_names_the_paper() -> None:
    view = _valid(latex_build_start({"entry": "paper/main.tex"}))
    assert view["status"] == "running" and view["blocks"][0]["label"] == "main.tex"


# --- the tool ------------------------------------------------------------------------------


def test_the_tool_reports_a_build_without_a_pdf_with_its_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from clio_agent.gact import latex_tools
    from clio_agent.gact.documents import comment_targets, renditions
    from clio_agent.gact.documents.latex.build import BuildResult
    from clio_agent.gact.documents.latex.diagnostics import Diagnostic
    from clio_agent.gact.documents.latex.rendition import LatexRenditionError

    monkeypatch.setattr(latex_tools, "_context", lambda: (object(), "sess_1", "ws_1"))
    version = SimpleNamespace(artifact_id="art_src", version=1, path="/ws/main.tex")
    seen: dict[str, str] = {}

    def register(app, sid, workspace_id, path, *, designation):  # noqa: ANN001, ANN202
        seen["designation"] = designation
        return SimpleNamespace(name="main.tex"), version

    monkeypatch.setattr(comment_targets, "register_target", register)
    built = BuildResult(
        entry="main.tex",
        ok=False,
        exit_code=1,
        sources={},
        read={},
        network="bundle_hosts",
        diagnostics=[
            Diagnostic(
                "shell_escape_unsupported",
                "minted needs shell escape, which builds never enable.",
                "main.tex",
                3,
                detail="minted",
            )
        ],
    )

    def render(*args):  # noqa: ANN002, ANN202
        try:
            raise LatexRenditionError("LaTeX build produced no PDF", built)
        except LatexRenditionError as exc:
            raise renditions.RenditionError(str(exc)) from exc

    monkeypatch.setattr(renditions, "render_pdf", render)
    row = latex_tools.latex_build("main.tex")
    assert seen["designation"] == "latex-source"
    assert row["status"] == "failed" and row["pdf"] is None
    assert row["counts"] == {"shell_escape_unsupported": 1}
    assert row["diagnostics"][0]["detail"] == "minted"


def test_the_tool_reads_a_successful_builds_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from clio_agent.gact import latex_tools
    from clio_agent.gact.documents import comment_targets, renditions

    monkeypatch.setattr(latex_tools, "_context", lambda: (object(), "sess_1", "ws_1"))
    version = SimpleNamespace(artifact_id="art_src", version=2, path="/ws/main.tex")
    monkeypatch.setattr(
        comment_targets,
        "register_target",
        lambda *a, **k: (SimpleNamespace(name="main.tex"), version),
    )
    pdf_version = SimpleNamespace(
        artifact_id="art_pdf",
        path="/ws/main.pdf",
        producer={
            "build_ok": True,
            "page_count": 3,
            "network": "bundle_hosts",
            "diagnostics": [{"kind": "overfull_box", "message": "m"}],
            "source_files": {"main.tex": "a", "refs.bib": "b"},
        },
    )
    monkeypatch.setattr(
        renditions,
        "render_pdf",
        lambda *a: SimpleNamespace(
            record=SimpleNamespace(name="main.tex.v2.pdf"),
            version=pdf_version,
            workspace_path=Path("/ws/p.pdf"),
        ),
    )
    row = latex_tools.latex_build("main.tex")
    assert row["status"] == "complete" and row["page_count"] == 3
    assert row["pdf"] == {"artifact_id": "art_pdf", "name": "main.tex.v2.pdf", "path": "/ws/p.pdf"}
    assert row["read"] == ["main.tex", "refs.bib"] and row["counts"] == {"overfull_box": 1}
    assert latex_tools.latex_build("notes.md")["reason"] == "latex_entry_not_tex"
