"""#771 truth pass: persisted version stamps reflect the installed build.

The gact compaction route (``routes/sessions.py`` ``POST
/v1/sessions/{sid}/compact``) used to hard-code
``clio_agent_version="0.2.0"`` into ARC conversation metadata while the
package was on 0.5.x. This test drives the real store path against a
recording ARC fake and asserts the *persisted*
``metadata["clio_agent_version"]`` equals the installed distribution
version — re-hardcoding a literal at the call site fails it.

Note (#948 S4b): the core agent's ``ClioAgent._store_conversation`` (and its
``_clio_agent_version`` helper) were deleted with the Tier-1 planner; the gact
route's ``_installed_clio_agent_version`` is now the sole stamping helper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

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
    """The gact stamping helper resolves to the installed distribution version.

    #948 S4b: the core ``clio_agent.agent._clio_agent_version`` helper was deleted
    with the planner; ``_installed_clio_agent_version`` is the surviving stamp.
    """

    from clio_agent.gact.runtime.constants import _installed_clio_agent_version

    assert _installed_clio_agent_version() == INSTALLED_VERSION
    assert INSTALLED_VERSION != "0.2.0"


def test_backend_version_appends_git_sha_in_checkout() -> None:
    """``GACT_BACKEND_VERSION`` carries the HEAD SHA (``semver+sha``) in a checkout.

    Outside a git repo the helper degrades to the plain semver; either shape is
    accepted, but the SHA suffix, when present, must be a short hex of the base.
    """

    from clio_agent.gact.runtime.constants import _backend_version, _git_head_sha

    version = _backend_version()
    sha = _git_head_sha()
    if sha is None:
        assert version == INSTALLED_VERSION
    else:
        assert version == f"{INSTALLED_VERSION}+{sha}"
        assert 0 < len(sha) <= 8
        assert all(c in "0123456789abcdef" for c in sha)
