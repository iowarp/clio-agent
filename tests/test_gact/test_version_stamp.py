"""#771 truth pass: persisted version stamps reflect the installed build.

Both the core agent (``agent.py::_store_conversation``) and the gact
compaction route (``routes/sessions.py`` ``POST /v1/sessions/{sid}/compact``)
used to hard-code ``clio_agent_version="0.2.0"`` into ARC conversation
metadata while the package was on 0.5.x. These tests drive the two real
store paths against a recording ARC fake and assert the *persisted*
``metadata["clio_agent_version"]`` equals the installed distribution
version — re-hardcoding a literal at either call site fails them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from clio_agent.arc.schema import Conversation as ARCConversation
from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part, Tokens

INSTALLED_VERSION = metadata.version("clio-agent")


class _RecordingArc:
    """ARC fake that records every stored conversation (fresh session)."""

    def __init__(self) -> None:
        self.stored: list[ARCConversation] = []

    def get_conversation(self, session_id: str) -> ARCConversation | None:
        return None

    def store_conversation(self, conversation: ARCConversation) -> None:
        self.stored.append(conversation)


class _StubCompactAgent:
    """Minimal agent for the compact route: fixed summary + recording ARC."""

    def __init__(self) -> None:
        self.arc = _RecordingArc()

    def _run_chat_agent(self, question: str, session_id: str) -> str:
        return "stub compact summary"


def test_agent_store_conversation_stamps_installed_version() -> None:
    """The core agent persists the installed version, not a frozen literal."""

    from clio_agent.agent import ClioAgent

    holder = SimpleNamespace(arc=_RecordingArc())
    ClioAgent._store_conversation(holder, "what is in run.h5?", "one dataset.", "sess-vstamp")

    assert len(holder.arc.stored) == 1
    stamped = holder.arc.stored[0].metadata["clio_agent_version"]
    assert stamped == INSTALLED_VERSION
    assert stamped != "0.2.0"


def test_gact_compact_route_stamps_installed_version(tmp_path: Path) -> None:
    """The gact compact route persists the installed version in ARC metadata."""

    agent = _StubCompactAgent()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as c:
        sid = c.post("/v1/sessions", json={"title": "version stamp"}).json()["id"]
        now = datetime.now(timezone.utc).isoformat()
        message = Message(
            id="msg_seed",
            session_id=sid,
            role="user",
            created_at=now,
            updated_at=now,
            parts=[Part(id="part_seed", type="text", text="dataset /a/b shape=(4,)")],
            tokens=Tokens(),
            stop_reason="end_turn",
        )
        c.app.state.messages[sid] = [message]
        c.app.state.message_store.replace_session(sid, [message])

        resp = c.post(f"/v1/sessions/{sid}/compact", json={})

        assert resp.status_code == 200, resp.text
        assert len(agent.arc.stored) == 1
        stamped = agent.arc.stored[0].metadata["clio_agent_version"]
        assert stamped == INSTALLED_VERSION
        assert stamped != "0.2.0"


def test_version_helpers_report_installed_distribution() -> None:
    """Both stamping helpers resolve to the installed distribution version."""

    from clio_agent.agent import _clio_agent_version
    from clio_agent.gact.runtime.constants import _installed_clio_agent_version

    assert _clio_agent_version() == INSTALLED_VERSION
    assert _installed_clio_agent_version() == INSTALLED_VERSION
    assert INSTALLED_VERSION != "0.2.0"
