"""Private browser review service: real GACT routes and bounded clio-core storage.

Run with ``uv run python -m tests.test_gact.response_rating_review_server``.
The synthetic conversation never invokes an LM. Shutdown through the private
test endpoint releases the native daemon and removes this run's temporary state.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import uvicorn


def main() -> None:
    """Start an isolated real store and the browser review's HTTP service."""
    with tempfile.TemporaryDirectory(
        prefix=f"clio-agent-cte-{os.getpid()}-", dir=os.environ["TEMP"]
    ) as directory:
        root = Path(directory).resolve()
        os.environ["CLIO_AGENT_HOME"] = str(root / "agent")
        os.environ["CLIO_GACT_CORS_ORIGINS"] = "http://127.0.0.1:5214"
        os.environ["CLIO_RUNTIME_AUTOSTART"] = "0"
        os.environ["CLIO_TEST_CTE_FILE_TIER_CAPACITY"] = "32MB"
        from tests._cte_isolation import isolate_cte_env, reap_private_daemon

        isolation = isolate_cte_env(root, os.environ)
        from clio_agent.arc.memory import ARCMemory
        from clio_agent.arc.response_feedback import ResponseFeedbackLedger
        from clio_agent.arc.storage import make_arc_store, release_runtime_client
        from clio_agent.gact.app import build_app
        from clio_agent.gact.types import Message, Part

        try:
            store = make_arc_store(namespace="response-rating-browser-review")
            arc = ARCMemory(data_dir=str(root / "arc"), store=store)
            app = build_app(sessions_path=root / "sessions.json", arc=arc)
            app.state.workspaces.update("ws_default", root_path=str(root))
            session = app.state.sessions.create(
                workspace_id="ws_default", title="Response rating review"
            )
            now = "2026-10-10T00:00:00Z"
            prompt = Message(
                id="question",
                session_id=session.id,
                turn_id="question",
                role="user",
                created_at=now,
                updated_at=now,
                parts=[Part(id="question-text", type="text", text="What is six times seven?")],
            )
            answer = Message(
                id="answer",
                session_id=session.id,
                turn_id="question",
                role="assistant",
                created_at=now,
                updated_at=now,
                stop_reason="end_turn",
                parts=[Part(id="answer-text", type="text", text="Six times seven is 42.")],
            )
            app.state.messages[session.id] = [prompt, answer]
            app.state.message_store.replace_session(session.id, [prompt, answer])
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=18817, log_level="warning")
            )

            @app.get("/__test/session")
            async def review_session() -> dict[str, str]:
                """Identify the synthetic conversation and real store."""
                return {"session_id": session.id, "store_type": type(store).__name__}

            @app.post("/__test/reopen-ledger")
            async def reopen_ledger() -> dict[str, bool]:
                """Drop the ledger object so the next GET must read clio-core."""
                arc.response_feedback = ResponseFeedbackLedger(store)
                return {"ok": True}

            @app.post("/__test/shutdown")
            async def shutdown() -> dict[str, bool]:
                """Stop cooperatively so native resources and temporary files are released."""
                server.should_exit = True
                return {"ok": True}

            print(json.dumps({"temporary_state": str(root), "port": 18817}), flush=True)
            server.run()
        finally:
            release_runtime_client()
            reap_private_daemon(isolation.state_dir)


if __name__ == "__main__":
    main()
