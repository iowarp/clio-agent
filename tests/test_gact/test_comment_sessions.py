"""Comment sessions on artifacts: labels, drafts, one turn per send, replies, file targets.

Covers comments ledger K11's shape on a Markdown file (one turn, labelled entries, no JSON),
K12 (a human note starts no turn), K15 (producer unknown), K22 (the cut and its session file),
K23 (drafts come back), K24 (labels continue per version), FB24 (no images to a model that
cannot take them), FB26 (replies, including through a steer), FB28 (a changed file becomes the
next version) and FB29 (on-demand names do not collide).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from clio_agent.gact.app import build_app
from clio_agent.gact.documents.anchors import ResolvedMark
from clio_agent.gact.documents.comment_replies import on_turn_finalized
from clio_agent.gact.documents.review_render import entry_block
from clio_agent.gact.documents.store import get_document_store
from tests.test_gact.test_document_artifacts import _pin, _workspace_session
from tests.test_gact.test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")

REPORT = "# Results\n\nThroughput rose by 40% in all configurations.\nThe 4 KB case is slower.\n"


def _quote(exact: str) -> dict[str, Any]:
    return {"profile": "text-quote", "exact": exact}


def _send(client: TestClient, session_id: str, pinned: dict, marks: list[dict], key: str) -> Any:
    return client.post(
        f"/v1/sessions/{session_id}/comment-sessions",
        json={
            "artifact_id": pinned["artifact_id"],
            "expected_version": pinned["version"],
            "expected_sha256": pinned["sha256"],
            "marks": marks,
            "idempotency_key": key,
        },
    )


def _wait(agent: FakeClioAgent, count: int) -> None:
    deadline = time.time() + 5
    while len(agent.calls) < count and time.time() < deadline:
        time.sleep(0.02)


def _app(tmp_path: Path, agent: FakeClioAgent) -> Any:
    return build_app(sessions_path=tmp_path / "sessions.json", agent=agent)


def test_one_send_is_one_turn_with_labelled_entries_and_no_json(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.md").write_text(REPORT)
    agent = FakeClioAgent()
    with TestClient(_app(tmp_path, agent)) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "report.md")
        sent = _send(
            client,
            session_id,
            pinned,
            [
                {"anchor": _quote("rose by 40%"), "comment": "Not all of them."},
                {"anchor": _quote("4 KB case"), "comment": "Name the block size."},
            ],
            "session-one-0001",
        )
        _wait(agent, 1)
        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
    assert sent.status_code == 202, sent.text
    body = sent.json()
    assert [t["label"] for t in body["threads"]] == ["S1", "S2"]
    assert {t["status"] for t in body["threads"]} == {"dispatched"}
    assert len(agent.calls) == 1
    question = agent.calls[0][0]
    assert "Review of report.md (artifact " in question
    assert "S1 (text)" in question and "S2 (text)" in question
    assert '  Selected: "rose by 40%"' in question
    assert "  Comment:  Not all of them." in question
    assert '"profile"' not in question and "{" not in question
    user = next(m for m in messages if m["role"] == "user")
    assert user["parts"][0]["text"].startswith("Comments on report.md (version 1):")
    labels = [p["metadata"]["label"] for p in user["parts"] if p["type"] == "artifact_review"]
    assert labels == ["S1", "S2"]


def test_labels_continue_per_version_and_drafts_come_back(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.md").write_text(REPORT)
    agent = FakeClioAgent()
    with TestClient(_app(tmp_path, agent)) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "report.md")
        marks = [{"anchor": _quote("Results"), "comment": f"c{i}"} for i in range(3)]
        assert _send(client, session_id, pinned, marks, "session-one-0002").status_code == 202
        drafts = client.put(
            f"/v1/artifacts/{pinned['artifact_id']}/comment-drafts",
            json={
                "session_id": session_id,
                "expected_version": pinned["version"],
                "expected_sha256": pinned["sha256"],
                "marks": [
                    {"anchor": _quote("4 KB case"), "comment": "draft one"},
                    {"anchor": _quote("Throughput"), "comment": "draft two"},
                ],
            },
        )
        _wait(agent, 1)
        listed = client.get(f"/v1/artifacts/{pinned['artifact_id']}/threads").json()
        resent = _send(
            client,
            session_id,
            pinned,
            [
                {
                    "label": "S4",
                    "anchor": _quote("4 KB case"),
                    "comment": "sent",
                    "send_to_agent": False,
                }
            ],
            "session-two-0002",
        )
    assert [d["label"] for d in drafts.json()["drafts"]] == ["S4", "S5"]
    by_label = {t["label"]: t for t in listed["threads"]}
    assert by_label["S4"]["status"] == "draft" and by_label["S4"]["text"] == "draft one"
    assert listed["next_label"] == "S6"
    sent = resent.json()["threads"][0]
    assert sent["label"] == "S4" and sent["id"] == by_label["S4"]["id"]
    assert sent["status"] == "human-note" and resent.status_code == 200
    assert len(agent.calls) == 1  # the human note started no turn


def test_a_replay_returns_the_same_threads_without_a_second_turn(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.md").write_text(REPORT)
    agent = FakeClioAgent()
    with TestClient(_app(tmp_path, agent)) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "report.md")
        marks = [{"anchor": _quote("Results"), "comment": "once"}]
        first = _send(client, session_id, pinned, marks, "session-replay-01")
        second = _send(client, session_id, pinned, marks, "session-replay-01")
        _wait(agent, 1)
    assert first.json()["threads"][0]["id"] == second.json()["threads"][0]["id"]
    assert len(agent.calls) == 1


def test_images_go_only_to_a_model_that_takes_them(tmp_path: Path, monkeypatch: Any) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    image = Image.new("RGB", (200, 100), "white")
    image.save(root / "figure.png")
    agent = FakeClioAgent()
    from clio_agent.gact import message_submission

    monkeypatch.setattr(message_submission, "_model_takes_images", lambda *a, **k: False)
    with TestClient(_app(tmp_path, agent)) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "figure.png")
        sent = _send(
            client,
            session_id,
            pinned,
            [{"anchor": {"profile": "region", "rect": [0.1, 0.1, 0.5, 0.5]}, "comment": "Legend"}],
            "session-images-01",
        )
        _wait(agent, 1)
        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
    assert sent.status_code == 202, sent.text
    question = agent.calls[0][0]
    assert "Producer unknown: no recorded tool call made this version." in question
    assert "Images were not sent: the selected model does not accept images." in question
    assert "S1 (region)" in question and "Box:      x 10% to 60%" in question
    user = next(m for m in messages if m["role"] == "user")
    assert not [p for p in user["parts"] if p["type"] == "image"]


def test_images_follow_the_overview_then_crops_order(tmp_path: Path, monkeypatch: Any) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    Image.new("RGB", (200, 100), "white").save(root / "figure.png")
    agent = FakeClioAgent()
    from clio_agent.gact import message_submission

    # One test decides both the renderer's choice and admission's check.
    monkeypatch.setattr(message_submission, "_model_takes_images", lambda *a, **k: True)
    with TestClient(_app(tmp_path, agent)) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "figure.png")
        region = {"profile": "region", "rect": [0.1, 0.1, 0.5, 0.5]}
        sent = _send(
            client,
            session_id,
            pinned,
            [{"anchor": region, "comment": "a"}, {"anchor": region, "comment": "b"}],
            "session-images-02",
        )
        _wait(agent, 1)
        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
    assert sent.status_code == 202, sent.text
    question = agent.calls[0][0]
    assert "Image 1 is the figure with S1 and S2 drawn on it." in question
    assert "S1 (region, attached as image 2)" in question
    assert "S2 (region, attached as image 3)" in question
    user = next(m for m in messages if m["role"] == "user")
    captions = [p["metadata"]["caption"] for p in user["parts"] if p["type"] == "image"]
    assert captions == [
        "the figure with S1 and S2 drawn on it",
        "S1 at twice its size",
        "S2 at twice its size",
    ]


def test_under_text_is_cut_with_the_rest_in_a_session_file(tmp_path: Path) -> None:
    mark = ResolvedMark(mode="region", under="x" * 5000, lines=[("Under", "x" * 5000)])
    block = entry_block("S1", mark, "Too dense.", image_number=2, overflow_dir=tmp_path)
    assert "S1 (region, attached as image 2)" in block
    assert "  Cut:      at 1,200 of 5,000 characters. The rest is in " in block
    assert (tmp_path / "S1-under.txt").read_text() == "x" * 5000


def test_the_turn_reply_lands_on_every_thread_including_through_a_steer(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.md").write_text(REPORT)
    agent = FakeClioAgent()
    app = _app(tmp_path, agent)
    with TestClient(app) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "report.md")
        body = _send(
            client,
            session_id,
            pinned,
            [
                {"anchor": _quote("Results"), "comment": "x"},
                {"anchor": _quote("4 KB"), "comment": "y"},
            ],
            "session-reply-001",
        ).json()
        message_id = body["message_id"]
        reply = SimpleNamespace(
            id="msg_reply",
            error_info=None,
            parts=[SimpleNamespace(type="text", text="Done: both fixed.", metadata={}, uri="")],
        )
        answered = on_turn_finalized(app, session_id, message_id, reply)
        other = on_turn_finalized(app, session_id, "turn_unrelated", reply)
        rows = get_document_store(app).session_threads(_ws, session_id)
    assert answered == 2 and other == 0
    assert {row.reply_text for row in rows if row.label in {"S1", "S2"}} == {"Done: both fixed."}


def test_a_steered_session_is_answered_by_the_turn_that_absorbed_it(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.md").write_text(REPORT)
    agent = FakeClioAgent()
    app = _app(tmp_path, agent)
    with TestClient(app) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "report.md")
        body = _send(
            client,
            session_id,
            pinned,
            [{"anchor": _quote("Results"), "comment": "x"}],
            "steer-0001",
        ).json()
        message_id = body["message_id"]
        history = SimpleNamespace(
            committed_notification_input=lambda sid, identity: SimpleNamespace(
                turn_id="turn_running"
            )
            if identity == message_id
            else None
        )
        app.state.task_history, real = history, app.state.task_history
        try:
            reply = SimpleNamespace(
                id="msg_running",
                error_info=None,
                parts=[SimpleNamespace(type="text", text="Handled.", metadata={}, uri="")],
            )
            assert on_turn_finalized(app, session_id, "turn_running", reply) == 1
        finally:
            app.state.task_history = real


def test_a_failed_turn_records_the_failure_not_a_reply(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.md").write_text(REPORT)
    agent = FakeClioAgent()
    app = _app(tmp_path, agent)
    with TestClient(app) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "report.md")
        body = _send(
            client,
            session_id,
            pinned,
            [{"anchor": _quote("Results"), "comment": "x"}],
            "fail-00001",
        ).json()
        failed = SimpleNamespace(
            id="msg_failed",
            error_info=SimpleNamespace(message="provider unavailable"),
            parts=[],
        )
        on_turn_finalized(app, session_id, body["message_id"], failed, failed=True)
        thread = get_document_store(app).get_review(body["threads"][0]["id"])
    assert thread is not None
    assert thread.reply_error == "provider unavailable" and not thread.reply_text


def test_a_file_changed_through_the_shell_becomes_the_linked_version(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    report = root / "report.md"
    report.write_text(REPORT)
    agent = FakeClioAgent()
    app = _app(tmp_path, agent)
    with TestClient(app) as client:
        _ws, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, "report.md")
        body = _send(
            client,
            session_id,
            pinned,
            [{"anchor": _quote("4 KB case"), "comment": "x"}],
            "edit-00001",
        ).json()
        time.sleep(0.01)
        report.write_text(REPORT.replace("4 KB case", "4 KB block size case"))
        reply = SimpleNamespace(
            id="msg_edit",
            error_info=None,
            parts=[SimpleNamespace(type="text", text="Renamed.", metadata={}, uri="")],
        )
        on_turn_finalized(app, session_id, body["message_id"], reply)
        thread = get_document_store(app).get_review(body["threads"][0]["id"])
        listed = client.get(
            f"/v1/artifacts/{pinned['artifact_id']}/threads", params={"session_id": session_id}
        ).json()
    assert thread is not None and thread.reply_version_artifact_id
    assert thread.reply_version_artifact_id != pinned["artifact_id"]
    assert listed["head"]["version"] == 2
    row = next(t for t in listed["threads"] if t["id"] == thread.id)
    assert row["anchor_state"] == "outdated"


def test_any_workspace_file_can_be_commented_without_basename_collisions(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "figs" / "a").mkdir(parents=True)
    (root / "figs" / "b").mkdir(parents=True)
    Image.new("RGB", (10, 10), "red").save(root / "figs" / "a" / "plot.png")
    Image.new("RGB", (10, 10), "blue").save(root / "figs" / "b" / "plot.png")
    agent = FakeClioAgent()
    with TestClient(_app(tmp_path, agent)) as client:
        workspace_id, session_id = _workspace_session(client, root)
        before = client.get(
            f"/v1/workspaces/{workspace_id}/comment-targets", params={"path": "figs/a/plot.png"}
        ).json()
        first = client.post(
            f"/v1/workspaces/{workspace_id}/comment-targets",
            json={"path": "figs/a/plot.png", "session_id": session_id},
        ).json()["artifact"]
        second = client.post(
            f"/v1/workspaces/{workspace_id}/comment-targets",
            json={"path": "figs/b/plot.png", "session_id": session_id},
        ).json()["artifact"]
        again = client.post(
            f"/v1/workspaces/{workspace_id}/comment-targets",
            json={"path": "figs/a/plot.png", "session_id": session_id},
        ).json()["artifact"]
        outside = client.post(
            f"/v1/workspaces/{workspace_id}/comment-targets",
            json={"path": "../escape.png", "session_id": session_id},
        )
    assert before == {"artifact": None}
    assert first["name"] == "plot.png" and first["producer"]["designation"] == "comment-target"
    assert second["name"] == "figs/b/plot.png"
    assert again["artifact_id"] == first["artifact_id"]
    assert outside.status_code in {403, 404}


def _png_b64(colour: str) -> str:
    import base64
    import io

    out = io.BytesIO()
    Image.new("RGB", (20, 10), colour).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def _attach(client: TestClient, session_id: str, session: dict, text: str = "") -> Any:
    parts = [{"type": "comment_session", "metadata": {"comment_session": session}}]
    if text:
        parts.insert(0, {"type": "text", "text": text})
    return client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"parts": parts, "client_message_id": f"attach-{time.time_ns()}"},
    )


def test_a_comment_on_an_answer_is_an_attachment_with_its_context(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    agent = FakeClioAgent()
    with TestClient(_app(tmp_path, agent)) as client:
        _ws, session_id = _workspace_session(client, root)
        posted = _attach(
            client,
            session_id,
            {
                "kind": "agent-answer",
                "marks": [
                    {
                        "label": "S1",
                        "anchor": {
                            "profile": "text-quote",
                            "exact": "rose by 40%",
                            "prefix": "throughput ",
                            "suffix": " overall",
                            "source": {"message_id": "msg_answer"},
                        },
                        "comment": "Which runs?",
                    }
                ],
            },
            text="Please check this.",
        )
        _wait(agent, 1)
    assert posted.status_code in {200, 202}, posted.text
    question = agent.calls[0][0]
    assert question.startswith("Please check this.")
    assert "Comment on your earlier answer (message msg_answer)." in question
    assert '  Selected: "rose by 40%"' in question and '  Before:   "throughput "' in question
    assert "  Comment:  Which runs?" in question and '"profile"' not in question


def test_an_a2ui_region_sends_readable_rows_and_no_json(tmp_path: Path, monkeypatch: Any) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    agent = FakeClioAgent()
    from clio_agent.gact import message_submission

    monkeypatch.setattr(message_submission, "_model_takes_images", lambda *a, **k: True)
    with TestClient(_app(tmp_path, agent)) as client:
        _ws, session_id = _workspace_session(client, root)
        posted = _attach(
            client,
            session_id,
            {
                "kind": "a2ui-surface",
                "title": "Depth vs magnitude",
                "marks": [
                    {
                        "label": "S1",
                        "anchor": {
                            "profile": "region",
                            "rect": [0.1, 0.2, 0.3, 0.4],
                            "source": {
                                "surface": "surf_1",
                                "revision": 3,
                                "component": "chart",
                                "data_reference": "results/quakes.csv",
                                "under": [
                                    "depth 10 km, magnitude 4.1",
                                    "depth 12 km, magnitude 4.4",
                                ],
                            },
                        },
                        "comment": "Outliers?",
                    }
                ],
                "images": [{"data": _png_b64("white")}, {"label": "S1", "data": _png_b64("red")}],
            },
        )
        _wait(agent, 1)
        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
    assert posted.status_code in {200, 202}, posted.text
    question = agent.calls[0][0]
    assert "Comment on Depth vs magnitude (A2UI surface surf_1, revision 3)." in question
    assert "Shown: component chart; data results/quakes.csv." in question
    assert "Image 1 is the view with S1 drawn on it." in question
    assert "S1 (region, attached as image 2)" in question
    assert "depth 10 km, magnitude 4.1" in question and "```json" not in question
    user = next(m for m in messages if m["role"] == "user")
    kinds = [p["type"] for p in user["parts"]]
    assert kinds == ["comment_session", "text", "image", "image"]
    assert user["parts"][1]["text"] == "Comments on Depth vs magnitude:\nS1: Outliers?"
    assert user["parts"][0]["metadata"]["comment_session"]["images"] == []


def test_a_malformed_comment_session_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    agent = FakeClioAgent()
    with TestClient(_app(tmp_path, agent)) as client:
        _ws, session_id = _workspace_session(client, root)
        posted = _attach(client, session_id, {"kind": "a2ui-surface", "marks": []})
    assert posted.status_code == 422
    assert posted.json()["error"]["error"] == "invalid_comment_session"
