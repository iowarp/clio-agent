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
from dspy.utils import DummyLM

from clio_agent.config import LMProviderConfig, create_lm
from clio_agent.gact.agents import reactv2
from clio_agent.gact.agents.builders import _build_blueprint_dspy_module
from clio_agent.gact.app import build_app
from clio_agent.gact.types import AgentDef
from clio_agent.lm import hooked_lm as hooked_lm_mod
from clio_agent.lm.io_logging import LMOutputTruncatedError
from tests.test_gact.test_reactv2_repair import _build, _WsSig

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _non_submit_response() -> dict[str, Any]:
    """A DummyLM turn that calls a non-submit tool (forces a repair re-ask).

    Reactv2_repair.py dropped its own copy of this helper (#901 S4 cleanup,
    88e3d07a) once it no longer needed it; this file still does, so it is kept
    local here rather than reintroduced as shared dead weight there.
    """
    return {
        "next_thought": "t",
        "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "x"}}]},
    }


def test_multi_turn_tool_loop_keeps_session_model_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session's bound LM identity survives a normal multi-turn tool loop.

    Replaces ``test_forced_submit_repair_keeps_session_model_and_endpoint``
    (#1331): the out-of-loop ``CLIO_SUBMIT_REPAIR_ATTEMPTS`` forced-submit-repair
    mechanism this test used to drive has no implementation in ``src/`` anymore
    -- the base stack dropped that repair loop, and #1331 adopts the removal
    ("malformed/empty output routes through the typed turn-level ladder").
    What is still real: a multi-turn ReAct loop (non-submit tool calls, then a
    submit) must keep using the SESSION's bound model/endpoint even with a
    conflicting boot-default LM installed on the main thread -- so this fixture
    is kept, just driven through the ordinary in-loop iteration budget
    (``max_iters``) instead of a separate repair-attempt budget.
    """
    wrong = DummyLM([])
    wrong.model = "openai/Qwen/Qwen2.5-0.5B-Instruct"
    monkeypatch.setitem(main_thread_config, "lm", wrong)
    session = DummyLM(
        [
            _non_submit_response(),
            _non_submit_response(),
            {
                "next_thought": "repaired",
                "tool_calls": {
                    "tool_calls": [
                        {
                            "name": "submit",
                            "args": {"answer": "FIXED", "workflow_state": {"ok": True}},
                        }
                    ]
                },
            },
        ]
    )
    session.model = "openai/granite-4.2-30b"
    session.kwargs["api_base"] = "http://localhost:8000/v1"
    agent = _build(_WsSig, max_iters=3)
    with dspy.context(lm=session, adapter=dspy.ChatAdapter()):
        result = agent(question="report")
    assert result.answer == "FIXED"
    assert len(session.history) == 3
    assert not wrong.history
    assert session.model == "openai/granite-4.2-30b"
    assert session.kwargs["api_base"] == "http://localhost:8000/v1"


def test_real_http_submit_schema_retry_stays_on_session_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive a rejected-then-valid submit through DSPy and the typed submit ladder.

    Replaces ``test_real_http_retained_history_repair_stays_on_session_endpoint``
    (#1331): that test drove the same three-turn shape (search, a submit missing a
    required field, then a fixed submit) through the ``CLIO_SUBMIT_REPAIR_ATTEMPTS``
    out-of-loop repair mechanism, which has no implementation in ``src/``. The
    fixture still exercises a real path -- a submit call missing a required
    structured field is rejected IN-LOOP with the typed
    ``REACT_SUBMIT_INVALID_OUTPUT`` reason (``reactv2._execute_tool_calls``) and
    the model gets to retry within the same session -- so it is kept, driven
    through the ordinary in-loop iteration budget, with an explicit capture of
    the typed reason proving the rejection routed through that ladder rather
    than being silently patched.
    """
    reasons: list[str] = []

    def _sink(stage: str, **fields: Any) -> None:
        reasons.append(str(fields.get("duplicate_reason") or ""))

    monkeypatch.setattr("clio_agent.runtime.stream_audit.stream_audit", _sink)
    requests: list[dict[str, Any]] = []
    replies = [
        "[[ ## next_thought ## ]]\nsearch\n\n[[ ## tool_calls ## ]]\n"
        '{"tool_calls":[{"name":"search","args":{"q":"x"}}]}\n\n[[ ## completed ## ]]',
        "[[ ## next_thought ## ]]\nbad submit\n\n[[ ## tool_calls ## ]]\n"
        '{"tool_calls":[{"name":"submit","args":{"answer":"MISSING"}}]}\n\n[[ ## completed ## ]]',
        "[[ ## next_thought ## ]]\nfixed\n\n[[ ## tool_calls ## ]]\n"
        '{"tool_calls":[{"name":"submit","args":{"answer":"FIXED",'
        '"workflow_state":{"ok":true}}}]}\n\n[[ ## completed ## ]]',
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            content = replies[len(requests) - 1]
            body = json.dumps(
                {
                    "id": f"repair-{len(requests)}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "session-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    monkeypatch.setattr("clio_agent.lm.io_logging._token_liveness_enabled", lambda: False)
    wrong = DummyLM([])
    wrong.model = "openai/boot-model"
    monkeypatch.setitem(main_thread_config, "lm", wrong)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        lm = create_lm(LMProviderConfig(provider="vllm", model="session-model", api_base=endpoint))
        try:
            with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
                result = _build(_WsSig, max_iters=3)(question="report")
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert result.answer == "FIXED"
    assert len(requests) == 3
    assert all(request["model"] == "session-model" for request in requests)
    assert not wrong.history
    assert reactv2.REACT_SUBMIT_INVALID_OUTPUT in reasons


def test_output_truncation_is_visible_terminal_state(tmp_path: Path) -> None:
    class TruncatedAgent:
        def forward(self, question: str, session_id: str) -> Any:
            raise LMOutputTruncatedError("openai/session-model")

    import time

    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=TruncatedAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "truncated"}).json()["id"]
        response = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "long report"}]},
        )
        assert response.status_code == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if client.get(f"/v1/sessions/{sid}").json()["status"] == "error":
                break
            time.sleep(0.05)
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
