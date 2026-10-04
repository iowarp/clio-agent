"""Browser QA against recorded capture bytes; this server performs no inference.

Run with an isolated CLIO_AGENT_HOME and CLIO_ALLOWED_ROOTS, then connect the UI
to port 18824. The final captured thought is presented as selectable text solely
to exercise the existing answer-selection entry point. Other transcript fields
and the prompt/token coordinates come from the recorded test fixture.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from clio_agent.gact.app import build_app
from clio_agent.gact.attention import routes
from clio_agent.gact.parts import Part
from tests.test_gact.test_attention._support import (
    FIXTURES,
    FixtureRenderer,
    fixture_flowcept,
    fixture_thought,
    fixture_transcript,
)
from tests.test_gact.test_attention.test_service_real_run import _call


def main() -> None:
    """Serve an explicitly isolated replay for interactive browser review."""
    root = Path(os.environ["CLIO_AGENT_HOME"]).resolve()
    if "beta3-attention-review" not in root.parts:
        raise ValueError("Use a dedicated beta3-attention-review state directory")
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root.parent)
    os.environ["CLIO_PROVENANCE_ATTENTION_FILES_DIR"] = str(FIXTURES)
    os.environ["CLIO_PROVENANCE_ATTENTION"] = "1"
    os.environ["CLIO_GACT_CORS_ORIGINS"] = "http://127.0.0.1:4394"
    app = build_app(agent=None, sessions_path=root / "sessions.json")
    title = "Recorded attention replay (no inference)"
    session = next((s for s in app.state.sessions.list() if s.title == title), None)
    if session is None:
        session = app.state.sessions.create(workspace_id="ws_default", title=title)
    messages = fixture_transcript()
    for message in messages:
        message.session_id = session.id
    # The compact test transcript omits invocation IDs. Give its recorded
    # call/result pairs explicit replay identities; production never infers
    # identity from display order.
    for index, part in enumerate(messages[-1].parts[:-1]):
        part.call_id = f"replay-call-{index // 2}"
    messages[-1].parts[-1] = Part(id="call_sel", type="text", text=fixture_thought())
    app.state.messages[session.id] = messages
    renderer = FixtureRenderer()
    flowcept = fixture_flowcept()
    routes._renderer = lambda identity: renderer
    routes.attention_capture_enabled = lambda: True
    routes.attention_store = lambda app: routes.AttentionStore(flowcept)
    routes.session_lm_calls = lambda app, sid: [replace(_call(), session_id=session.id)]
    # The fixture carries no live writer; route overrides above read the fixture.
    app.state.semantic_trace_backend = SimpleNamespace()
    print(f"Replay session: {session.id}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=18824)


if __name__ == "__main__":
    main()
