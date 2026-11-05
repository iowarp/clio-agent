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


def test_a_build_that_made_a_paper_leads_with_its_title() -> None:
    row = {
        "status": "complete",
        "entry": "paper-bb/main.tex",
        "pdf": _PDF,
        "paper": {
            "artifact_id": "art_paper",
            "name": "paper-bb",
            "version": 3,
            "title": "Burst Buffers and Checkpoint Performance",
            "document_type": "Paper",
            "venue": "IEEE conference",
            "members": ["main.tex", "sections/intro.tex", "refs.bib"],
            "untracked": [],
        },
        "page_count": 4,
        "diagnostics": [],
        "counts": {},
        "network": "bundle_hosts",
    }
    view = _valid(latex_build_presentation({"entry": "paper-bb/main.tex"}, row, None))
    blocks = {block["id"]: block for block in view["blocks"]}
    assert list(blocks) == ["entry", "paper", "paper-detail"]  # the paper replaces the PDF link
    assert blocks["paper"]["target"] == "artifact" and blocks["paper"]["uri"] == "art_paper"
    assert blocks["paper"]["label"] == "Burst Buffers and Checkpoint Performance"
    assert blocks["paper-detail"]["text"] == (
        "Paper, IEEE conference, version 3, built from 3 source files"
    )


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


def test_a_build_in_a_turn_ends_the_reply_with_the_papers_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The card is the entry's source version: it opens as the LaTeX workspace.
    from types import SimpleNamespace

    from clio_agent.gact import context, latex_tools
    from clio_agent.gact.artifacts import minting
    from clio_agent.gact.documents import comment_targets
    from clio_agent.gact.documents.latex import paper

    app = object()
    monkeypatch.setattr(latex_tools, "_context", lambda: (app, "sess_1", "ws_1"))
    version = SimpleNamespace(artifact_id="art_src", version=2, path="/ws/paper/main.tex")
    monkeypatch.setattr(
        comment_targets,
        "register_target",
        lambda *a, **k: (SimpleNamespace(name="main.tex"), version),
    )
    monkeypatch.setattr(paper, "build_row", lambda *a, **k: {"status": "complete"})
    offered: list[dict] = []
    monkeypatch.setattr(
        minting, "_record_turn_artifact", lambda *a, **k: offered.append({"args": a, **k})
    )
    monkeypatch.setattr(context, "active_turn_id", lambda: "")
    latex_tools.latex_build("paper/main.tex")
    assert offered == []  # outside a turn there is no reply to end
    monkeypatch.setattr(context, "active_turn_id", lambda: "turn_7")
    latex_tools.latex_build("paper/main.tex")
    [card] = offered
    assert card["args"] == (app, "sess_1")
    assert card["version"] is version and card["turn_id"] == "turn_7"
    assert card["name"] == "main.tex" and card["purpose"] == "deliverable"


def test_the_outline_lists_each_part_at_its_source_line_and_page() -> None:
    from clio_agent.gact.agents.native_presenters_latex import latex_outline_presentation

    row = {
        "status": "complete",
        "entry": "paper/main.tex",
        "synctex": True,
        "counts": {"section": 2, "figure": 1},
        "unresolved_citations": ["missing2020"],
        "nodes": [
            {
                "kind": "section",
                "file": "sections/eval.tex",
                "start_line": 1,
                "end_line": 9,
                "title": "Evaluation",
                "labels": ["sec:eval"],
                "level": 2,
                "places": [{"page": 3, "rect": [72, 90, 400, 300]}],
                "children": [
                    {
                        "kind": "figure",
                        "file": "sections/eval.tex",
                        "start_line": 4,
                        "end_line": 7,
                        "title": "Throughput by block size",
                        "labels": ["fig:tput"],
                        "places": [{"page": 3, "rect": [72, 120, 200, 140]}],
                    }
                ],
            },
            {
                "kind": "section",
                "file": "sections/end.tex",
                "start_line": 1,
                "end_line": 2,
                "title": "Conclusion",
                "level": 2,
            },
        ],
    }
    view = _valid(latex_outline_presentation({"entry": "paper/main.tex"}, row, None))
    assert view["summary"] == "2 sections, 1 figure"
    blocks = {block["id"]: block for block in view["blocks"]}
    assert blocks["node-0"]["uri"] == "paper/sections/eval.tex" and blocks["node-0"]["line"] == 1
    assert blocks["node-0"]["label"] == "Evaluation (sec:eval, page 3) at sections/eval.tex:1"
    assert blocks["node-1"]["label"].startswith("  Throughput by block size (figure, fig:tput")
    assert blocks["unresolved"]["text"] == "missing2020"


def test_locate_says_where_a_point_is_in_the_source_and_back() -> None:
    from clio_agent.gact.agents.native_presenters_latex import latex_locate_presentation

    forward = {
        "status": "complete",
        "entry": "main.tex",
        "direction": "pdf_to_source",
        "page": 6,
        "x": 200,
        "y": 300,
        "file": "sections/eval.tex",
        "line": 112,
        "node": 'Section "Scaling" (sec:scaling)',
    }
    view = _valid(latex_locate_presentation({}, forward, None))
    assert view["summary"] == "Page 6 is sections/eval.tex:112"
    assert view["blocks"][1]["line"] == 112
    back = {
        "status": "complete",
        "entry": "main.tex",
        "direction": "source_to_pdf",
        "file": "sections/eval.tex",
        "line": 112,
        "node": "",
        "places": [{"page": 6, "rect": [72.0, 300.5, 410.2, 24.0]}],
    }
    view = _valid(latex_locate_presentation({}, back, None))
    assert view["summary"] == "sections/eval.tex:112 is on page 6"
    assert view["blocks"][2]["text"] == "Box at (72, 300), 410 x 24 pt"
    failed = {"status": "failed", "entry": "main.tex", "error": "this build has no SyncTeX index"}
    assert _valid(latex_locate_presentation({}, failed, None))["status"] == "failed"
