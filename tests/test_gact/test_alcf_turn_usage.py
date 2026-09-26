"""An ALCF (OpenAI-compatible, vLLM) turn records the provider's own token usage.

The bottom bar read "Tokens: 0 Cost: -" after ALCF Metis turns. Two gaps, both
exercised here against a recorded Metis stream served by a local HTTP server:

* A streamed call never asked for usage (``stream_options.include_usage``), and
  vLLM sends its usage block only when asked, so the call's usage was litellm's
  local estimate at best.
* Every expert / dynamic-agent forward builds a fresh LM from its resolved
  spec; the turn's usage rollup only summed the app's long-lived LMs, so those
  per-forward calls were invisible and the turn recorded zero tokens.

Cost stays "not reported" (unknown) because ALCF publishes no price: never 0.0.
"""

from __future__ import annotations

import contextvars
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.gact.turn_usage import roll_up_usage
from clio_agent.gact.usage import _snapshot_lm_history_index
from clio_agent.runtime import turn_lm_ledger

_RECORDED = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "alcf" / "metis_chat_stream.json").read_text(
        encoding="utf-8"
    )
)


class _MetisReplay(BaseHTTPRequestHandler):
    """Replays the recorded stream; the usage chunk only when it is asked for."""

    requests: list[dict[str, Any]] = []

    def log_message(self, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        type(self).requests.append(body)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        chunks = list(_RECORDED["chunks"])
        if (body.get("stream_options") or {}).get("include_usage"):
            chunks.append(_RECORDED["usage_chunk"])
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def metis() -> Iterator[str]:
    _MetisReplay.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MetisReplay)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _metis_config(api_base: str) -> LMProviderConfig:
    return LMProviderConfig(
        provider_id="argonne_metis",
        api_base=api_base,
        model="gpt-oss-120b",
        api_key="recorded-test-token",
    )


def _app() -> Any:
    """An app with no long-lived LMs: only the per-forward LM can carry usage."""

    return SimpleNamespace(state=SimpleNamespace(agent=None))


def _expert_forward(api_base: str) -> None:
    """What a dynamic agent's forward does: a fresh LM inside ``dspy.context``."""

    import dspy

    from clio_agent.config import create_chat_adapter
    from clio_agent.lm.hooked_lm import create_hooked_lm

    class Answer(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    cfg = _metis_config(api_base)
    with dspy.context(lm=create_hooked_lm(cfg), adapter=create_chat_adapter(cfg)):
        dspy.Predict(Answer)(question="Which station?")


def test_a_streamed_call_asks_for_and_records_the_provider_usage(metis: str) -> None:
    from clio_agent.lm.factory import create_lm

    lm = create_lm(_metis_config(metis))
    import dspy

    from clio_agent.config import create_chat_adapter

    with dspy.context(lm=lm, adapter=create_chat_adapter(_metis_config(metis))):
        dspy.Predict("question -> answer")(question="Which station?")

    assert _MetisReplay.requests[-1]["stream"] is True
    assert _MetisReplay.requests[-1]["stream_options"] == {"include_usage": True}
    usage = lm.history[-1]["usage"]
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (1874, 42)


def test_a_turn_on_a_per_forward_alcf_lm_rolls_up_its_tokens(
    metis: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)
    app = _app()
    token = turn_lm_ledger.open_ledger()
    try:
        state = SimpleNamespace(
            app=app,
            sid="sess_alcf",
            history_start=_snapshot_lm_history_index(app),
            turn_tokens={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
            turn_cost=0.0,
            turn_cost_known=False,
            answer_text="The station is SIO5.",
            enriched_text="Which station?",
            last_prompt_usage={},
            agent_runtime={"model": {"provider_id": "argonne_metis", "model_id": "gpt-oss-120b"}},
        )
        # The forward runs on an executor thread under a copy of the turn context.
        worker = threading.Thread(
            target=contextvars.copy_context().run, args=(_expert_forward, metis)
        )
        worker.start()
        worker.join(timeout=60)

        roll_up_usage(state, SimpleNamespace(answer=state.answer_text))
    finally:
        turn_lm_ledger.close_ledger(token)

    assert state.turn_tokens["input"] == 1874
    assert state.turn_tokens["output"] == 42
    # ALCF reports no price and none is in the table: unknown, never a real $0.
    assert state.turn_cost_known is False


def test_the_ledger_is_empty_outside_a_turn(metis: str) -> None:
    from clio_agent.lm.factory import create_lm

    create_lm(_metis_config(metis))
    assert turn_lm_ledger.ledger_lms() == []
