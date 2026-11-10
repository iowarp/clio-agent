"""Context-frame sizes for @-referenced context (gact-tui#739)."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.context_reference_delivery import context_reference_frame_items
from clio_agent.gact.runtime.constants import _CTX_MAX_BYTES
from clio_agent.gact.types import Message, Part


def _ref(kind: str, ref_id: str, label: str, delivery: dict[str, Any], **extra: Any) -> Part:
    return Part(
        id=f"part_{ref_id}",
        type="context_ref",
        ref_kind=kind,
        ref_id=ref_id,
        label=label,
        revision="sha256:abc",
        metadata={
            "context_reference": {
                "kind": kind,
                "ref_id": ref_id,
                "workspace_id": "ws_default",
                "resolved_revision": "sha256:abc",
                "delivery": delivery,
                **extra,
            }
        },
    )


def _rows(*parts: Part) -> list[dict[str, Any]]:
    message = Message(
        id="msg_user_1",
        session_id="sess_1",
        role="user",
        created_at="2026-11-10T00:00:00+00:00",
        updated_at="2026-11-10T00:00:00+00:00",
        parts=[*parts, Part(id="p_text", type="text", text="look at this")],
    )
    return context_reference_frame_items(message)


def test_binary_workspace_file_counts_its_path_block_not_its_bytes() -> None:
    (row,) = _rows(
        _ref(
            "workspace_file",
            "sample.bin",
            "sample.bin",
            {
                "mode": "workspace_path",
                "revision_basis": "sha256",
                "size_bytes": 200_000,
                "workspace_path": "sample.bin",
                "agent_host_path": "/ws/sample.bin",
                "sha256": "abc",
            },
            media_type="application/octet-stream",
        )
    )

    assert 50 < row["tokens_estimated"] < 400
    assert row["display_path"] == "sample.bin"


def test_inline_text_counts_capped_bytes_plus_header() -> None:
    small, large = _rows(
        _ref(
            "workspace_file",
            "notes.txt",
            "notes.txt",
            {"mode": "inline_text", "size_bytes": 2_000, "workspace_path": "notes.txt"},
            media_type="text/plain",
        ),
        _ref(
            "workspace_file",
            "big.log",
            "big.log",
            {"mode": "inline_text", "size_bytes": 10 * _CTX_MAX_BYTES, "workspace_path": "big.log"},
            media_type="text/plain",
        ),
    )

    assert 500 <= small["tokens_estimated"] < 540
    assert _CTX_MAX_BYTES // 4 <= large["tokens_estimated"] < _CTX_MAX_BYTES // 4 + 60


def test_metadata_only_artifact_counts_its_metadata_block() -> None:
    (row,) = _rows(
        _ref(
            "artifact",
            "art_1",
            "plot.png",
            {"mode": "metadata", "size_bytes": 5_000_000, "sha256": "abc"},
            media_type="image/png",
        )
    )

    assert 10 < row["tokens_estimated"] < 100
    assert row["display_path"] == "plot.png"


def test_summary_reference_counts_its_delivered_summary() -> None:
    summary = {"kind": "context_frame", "snapshot": {"items": ["x" * 3_960]}}
    (row,) = _rows(
        _ref(
            "context_frame",
            "ctx_1",
            "Context for chat",
            {"mode": "bounded_summary", "summary": summary},
            media_type="application/vnd.clio.context-frame+json",
        )
    )

    assert 1_000 <= row["tokens_estimated"] < 1_060
    assert row["display_path"] == "Context for chat"
