"""A repair must use the bound session LM even with a conflicting boot default."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import dspy
import pytest
from dspy.dsp.utils.settings import main_thread_config
from dspy.utils import DummyLM

from clio_agent import conf
from clio_agent.config import LMProviderConfig, create_lm
from clio_agent.gact.agents.builders import _build_blueprint_dspy_module
from clio_agent.gact.app import build_app
from clio_agent.gact.hooks.wire import HookOutcome
from clio_agent.gact.types import AgentDef
from clio_agent.lm import hooked_lm as hooked_lm_mod
from clio_agent.lm.io_logging import LMOutputTruncatedError
from tests.test_gact.test_reactv2_repair import _build, _non_submit_response, _WsSig

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_forced_submit_repair_keeps_session_model_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "2")
    conf.reload()
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
    agent = _build(_WsSig, max_iters=1)
    with dspy.context(lm=session, adapter=dspy.ChatAdapter()):
        result = agent(question="report")
    assert result.answer == "FIXED"
    assert len(session.history) == 3
    assert not wrong.history
    assert session.model == "openai/granite-4.2-30b"
    assert session.kwargs["api_base"] == "http://localhost:8000/v1"


def test_real_http_retained_history_repair_stays_on_session_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive malformed-then-valid output through DSPy and the retained repair path."""
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

    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "2")
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
                result = _build(_WsSig, max_iters=2)(question="report")
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert result.answer == "FIXED"
    assert len(requests) == 3
    assert all(request["model"] == "session-model" for request in requests)
    assert not wrong.history


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


def test_blueprint_schema_repair_uses_own_real_http_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production builder must re-ask malformed typed output on the caller endpoint."""
    requests: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    hook_calls = 0
    hook_lock = threading.Lock()

    def before_hook(*args: Any, **kwargs: Any) -> HookOutcome:
        nonlocal hook_calls
        with hook_lock:
            hook_calls += 1
        return HookOutcome()

    monkeypatch.setattr(hooked_lm_mod, "model_hooks_active", lambda: True)
    monkeypatch.setattr(hooked_lm_mod, "dispatch_before_model", before_hook)
    monkeypatch.setattr(
        hooked_lm_mod, "dispatch_after_model", lambda *args, **kwargs: HookOutcome()
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            model = request["model"]
            counts[model] = counts.get(model, 0) + 1
            prompt = json.dumps(request.get("messages", []))
            owner = next(
                candidate
                for candidate in ("session-a-model", "session-b-model", "session-c-model")
                if candidate in prompt
            )
            content = (
                "[[ ## answer ## ]]\nmissing workflow state\n\n[[ ## completed ## ]]"
                if model != "repair-model" and counts[model] == 1
                else "[[ ## answer ## ]]\nFIXED\n\n[[ ## workflow_state ## ]]\n"
                f'{{"session":"{owner}"}}\n\n[[ ## completed ## ]]'
            )
            body = json.dumps(
                {
                    "id": f"blueprint-{len(requests)}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
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

    monkeypatch.setenv("CLIO_EXTRACT_REPAIR_ATTEMPTS", "1")
    monkeypatch.setenv("CLIO_REPAIRER_MODEL", "repair-model")
    monkeypatch.setattr("clio_agent.lm.io_logging._token_liveness_enabled", lambda: False)
    wrong = DummyLM([])
    wrong.model = "openai/boot-model"
    monkeypatch.setitem(main_thread_config, "lm", wrong)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"

        def build(model: str) -> Any:
            base = type(
                "Base",
                (),
                {
                    "_provider_config": LMProviderConfig(
                        provider="vllm", model=model, api_base=endpoint
                    )
                },
            )()
            return _build_blueprint_dspy_module(
                base,
                AgentDef(
                    id=f"typed-{model}",
                    title="Typed Blueprint",
                    source="expert_pack",
                    module={"kind": "predict"},
                    structured_outputs={"workflow_state": True},
                ),
            )

        modules = {model: build(model) for model in ("session-a-model", "session-b-model")}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {
                    model: pool.submit(module, question=f"report {model}", session_id=model)
                    for model, module in modules.items()
                }
                results = {model: future.result() for model, future in futures.items()}
            # The role override is invocation-local: a later original attempt still
            # uses the caller model, rather than leaving a mutated repair identity.
            again = modules["session-a-model"](
                question="report session-a-model", session_id="session-a-model"
            )
            # Empty role config inherits the production builder caller exactly.
            monkeypatch.delenv("CLIO_REPAIRER_MODEL")
            inherited_model = "session-c-model"
            inherited = build(inherited_model)(
                question=f"report {inherited_model}", session_id=inherited_model
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert {model: result.answer for model, result in results.items()} == {
        "session-a-model": "FIXED",
        "session-b-model": "FIXED",
    }
    assert {model: result.workflow_state for model, result in results.items()} == {
        "session-a-model": {"session": "session-a-model"},
        "session-b-model": {"session": "session-b-model"},
    }
    assert again.workflow_state == {"session": "session-a-model"}
    assert inherited.workflow_state == {"session": "session-c-model"}
    assert counts == {
        "session-a-model": 2,
        "session-b-model": 1,
        "session-c-model": 2,
        "repair-model": 2,
    }
    assert hook_calls == len(requests) == 7
    assert {
        request.get("temperature") for request in requests if request["model"] == "repair-model"
    } == {0.5}
    assert [
        request.get("temperature") for request in requests if request["model"] == "session-c-model"
    ] == [0.0, 0.5]
    assert not wrong.history


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
