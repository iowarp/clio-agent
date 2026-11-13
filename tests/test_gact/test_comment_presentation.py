"""The transcript shows a file's comment threads as one reference to the commented version."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.protocol.v3.message import message_to_v3, part_to_v3_block


def _review(part_id: str, review_id: str, label: str, artifact_id: str = "art_v1") -> dict:
    return {
        "id": part_id,
        "type": "artifact_review",
        "review_id": review_id,
        "artifact_id": artifact_id,
        "artifact_version": 1,
        "artifact_sha256": "abc123",
        "review_text": "Fix this.",
        "anchor": {"profile": "text-quote", "exact": "x"},
        "metadata": {
            "artifact_name": "validation.json",
            "workspace_id": "ws_1",
            "native": False,
            "label": label,
            "session_key": "k",
        },
    }


def _user(*parts: dict) -> dict[str, Any]:
    text = {"id": "p0", "type": "text", "text": "Comments on validation.json (version 1): ..."}
    return {"id": "m1", "session_id": "s1", "role": "user", "parts": [text, *parts]}


def test_a_thread_is_a_reference_to_the_commented_version() -> None:
    block = part_to_v3_block(_review("p1", "rev_1", "S1"))
    assert block == {
        "id": "p1",
        "type": "context_reference",
        "ref_kind": "artifact",
        "ref_id": "art_v1",
        "label": "validation.json",
        "revision": "abc123",
        "media_type": "application/json",
        "navigation": {
            "comment_threads": ["rev_1"],
            "comment_labels": ["S1"],
            "workspace_id": "ws_1",
            "artifact_version": 1,
        },
    }


def test_one_send_names_each_commented_version_once() -> None:
    message = _user(
        _review("p1", "rev_1", "S1"),
        _review("p2", "rev_2", "S2"),
        _review("p3", "rev_3", "S1", artifact_id="art_v2"),
    )
    blocks = message_to_v3(message)["blocks"]
    assert [b["type"] for b in blocks] == ["text", "context_reference", "context_reference"]
    first, second = blocks[1], blocks[2]
    assert first["ref_id"] == "art_v1"
    assert first["navigation"]["comment_threads"] == ["rev_1", "rev_2"]
    assert first["navigation"]["comment_labels"] == ["S1", "S2"]
    assert second["ref_id"] == "art_v2"
    assert second["navigation"]["comment_threads"] == ["rev_3"]


def test_an_ordinary_reference_is_never_folded() -> None:
    reference = {
        "id": "p9",
        "type": "context_ref",
        "ref_kind": "artifact",
        "ref_id": "art_v1",
        "label": "validation.json",
    }
    blocks = message_to_v3(_user(_review("p1", "rev_1", "S1"), reference))["blocks"]
    assert [b["id"] for b in blocks] == ["p0", "p1", "p9"]
