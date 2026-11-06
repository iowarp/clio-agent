"""The LaTeX workspace's routes: a paper's build, outline and click-through (LaTeX plan).

Live: it builds a real paper with the pinned Tectonic (``CLIO_DOCUMENT_TECTONIC`` or the
managed install) and skips otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests._tectonic import live_tectonic

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@live_tectonic
def test_the_report_and_click_through_both_ways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The routes are under test, not the fence: with the fence on, this host's egress goes
    # through a proxy the chokepoint does not chain to (LaTeX ledger L6), and the bundle is
    # unreachable. With it off, the build says its network was unrestricted.
    monkeypatch.setenv("CLIO_SANDBOX_ENABLED", "false")
    root = tmp_path / "ws"
    paper = root / "paper"
    (paper / "sections").mkdir(parents=True)
    (paper / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\input{sections/eval}\n\\end{document}\n"
    )
    (paper / "sections" / "eval.tex").write_text(
        "\\section{Scaling}\\label{sec:scaling}\nWe observe a speedup over the baseline.\n"
        "See~\\ref{nowhere}.\n"
    )
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        workspace_id = client.post(
            "/v1/workspaces", json={"name": "paper", "root_path": str(root)}
        ).json()["id"]
        session_id = client.post("/v1/sessions", json={"workspace_id": workspace_id}).json()["id"]
        target = client.post(
            f"/v1/workspaces/{workspace_id}/comment-targets",
            json={"path": "paper/main.tex", "session_id": session_id},
        )
        assert target.status_code == 200, target.text
        artifact_id = target.json()["artifact"]["artifact_id"]

        report = client.get(f"/v1/artifacts/{artifact_id}/latex", params={"session_id": session_id})
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["entry"] == "paper/main.tex" and body["folder"] == "paper"
        build = body["build"]
        # The undefined reference fails the build, though a PDF was made.
        assert build["pdf"], (build["network"], build.get("error"))
        assert build["network"].startswith("unrestricted")
        assert build["status"] == "failed" and build["pdf"]["artifact_id"]
        assert build["counts"] == {"undefined_reference": 1}
        assert body["files"] == ["main.tex", "sections/eval.tex"]
        assert body["texts"]["sections/eval.tex"].startswith("\\section{Scaling}")
        assert body["page_sizes"] == [[612.0, 792.0]]
        [section] = body["outline"]["nodes"]
        assert (section["title"], section["labels"]) == ("Scaling", ["sec:scaling"])
        place = section["places"][0]

        back = client.get(
            f"/v1/artifacts/{artifact_id}/latex/locate",
            params={"session_id": session_id, "file": "sections/eval.tex", "line": 2},
        ).json()
        assert back["direction"] == "source_to_pdf" and back["places"][0]["page"] == 1
        x, top, _width, height = back["places"][0]["rect"]
        forward = client.get(
            f"/v1/artifacts/{artifact_id}/latex/locate",
            params={"session_id": session_id, "page": 1, "x": x + 5, "y": top + height / 2},
        ).json()
        assert (forward["file"], forward["line"]) == ("sections/eval.tex", 2)
        assert forward["node"] == 'Section "Scaling" (sec:scaling)'
        assert place["page"] == 1

        other = client.get("/v1/artifacts/art_missing/latex", params={"session_id": session_id})
        assert other.status_code == 404
        (root / "notes.md").write_text("# notes")
        notes = client.post(
            f"/v1/workspaces/{workspace_id}/comment-targets",
            json={"path": "notes.md", "session_id": session_id},
        ).json()["artifact"]["artifact_id"]
        refused = client.get(f"/v1/artifacts/{notes}/latex", params={"session_id": session_id})
        assert refused.status_code == 422
        assert refused.json()["error"]["error"] == "latex_entry_not_tex"
