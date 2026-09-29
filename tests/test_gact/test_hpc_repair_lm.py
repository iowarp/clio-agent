"""The bound session LM identity is used even with a conflicting boot default.

The out-of-loop ``CLIO_SUBMIT_REPAIR_ATTEMPTS`` forced-submit-repair mechanism
and the ``CLIO_EXTRACT_REPAIR_ATTEMPTS``/``CLIO_REPAIRER_MODEL`` schema-repair
mechanism this file used to exercise are both gone (#1331: "adopts the base
stack's repair-loop removal ... malformed/empty output routes through the
typed turn-level ladder"). What remains real and is still covered here: a
normal multi-turn ReAct loop, and an in-loop submit-schema rejection/retry,
both keep using the session's own bound model/endpoint identity rather than a
conflicting boot-default LM or a separate repair identity.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import dspy
import pytest
from dspy.dsp.utils.settings import main_thread_config

from clio_agent.config import LMProviderConfig, create_lm
from clio_agent.gact.agents import clio_react
from clio_agent.gact.agents.builders import _build_blueprint_dspy_module
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.app import build_app
from clio_agent.gact.types import AgentDef
from clio_agent.lm import hooked_lm as hooked_lm_mod
from clio_agent.lm.io_logging import LMOutputTruncatedError
from tests._scripted_engine import calls, scripted_lm
from tests.turn_signals import wait_for_terminal_status

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _WsSig(dspy.Signature):
    """Two required outputs; omitting either is a real submit rejection."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    workflow_state: dict[str, Any] = dspy.OutputField()


def _search(q: str) -> str:
    """Search."""
    return f"result:{q}"


def _build(max_iters: int) -> ClioReAct:
    return ClioReAct(_WsSig, tools=[dspy.Tool(_search, name="search")], max_iters=max_iters)


def test_multi_turn_tool_loop_keeps_session_model_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session's bound LM serves every step, never a conflicting boot default.

    A multi-step loop (non-submit tool calls, then a submit) runs with a boot-default
    LM installed on the main thread: every ``Request`` goes to the session LM's engine
    and names the session model; the boot LM is never called.
    """
    wrong, wrong_engine = scripted_lm([])
    monkeypatch.setitem(main_thread_config, "lm", wrong)
    session, engine = scripted_lm(
        [
            calls(("search", {"q": "x"})),
            calls(("search", {"q": "y"})),
            calls(("submit", {"answer": "FIXED", "workflow_state": {"ok": True}})),
        ]
    )
    with dspy.context(lm=session):
        result = _build(max_iters=3)(question="report")
    assert result.answer == "FIXED"
    assert len(engine.requests) == 3
    assert {r.model for r in engine.requests} == {session.model}
    assert wrong_engine.requests == []


def _chat_reply(index: int, call: dict[str, Any], *, stream: bool) -> tuple[str, bytes]:
    """One OpenAI Chat Completions reply carrying a single native tool call.

    ``(content_type, body)``: an SSE chunk stream when the request asked to stream
    (the loop streams every call), else one JSON completion.
    """
    tool_call = {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
    }
    usage = {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}
    head = {"id": f"repair-{index}", "created": 0, "model": "session-model"}
    if not stream:
        message = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
        choice = {"index": 0, "message": message, "finish_reason": "tool_calls"}
        body = {**head, "object": "chat.completion", "choices": [choice], "usage": usage}
        return "application/json", json.dumps(body).encode()
    chunk = {**head, "object": "chat.completion.chunk"}
    delta = {"role": "assistant", "tool_calls": [{"index": 0, **tool_call}]}
    events = [
        {**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {**chunk, "choices": [], "usage": usage},
    ]
    sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return "text/event-stream", sse.encode()


def test_real_http_submit_schema_retry_stays_on_session_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected-then-valid submit over a real HTTP endpoint stays on the session LM.

    Three native tool-call replies (search, a submit missing a required field, a fixed
    submit) from a local OpenAI-compatible server: the missing field is rejected
    IN-LOOP with the typed ``REACT_SUBMIT_INVALID_OUTPUT`` reason, the rejection goes
    back to the model as an error tool result, and every request names the session
    model and carries the tools natively.
    """
    reasons: list[str] = []

    def _sink(stage: str, **fields: Any) -> None:
        reasons.append(str(fields.get("duplicate_reason") or ""))

    monkeypatch.setattr("clio_agent.runtime.stream_audit.stream_audit", _sink)
    requests: list[dict[str, Any]] = []
    replies = [
        {"name": "search", "args": {"q": "x"}},
        {"name": "submit", "args": {"answer": "MISSING"}},
        {"name": "submit", "args": {"answer": "FIXED", "workflow_state": {"ok": True}}},
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            content_type, body = _chat_reply(
                len(requests), replies[len(requests) - 1], stream=bool(request.get("stream"))
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    monkeypatch.setattr("clio_agent.lm.io_logging._token_liveness_enabled", lambda: False)
    wrong, wrong_engine = scripted_lm([])
    monkeypatch.setitem(main_thread_config, "lm", wrong)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        lm = create_lm(LMProviderConfig(provider="vllm", model="session-model", api_base=endpoint))
        try:
            with dspy.context(lm=lm):
                result = _build(max_iters=3)(question="report")
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert result.answer == "FIXED"
    assert len(requests) == 3
    assert all(request["model"] == "session-model" for request in requests)
    assert all(
        {t["function"]["name"] for t in request["tools"]} == {"search", "submit"}
        for request in requests
    )
    rejection = [m for m in requests[2]["messages"] if m["role"] == "tool"][-1]
    assert "Missing required final output field(s): workflow_state" in str(rejection["content"])
    assert wrong_engine.requests == []
    assert clio_react.REACT_SUBMIT_INVALID_OUTPUT in reasons


def test_output_truncation_is_visible_terminal_state(tmp_path: Path) -> None:
    class TruncatedAgent:
        def forward(self, question: str, session_id: str) -> Any:
            raise LMOutputTruncatedError("openai/session-model")

    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=TruncatedAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "truncated"}).json()["id"]
        cursor = app.state.bus.latest_event_id(sid)
        response = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "long report"}]},
        )
        assert response.status_code == 200
        # Settled = the terminal status event (published after completion events are
        # persisted), not a fixed window a cold first turn can outlast.
        assert wait_for_terminal_status(app.state.bus, sid, after_event_id=cursor) == "error"
        completed = [
            event for event in app.state.bus._history[sid] if event.type == "message.completed"
        ]
        assert completed[-1].payload["error_info"]["details"]["reason"] == "output_truncated"
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        terminal = [m for m in messages if m.get("stop_reason") == "error"][-1]
        assert terminal["error_info"]["details"]["reason"] == "output_truncated"


def test_blueprint_factory_failure_preserves_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-invocation LM factory failure must escape without stale LM lookup."""
    failure = RuntimeError("credential materialization failed")

    def fail_factory(config: LMProviderConfig) -> Any:
        raise failure

    monkeypatch.setattr(hooked_lm_mod, "create_hooked_lm", fail_factory)
    base = type(
        "Base",
        (),
        {
            "_provider_config": LMProviderConfig(
                provider="vllm",
                model="session-model",
                api_base="http://127.0.0.1:1/v1",
            )
        },
    )()
    module = _build_blueprint_dspy_module(
        base,
        AgentDef(
            id="factory-failure",
            title="Factory failure",
            source="expert_pack",
            module={"kind": "predict"},
            structured_outputs={"workflow_state": True},
        ),
    )

    with pytest.raises(RuntimeError) as captured:
        module(question="report", session_id="factory-failure")

    assert captured.value is failure
