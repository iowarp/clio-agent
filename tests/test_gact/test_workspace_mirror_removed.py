"""#771: the reader-less per-workspace session/message mirror is DELETED.

The mirror was write-only (zero readers in ``src/`` or gact-tui). Its removal means
a workspace-owned session and its message ledger must write NOTHING under the
workspace storage root — while ``resolve_workspace_storage_root`` still resolves the
``storage_root`` wire field the TUI displays.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact import session_store
from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part, Tokens


def _text_message(session_id: str, text: str) -> Message:
    now = datetime.now(timezone.utc).isoformat()
    return Message(
        id="m1",
        session_id=session_id,
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id="p1", type="text", text=text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )


def test_workspace_owned_session_writes_nothing_under_storage_root(tmp_path: Path) -> None:
    """A workspace-owned session + every message-ledger write seam leave the
    workspace storage root empty (the mirror is gone)."""

    ws_root = tmp_path / "ws-store"
    sessions_path = tmp_path / "server" / "sessions.json"
    with TestClient(build_app(sessions_path=sessions_path)) as client:
        ws = client.post(
            "/v1/workspaces",
            json={
                "name": "w",
                "root_path": str(tmp_path / "proj"),
                "storage_root": str(ws_root),
            },
        ).json()
        wid = ws["id"]
        # The wire field still resolves — only the on-disk mirror was removed.
        assert ws["storage_root"] == str(ws_root)

        sid = client.post(
            "/v1/sessions", json={"title": "t", "workspace_id": wid}
        ).json()["id"]

        # Drive every message-ledger write seam (append/replace/delete) directly.
        message = _text_message(sid, "hello")
        session_store._append_session_message(client.app, sid, message)
        session_store._replace_session_messages(client.app, sid, [message])
        session_store._delete_session_messages(client.app, sid)

    # Nothing — no sessions.json, no messages/ ledger — under the workspace root.
    written = list(ws_root.rglob("*")) if ws_root.exists() else []
    assert written == [], f"workspace storage root was written to: {written}"
