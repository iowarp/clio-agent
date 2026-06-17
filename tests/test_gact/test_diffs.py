"""CLIO-BBBBBBBBBB21: two-phase edits."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    file_diffs: list = field(default_factory=list)


class _Agent:
    def __init__(self, diffs):
        self._pred = _Pred(file_diffs=diffs)

    def forward(self, question: str, session_id: str):
        return self._pred


def _client(tmp_path: Path, diffs) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent(diffs)))


def _turn(client: TestClient, sid: str) -> dict:
    from .conftest import complete_turn

    return complete_turn(client, sid, "propose an edit")


SAMPLE_DIFF = """--- a/main.go
+++ b/main.go
@@ -1,3 +1,3 @@
 package main
-func old() {}
+func newish() {}
"""


def test_assistant_emits_file_diff_part(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        diffs=[
            {"path": "main.go", "unified_diff": SAMPLE_DIFF},
        ],
    )
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    a = _turn(client, sid)
    parts = a["parts"]
    diff_parts = [p for p in parts if p["type"] == "file_diff"]
    assert len(diff_parts) == 1
    assert diff_parts[0]["path"] == "main.go"
    assert diff_parts[0]["status"] == "pending"
    assert "newish" in diff_parts[0]["unified_diff"]


def test_apply_flips_status_and_returns_paths(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        diffs=[
            {"path": "a.py", "unified_diff": SAMPLE_DIFF},
            {"path": "b.py", "unified_diff": SAMPLE_DIFF},
        ],
    )
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    _turn(client, sid)

    resp = client.post(f"/v1/sessions/{sid}/diffs/apply", json={"paths": ["a.py"]}).json()
    assert resp["applied"] == ["a.py"]

    # b.py still pending — apply-all picks it up.
    resp = client.post(f"/v1/sessions/{sid}/diffs/apply", json={}).json()
    assert resp["applied"] == ["b.py"]

    # Re-apply is a no-op (both now applied).
    resp = client.post(f"/v1/sessions/{sid}/diffs/apply", json={}).json()
    assert resp["applied"] == []


def test_reject_flips_status(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        diffs=[
            {"path": "a.py", "unified_diff": SAMPLE_DIFF},
        ],
    )
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    _turn(client, sid)
    resp = client.post(f"/v1/sessions/{sid}/diffs/reject", json={}).json()
    assert resp["rejected"] == ["a.py"]
    # Subsequent apply-all is a no-op because the row is rejected.
    resp = client.post(f"/v1/sessions/{sid}/diffs/apply", json={}).json()
    assert resp["applied"] == []


def test_apply_unknown_session_404s(tmp_path: Path) -> None:
    client = _client(tmp_path, diffs=[])
    resp = client.post("/v1/sessions/sess_nope/diffs/apply", json={})
    assert resp.status_code == 404
