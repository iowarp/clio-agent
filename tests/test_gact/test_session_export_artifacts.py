"""An export carries the artifacts each session minted, as the live conversation has them."""

from __future__ import annotations

from clio_agent.gact.session_export_projection import with_minted_artifacts


def _entry(artifact_id: str, sid: str, name: str = "shot.png") -> dict:
    return {
        "artifact_id": artifact_id,
        "name": name,
        "workspace_id": "ws_1",
        "version": 1,
        "producer": {"session_id": sid, "designation": "browser_screenshot"},
        "evidence": {"size_bytes": 2048},
        "archive_path": f"effects/{artifact_id}/{name}",
    }


def test_a_tool_minted_image_joins_its_sessions_view_once() -> None:
    """A screenshot an answer shows by artifact:// link had no record offline, so the
    page said the image was unavailable although the archive carried the file."""
    views = {
        "sess_1": {"messages": [], "artifacts": [{"id": "artifact_known", "name": "a.csv"}]},
        "sess_2": {"messages": []},
    }
    manifest = {
        "artifacts": [
            _entry("artifact_shot", "sess_1"),
            _entry("artifact_known", "sess_1", "a.csv"),
            _entry("artifact_other", "sess_9"),  # a session the export does not carry
            _entry("artifact_child", "sess_2", "report.csv"),
        ]
    }
    out = with_minted_artifacts(views, manifest)
    assert [a["id"] for a in out["sess_1"]["artifacts"]] == ["artifact_known", "artifact_shot"]
    shot = out["sess_1"]["artifacts"][1]
    assert shot == {
        "id": "artifact_shot",
        "session_id": "sess_1",
        "workspace_id": "ws_1",
        "name": "shot.png",
        "media_type": "image/png",
        "uri": "artifact://artifact_shot",
        "version": 1,
        "size": 2048,
    }
    assert out["sess_2"]["artifacts"][0]["media_type"] == "text/csv"
