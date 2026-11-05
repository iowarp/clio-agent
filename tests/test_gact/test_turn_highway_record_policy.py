"""A lost highway record is reported by the turn's publication, never fatal to it.

The routing decision and the turn's tool and model records are recorded behind the call
(``gact.highway_writer``). ``turn_finalize`` waits for them twice: before the answer
message (so the decision stays ahead of it on the wire) and as a publication step. Both
use one policy (``flush_session_reported``): an unconfirmed record is logged with the
typed reason ``highway_record_unconfirmed`` and the turn's transcript is still published.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import highway_writer
from clio_agent.gact.app import build_app
from clio_agent.gact.highway_writer import HIGHWAY_RECORD_UNCONFIRMED, HighwayRecordUnconfirmed
from tests.turn_signals import post_turn_and_wait

from .test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_an_unconfirmed_routing_record_does_not_abandon_the_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every barrier the turn's finalize waits on raises; the turn still answers ``idle``.

    SABOTAGE: call ``flush_session`` (not ``flush_session_reported``) for the routing
    decision in ``finalize_turn`` and the turn settles ``error`` with no answer.
    """
    barriers: list[str] = []
    real = highway_writer.flush_session

    def unconfirmed(
        app: Any, session_id: str, timeout: float | None = None, *, report: bool = True
    ) -> None:
        caller = sys._getframe(1).f_code
        if caller.co_name != "flush_session_reported" and not caller.co_filename.endswith(
            "turn_finalize.py"
        ):
            return real(app, session_id, timeout, report=report)  # ordering waits: not here
        barriers.append(session_id)
        raise HighwayRecordUnconfirmed(f"{HIGHWAY_RECORD_UNCONFIRMED}: simulated")

    monkeypatch.setattr(highway_writer, "flush_session", unconfirmed)
    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=FakeClioAgent(answer="routed answer", selected_expert="code_expert"),
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "policy"}).json()["id"]
        with caplog.at_level(logging.WARNING, logger="clio_agent.gact.highway_writer"):
            assert post_turn_and_wait(client, sid, {"text": "hi"}) == "idle"
        messages = client.get(f"/v1/sessions/{sid}/messages").json()
    answers = [m for m in messages["messages"] if m.get("role") == "assistant"]
    assert answers and answers[-1]["stop_reason"] == "end_turn"
    assert sid in barriers
    assert "step=routing_decision" in caplog.text
