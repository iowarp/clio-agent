"""Turn text delivery: live deltas through the LM token hooks, batch parts otherwise.

The turn builds the agent module and runs it ONCE in its forward executor
(``turn_forward._run_module``). Live text reaches the transcript through the LM
token hooks (``clio_agent.runtime.lm_activity``); an answer that arrives only as
the prediction lands as one batch part stamped ``sync_execution_path``.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import (
    _build_prompt_user_agent_module,
    _dynamic_agent_lm_config,
    _pop_stream_fallback,
    _pop_stream_fallback_notes,
    _record_stream_fallback,
    _stream_fallback_reason_capabilities,
    build_app,
)
from clio_agent.gact.turn_forward import _run_module
from clio_agent.gact.types import AgentDef
from clio_agent.providers.claude_code_errors import CLAUDE_CODE_INSTALL_FAILED_MESSAGE
from clio_agent.providers.codex.errors import CODEX_AUTHENTICATION_ERROR_MESSAGE
from tests._config_layer import set_config
from tests._harness import emit_live_text, install_scripted_module

# #948 S4b: turns that POST through the engine now run the default blueprint react
# ``main``; route that root to each test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


@pytest.fixture(autouse=True)
def _claude_code_support_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Claude Code support install off PyPI.

    The SDK is absent from the test venv, so a claude_code turn's auth reprobe
    starts a background model discovery whose support install would look the
    release up on PyPI -- sometimes after the test ended, as a network error.
    """
    from clio_agent.providers import dependencies  # noqa: PLC0415

    def fail_install(**kwargs: Any) -> bool:
        del kwargs
        raise dependencies.ProviderDependencyInstallError("offline")

    monkeypatch.setattr(dependencies, "ensure_claude_code_support", fail_install)


@dataclass
class _Pred:
    answer: str = ""
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def __init__(self, answer: str) -> None:
        self._answer = answer

    def forward(self, question: str, session_id: str) -> _Pred:
        return _Pred(answer=self._answer)


class _ProviderAgent(_Agent):
    """A host agent configured for one provider (the turn records it as the route)."""

    def __init__(self, provider_id: str, provider: str) -> None:
        super().__init__("never returned")
        self._provider_config = SimpleNamespace(provider_id=provider_id, provider=provider)


class _Script:
    """A built module's forward: optional live deltas, then a result or a raise.

    Records every call so a test can assert the module ran exactly once.
    """

    def __init__(
        self,
        chunks: list[tuple[str, str, str]] | None = None,
        *,
        result: Any = None,
        error: BaseException | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.chunks = list(chunks or [])
        self.result = result
        self.error = error
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        for text, agent_id, field in self.chunks:
            emit_live_text(text, agent_id, field)
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture()
def app_client(tmp_path: Path):
    answer = "X" * 200
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(answer))
    # Keep one app-lifetime portal for the fixture. Constructing TestClient
    # without entering it gives each request a transient portal; a turn that
    # correctly outlives POST /messages can then race that portal's teardown.
    with TestClient(app) as client:
        yield app, client, answer


@pytest.fixture()
def enter_client() -> Iterator[Callable[[Any], TestClient]]:
    """Build TestClients that are ENTERED for the whole test, never transient.

    A ``TestClient`` that is constructed but never entered gives EVERY request
    its own anyio portal. ``POST /messages`` returns an ack while the turn is
    still running, so the turn task — created on that transient portal's loop —
    is cancelled when the portal tears down the instant the POST response
    lands. The turn then settles truthfully as ``cancelled`` and every
    assertion about its real outcome fails.

    Entering the client keeps ONE app-lifetime portal (and runs the lifespan,
    so ``turn_runner.bind_loop`` anchors turns to the app loop), letting a turn
    that correctly outlives ``POST /messages`` survive to settle. Pair it with
    :func:`_wait_for_turn_settlement` (or ``complete_turn``) before asserting.
    """

    with contextlib.ExitStack() as stack:

        def _enter(app: Any) -> TestClient:
            return stack.enter_context(TestClient(app))

        yield _enter


def _wait_for_turn_settlement(app: Any, sid: str, timeout: float = 30.0) -> None:
    """Wait on the owned turn task without polling or changing turn semantics.

    30s for the same reason ``conftest.complete_turn`` uses it: every turn builds
    and runs a blueprint module, and slow 2-core CI runners under coverage need
    the margin. The wait returns the instant the task settles, so the bound only
    ever fires on a genuine hang.
    """

    task = app.state.in_flight_turns.get(sid)
    if task is None:
        return

    settled = threading.Event()

    def _observe() -> None:
        task.add_done_callback(lambda _finished: settled.set())

    app.state.turn_runner.call_soon_threadsafe(_observe)
    assert settled.wait(timeout), "background turn did not settle before event assertions"
    assert not app.state.turn_runner.busy(sid)


def _run_turn(client: TestClient, app: Any, text: str = "stream me") -> str:
    """Create a session, post one message and wait for its turn to settle."""

    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": text}]})
    _wait_for_turn_settlement(app, sid)
    return sid


def _last_assistant(client: TestClient, sid: str) -> dict[str, Any]:
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    return [m for m in messages if m["role"] == "assistant"][-1]


def _assert_structured_stream_fallback(payload: dict[str, Any], reason: str) -> None:
    fallback = payload["stream_fallback"]
    assert fallback["reason"] == reason
    assert fallback["synthetic_posthoc"] is True
    assert fallback["live_streaming"] is False
    assert isinstance(fallback["category"], str)
    assert fallback["category"]
    assert isinstance(fallback["description"], str)
    assert fallback["description"]
    assert isinstance(fallback["recovery_actions"], list)
    assert fallback["recovery_actions"]


def test_batch_text_is_delivered_without_deltas(app_client) -> None:
    app, client, answer = app_client
    sid = _run_turn(client, app)

    history = app.state.bus._history.get(sid, [])
    added = [
        e for e in history if e.type == "message.part.added" and e.payload["part"]["type"] == "text"
    ]
    deltas = [e for e in history if e.type == "message.part.delta"]
    completed = [e for e in history if e.type == "message.part.completed"]
    message_completed = [e for e in history if e.type == "message.completed"]

    # An answer that arrives only on the prediction lands as one completed part,
    # never as synthetic chunks: only real live provider output uses deltas.
    assert len(added) == 1
    assert added[0].payload["part"]["text"] == answer
    assert added[0].payload["part"]["metadata"]["stream_source"] == "batch"
    _assert_structured_stream_fallback(added[0].payload["part"]["metadata"], "sync_execution_path")
    assert deltas == []
    assert len(completed) == 1
    assert completed[0].payload["stream_source"] == "batch"
    _assert_structured_stream_fallback(completed[0].payload, "sync_execution_path")
    assert completed[0].payload["final_text"] == answer
    _assert_structured_stream_fallback(
        message_completed[-1].payload["metadata"], "sync_execution_path"
    )
    text_parts = [p for p in _last_assistant(client, sid)["parts"] if p["type"] == "text"]
    assert text_parts[-1]["metadata"]["stream_source"] == "batch"
    _assert_structured_stream_fallback(text_parts[-1]["metadata"], "sync_execution_path")


def test_stream_fallback_reasons_are_audited_and_reject_unknowns(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    catalog = _stream_fallback_reason_capabilities()

    assert {
        "sync_execution_path",
        "mcp_result_downgraded_to_complete",
        "mcp_capability_refused",
        "mcp_protocol_refused",
        "mcp_wire_cancellation_unavailable",
        # P1.4 #1114: the MRTR loop exhausted its config-resolved round bound.
        "mcp_input_required_rounds_exceeded",
        # AF-IMG: the only INPUT-side degradation in the set -- the text still
        # streamed live, the attachment did not ride the request.
        "native_model_inputs_dropped",
    } == set(catalog)
    # Every DELIVERY-path reason means "the tokens did not stream"; the one
    # input-side reason must not be flattened into that claim, so it is asserted
    # on its own honest shape rather than excused from the invariant.
    input_side = {"native_model_inputs_dropped"}
    for reason, details in catalog.items():
        assert details["category"], reason
        assert details["description"], reason
        assert details["recovery_actions"], reason
        if reason in input_side:
            continue
        assert details["synthetic_posthoc"] is True, reason
        assert details["live_streaming"] is False, reason
    dropped = catalog["native_model_inputs_dropped"]
    assert dropped["synthetic_posthoc"] is False
    assert dropped["live_streaming"] is True

    with pytest.raises(ValueError, match="Unknown stream fallback reason"):
        _record_stream_fallback(app, "sid", "unclassified_silent_downgrade")


class _DevelopEraAgent(dspy.Module):
    """The full pre-multimodal forward contract: every mode kwarg, no ``images``.

    This is the shape every module built before the native-input parameters
    landed still has. It must run cleanly on an IMAGELESS turn.
    """

    def __init__(self, answer: str) -> None:
        super().__init__()
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    def forward(
        self,
        question: str,
        session_id: str,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
        cancel_requested: Any | None = None,
    ) -> dspy.Prediction:
        self.calls.append(
            {
                "question": question,
                "session_id": session_id,
                "session_mode": session_mode,
                "session_edit_mode": session_edit_mode,
                "cancel_requested": cancel_requested,
            }
        )
        return dspy.Prediction(answer=self._answer)


class _NativeInputAgent(dspy.Module):
    """A post-multimodal forward that DOES declare the native-input parameters."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    def forward(
        self,
        question: str,
        session_id: str,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
        images: list[Any] | None = None,
        files: list[Any] | None = None,
        cancel_requested: Any | None = None,
    ) -> dspy.Prediction:
        del session_mode, session_edit_mode, cancel_requested
        self.calls.append(
            {
                "question": question,
                "session_id": session_id,
                "images": list(images or []),
                "files": list(files or []),
            }
        )
        return dspy.Prediction(answer="saw them")


def _turn_state(
    app: Any, text: str, *, images: list[Any] | None = None, files: list[Any] | None = None
) -> SimpleNamespace:
    """The slice of ``TurnState`` that ``_run_module`` reads."""

    return SimpleNamespace(
        app=app,
        sid="sid",
        enriched_text=text,
        sess=SimpleNamespace(mode="edit", edit_mode="diff"),
        native_images=list(images or []),
        native_files=list(files or []),
    )


def _never_cancelled() -> bool:
    return False


async def test_imageless_turn_runs_an_agent_without_an_images_parameter(tmp_path: Path) -> None:
    """No ``images=[]`` is injected into a forward that never declared it.

    Injecting it unconditionally raised ``TypeError``, so an ordinary text turn
    on a pre-multimodal module failed instead of answering.
    """

    agent = _DevelopEraAgent("develop era answer")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)

    result = await _run_module(_turn_state(app, "no attachments here"), agent, _never_cancelled)

    # The module ran ONCE with its own contract intact -- no TypeError, no
    # fabricated kwarg, and the mode flags it DOES declare arrived.
    assert agent.calls == [
        {
            "question": "no attachments here",
            "session_id": "sid",
            "session_mode": "edit",
            "session_edit_mode": "diff",
            "cancel_requested": _never_cancelled,
        }
    ]
    assert result.answer == "develop era answer"
    # An imageless turn dropped nothing, so nothing is recorded as degraded.
    assert _pop_stream_fallback_notes(app, "sid") == []


async def test_images_on_an_agent_without_an_images_parameter_are_typed_not_silent(
    tmp_path: Path,
) -> None:
    """A real attachment that cannot be delivered is a typed reason, not a drop."""

    agent = _DevelopEraAgent("answered without seeing the image")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    state = _turn_state(app, "describe the attachment", images=[object()], files=[object()])

    result = await _run_module(state, agent, _never_cancelled)

    assert result.answer == "answered without seeing the image"
    assert len(agent.calls) == 1
    # A NOTE, not the single delivery slot: a later delivery-path reason must not
    # be able to overwrite the record that an attachment never reached the model.
    notes = _pop_stream_fallback_notes(app, "sid")
    assert [note["reason"] for note in notes] == ["native_model_inputs_dropped"]
    assert notes[0]["live_streaming"] is True
    assert notes[0]["synthetic_posthoc"] is False
    assert "images=1" in notes[0]["message"]
    assert "files=1" in notes[0]["message"]
    assert "_DevelopEraAgent.forward" in notes[0]["message"]
    assert _pop_stream_fallback(app, "sid").get("reason") != "native_model_inputs_dropped"


def test_degradation_note_ledger_is_bounded_by_configuration() -> None:
    """The per-session note ledger is an operator knob, and it keeps the NEWEST notes."""

    from clio_agent.gact.stream_fallbacks import (
        _max_notes_per_session,
        record_stream_fallback_note,
        stream_fallback_notes,
    )

    app = SimpleNamespace(state=SimpleNamespace())

    set_config("gact.ledger_retention.stream_fallback_notes.max", 3)
    assert _max_notes_per_session() == 3
    for index in range(10):
        record_stream_fallback_note(app, "sid", "native_model_inputs_dropped", f"drop {index}")

    entries = stream_fallback_notes(app)["sid"]
    assert [note["message"] for note in entries] == ["drop 7", "drop 8", "drop 9"]

    # A nonsense bound degrades to a floor of one rather than a ledger that
    # accepts a note and immediately discards it.
    set_config("gact.ledger_retention.stream_fallback_notes.max", 0)
    assert _max_notes_per_session() == 1
    record_stream_fallback_note(app, "sid", "native_model_inputs_dropped", "drop last")
    assert [note["message"] for note in stream_fallback_notes(app)["sid"]] == ["drop last"]


async def test_native_inputs_reach_an_agent_that_declares_them(tmp_path: Path) -> None:
    """The gate must not become a blanket refusal: a capable forward still gets them."""

    agent = _NativeInputAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    first, second, only_file = object(), object(), object()

    await _run_module(
        _turn_state(app, "look", images=[first, second], files=[only_file]),
        agent,
        _never_cancelled,
    )

    assert agent.calls == [
        {
            "question": "look",
            "session_id": "sid",
            "images": [first, second],
            "files": [only_file],
        }
    ]
    assert _pop_stream_fallback_notes(app, "sid") == []


async def test_a_module_that_raises_is_run_once_and_the_error_propagates(tmp_path: Path) -> None:
    """The one-path engine never re-runs a module: its failure is the turn's failure."""

    calls = {"n": 0}

    class _Raising:
        def __call__(self, **kwargs: Any) -> Any:
            del kwargs
            calls["n"] += 1
            raise TypeError("internal boom")

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("unused"))

    with pytest.raises(TypeError, match="internal boom"):
        await _run_module(_turn_state(app, "hi"), _Raising(), _never_cancelled)
    assert calls["n"] == 1


def test_dynamic_agent_module_carries_codex_provider_config() -> None:
    from clio_agent.config import LMProviderConfig

    base_agent = SimpleNamespace(
        _provider_config=LMProviderConfig(
            provider="codex",
            api_base="codex://direct",
            model="gpt-5.5",
            api_key="x",
            codex_transport="websocket",
        )
    )
    module = _build_prompt_user_agent_module(
        base_agent,
        AgentDef(
            id="reference",
            source="expert_pack",
            title="Reference Expert",
            system_prompt="Review reference evidence.",
        ),
    )
    assert module._provider_config.provider == "codex"
    assert module._provider_config.codex_transport == "websocket"


def test_dynamic_agent_lm_config_preserves_claude_code_transport() -> None:
    from clio_agent.config import LMProviderConfig

    base_agent = SimpleNamespace(
        _provider_config=LMProviderConfig(
            provider="claude_code",
            api_base="claude-code://sdk",
            model="haiku",
            api_key="x",
            claude_code_transport="sdk",
        )
    )

    # Step 6: the delegate returns a ResolvedLMSpec; materialize to the config.
    cfg = _dynamic_agent_lm_config(
        base_agent,
        AgentDef(
            id="earthscope",
            source="expert_pack",
            title="EarthScope",
            system_prompt="Use the EarthScope blueprint.",
        ),
    ).materialize()

    assert cfg.provider == "claude_code"
    assert cfg.claude_code_transport == "sdk"


def test_mid_stream_failure_keeps_the_streamed_text_and_runs_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    script = _Script([("partial ", "", "answer")], error=RuntimeError("stream transport lost"))
    install_scripted_module(monkeypatch, script)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("unused"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    assistant = _last_assistant(client, sid)
    assert len(script.calls) == 1
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "agent_error"
    assert assistant["error_info"]["message"] == "agent.forward raised: stream transport lost"
    assert assistant["error_info"]["details"]["original_error"] == "RuntimeError"
    assert assistant["error_info"]["details"]["partial_output"] is True
    assert assistant["parts"][0]["text"] == "partial "

    history = app.state.bus._history.get(sid, [])
    completed_parts = [
        e
        for e in history
        if e.type == "message.part.completed" and e.payload.get("stream_source") == "live"
    ]
    completed_messages = [e for e in history if e.type == "message.completed"]
    assert completed_parts[-1].payload["final_text"] == "partial "
    assert completed_messages[-1].payload["stop_reason"] == "error"
    assert completed_messages[-1].payload["error_info"]["error"] == "agent_error"


def test_failure_before_any_output_is_a_typed_error_with_no_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    script = _Script(error=RuntimeError("planner/provider failed before output"))
    install_scripted_module(monkeypatch, script)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("unused"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    assistant = _last_assistant(client, sid)
    assert len(script.calls) == 1
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "agent_error"
    assert "planner/provider failed" in assistant["error_info"]["message"]
    assert assistant["parts"] == []

    history = app.state.bus._history.get(sid, [])
    deltas = [e for e in history if e.type == "message.part.delta"]
    completed_messages = [e for e in history if e.type == "message.completed"]
    assert deltas == []
    payload = completed_messages[-1].payload
    assert payload["stop_reason"] == "error"
    assert payload["error_info"]["details"]["partial_output"] is False
    assert payload["error_info"]["details"]["original_error"] == "RuntimeError"
    assert payload["metadata"]["stream_source"] == "batch"
    _assert_structured_stream_fallback(payload["metadata"], "sync_execution_path")


def test_codex_missing_auth_surfaces_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    install_scripted_module(
        monkeypatch,
        _Script(
            error=RuntimeError(
                "[codex-gpt-5.5] unexpected status 401 Unauthorized: "
                "access token rejected, "
                "url: https://chatgpt.com/backend-api/codex/responses"
            )
        ),
    )
    # The message names a CLI provider, so that provider must be the one configured.
    app = build_app(sessions_path=tmp_path / "s.json", agent=_ProviderAgent("codex", "codex"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    error = _last_assistant(client, sid)["error_info"]
    assert error["error"] == "provider_error"
    assert error["message"] == CODEX_AUTHENTICATION_ERROR_MESSAGE
    assert "chatgpt.com" not in error["message"]


def test_provider_http_error_surfaces_one_plain_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    """rel18: OpenRouter's 404 must reach the user in the provider's own words on
    one line, not wrapped in the transport's exception text."""
    import litellm

    raw = litellm.NotFoundError(
        message=(
            "OpenrouterException - "
            '{"error":{"message":"No endpoints available for openrouter/free","code":404}}'
        ),
        model="openrouter/free",
        llm_provider="openrouter",
    )
    provider_error = dspy.LM("openrouter/openrouter/free", api_key="t")._wrap_litellm_exception(raw)
    install_scripted_module(monkeypatch, _Script(error=provider_error))
    app = build_app(sessions_path=tmp_path / "s.json", agent=_ProviderAgent("openrouter", "openai"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    error = _last_assistant(client, sid)["error_info"]
    assert error["error"] == "provider_error"
    assert error["message"] == "OpenRouter: No endpoints available for openrouter/free (HTTP 404)"
    assert error["details"]["original_error"] == type(provider_error).__name__


def test_claude_code_missing_sdk_surfaces_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    install_scripted_module(
        monkeypatch, _Script(error=ModuleNotFoundError("No module named 'claude_agent_sdk'"))
    )
    # The message names a CLI provider, so that provider must be the one configured.
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=_ProviderAgent("claude_code", "claude_code")
    )
    client = enter_client(app)
    sid = _run_turn(client, app)

    error = _last_assistant(client, sid)["error_info"]
    assert error["error"] == "provider_error"
    assert error["message"] == CLAUDE_CODE_INSTALL_FAILED_MESSAGE
    assert "Traceback" not in error["message"]


def test_claude_code_signed_out_surfaces_one_line_and_the_sign_in_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    """#1454: a signed-out Claude subscription reached the user as a raw
    traceback ending "returned an error for model=claude-sonnet-5: success".
    The failed turn now carries one plain line plus the provider to sign in to,
    as a typed ``provider_error`` (``details.reason: provider_auth_required``)."""
    from clio_agent.providers.claude_code_errors import (
        CLAUDE_CODE_SIGNED_OUT_MESSAGE,
        ClaudeCodeSignedOutError,
    )

    signed_out = ClaudeCodeSignedOutError(
        detail="Not logged in · Please run /login", model="claude-sonnet-5"
    )
    # LiteLLM re-wraps a custom provider's exception as TEXT (the live trace:
    # "LMTransportError: [cc-claude-sonnet-5] litellm.MidStreamFallbackError:
    # litellm.APIConnectionError: <the provider's message>").
    wrapped = RuntimeError(
        "[cc-claude-sonnet-5] litellm.MidStreamFallbackError: "
        f"litellm.APIConnectionError: {signed_out}\nTraceback (most recent call last): ..."
    )
    install_scripted_module(monkeypatch, _Script(error=wrapped))
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=_ProviderAgent("claude_code", "claude_code")
    )
    client = enter_client(app)
    sid = _run_turn(client, app, "Hello")

    error = _last_assistant(client, sid)["error_info"]
    assert error["error"] == "provider_error"
    assert error["message"] == CLAUDE_CODE_SIGNED_OUT_MESSAGE
    assert error["details"]["reason"] == "provider_auth_required"
    assert error["details"]["provider_id"] == "claude_code"
    assert error["details"]["provider_label"] == "Claude Code"


def test_a_turn_that_outlives_the_post_still_settles_with_its_real_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    """Lifecycle pin for the failed-turn family above.

    The module blocks past the POST response before it fails, so the outcome no
    longer depends on machine speed: an entered client keeps one app-lifetime
    portal, the turn survives, and it settles on its real error.

    SABOTAGE: build the client as a bare ``TestClient(app)`` instead of via
    ``enter_client`` -> each request gets a transient portal whose teardown
    cancels the still-running turn -> stop_reason comes back ``cancelled``.
    """

    script = _Script(error=RuntimeError("planner/provider failed before output"), delay_s=0.3)
    install_scripted_module(monkeypatch, script)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("unused"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    assistant = _last_assistant(client, sid)
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "agent_error"
    assert len(script.calls) == 1  # and never a second run


def test_non_text_parts_skip_deltas(tmp_path: Path) -> None:
    # An atomic non-text part (tool_call) arrives via .added, never .delta —
    # only live provider text streams as delta events. (routing_decision was the
    # old fixture here; routing decisions are semantic events now, a0e1d9a9.)
    class _ToolObservingAgent:
        def forward(self, question: str, session_id: str) -> _Pred:
            from clio_agent.tools.execution import current_tool_runtime

            observer = current_tool_runtime().tool_observer
            assert observer is not None
            observer("fs_read_file", {"path": "README.md"}, "started", None)
            observer("fs_read_file", {"path": "README.md"}, "completed", None, {"ok": True})
            return _Pred(answer="tool turn done")

    from .conftest import complete_turn

    app = build_app(sessions_path=tmp_path / "s.json", agent=_ToolObservingAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "hi")
    history = app.state.bus._history.get(sid, [])
    tool_call_added = [
        e
        for e in history
        if e.type == "message.part.added" and e.payload["part"]["type"] == "tool_call"
    ]
    assert len(tool_call_added) == 1
    assert tool_call_added[0].payload["part"]["tool_name"] == "fs_read_file"
    # The non-text part never streamed: no delta event targets its part id.
    part_id = tool_call_added[0].payload["part"]["id"]
    deltas = [e for e in history if e.type == "message.part.delta"]
    assert all(e.payload.get("part_id") != part_id for e in deltas)


def test_live_streamed_deltas_are_marked_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    install_scripted_module(
        monkeypatch,
        _Script(
            [("Hel", "", "answer"), ("lo", "", "answer")],
            result=_Pred(answer="Hello", selected_expert="", routing_rationale=""),
        ),
    )
    set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first
    set_config("trace.path", str(tmp_path / "semantic_traces"))
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    history = app.state.bus._history.get(sid, [])
    added = [e for e in history if e.type == "message.part.added"]
    deltas = [e for e in history if e.type == "message.part.delta"]
    trace_backend = app.state.semantic_trace_backend
    trace_backend.flush()
    trace_rows = [
        line
        for line in (tmp_path / "semantic_traces" / f"{sid}.semantic.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    completed = [
        e
        for e in history
        if e.type == "message.part.completed" and e.payload.get("final_text") == "Hello"
    ]
    message_completed = [e for e in history if e.type == "message.completed"]

    assert [d.payload["delta"]["text_append"] for d in deltas] == ["Hel", "lo"]
    assert all(d.payload["stream_source"] == "live" for d in deltas)
    assert all(d.payload["signature_field_name"] == "answer" for d in deltas)
    text_added = [e for e in added if e.payload["part"]["type"] == "text"]
    assert text_added[-1].payload["part"]["metadata"]["signature_field_name"] == "answer"
    assert any(
        '"event_type": "lm.token.delta"' in row and '"delta": "Hel"' in row for row in trace_rows
    )
    assert any(
        '"event_type": "lm.token.delta"' in row and '"delta": "lo"' in row for row in trace_rows
    )
    assert all(e.type != "semantic.event" for e in history)
    assert len(completed) == 1
    assert completed[0].payload["stream_source"] == "live"
    assert message_completed[-1].payload["metadata"]["stream_source"] == "live"
    text_parts = [p for p in _last_assistant(client, sid)["parts"] if p["type"] == "text"]
    assert text_parts[-1]["metadata"]["stream_source"] == "live"
    assert "stream_fallback" not in text_parts[-1]["metadata"]


def test_live_streamed_contract_fields_emit_message_part_deltas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    install_scripted_module(
        monkeypatch,
        _Script(
            [
                ("thinking ", "main", "reasoning"),
                ("next ", "main", "next_thought"),
                ("answer", "main", "answer"),
            ],
            result=_Pred(answer="answer", selected_expert="", routing_rationale=""),
        ),
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    history = app.state.bus._history.get(sid, [])
    transcript_events = [e for e in history if e.type.startswith("turn.")]
    part_deltas = [e for e in history if e.type == "message.part.delta"]

    # #767 PR5: the normalized turn.text.delta twin is retired; the streamed
    # contract fields ride message.part.delta only.
    assert [e for e in history if e.type == "turn.text.delta"] == []
    assert transcript_events[0].type == "turn.started"
    assert [e.payload["signature_field_name"] for e in part_deltas] == [
        "reasoning",
        "next_thought",
        "answer",
    ]
    assert [e.payload["delta"]["text_append"] for e in part_deltas] == [
        "thinking ",
        "next ",
        "answer",
    ]
    assert transcript_events[-1].type == "turn.completed"
    assert all("[[ ##" not in e.payload["delta"].get("text_append", "") for e in part_deltas)


def test_provider_aux_streams_as_thinking_part(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    install_scripted_module(
        monkeypatch,
        _Script(
            [("raw provider thought", "main", "provider_thinking:claude_code_sdk")],
            result=_Pred(answer="done", selected_expert="", routing_rationale=""),
        ),
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    history = app.state.bus._history.get(sid, [])

    # #767 PR5: the normalized turn.trace.delta twin is retired; provider aux
    # rides a message.part.* thinking part.
    assert [e for e in history if e.type == "turn.trace.delta"] == []
    thinking_added = [
        e
        for e in history
        if e.type == "message.part.added" and e.payload["part"]["type"] == "thinking"
    ]
    assert len(thinking_added) == 1
    thinking_meta = thinking_added[0].payload["part"]["metadata"]
    assert thinking_meta["thinking_source"] == "provider"
    assert thinking_meta["provider_source"] == "claude_code_sdk"
    thinking_deltas = [
        e
        for e in history
        if e.type == "message.part.delta"
        and e.payload["signature_field_name"] == "provider_thinking:claude_code_sdk"
    ]
    assert len(thinking_deltas) == 1
    assert thinking_deltas[0].payload["delta"]["text_append"] == "raw provider thought"


def _turn_id_of(history: list[Any]) -> str:
    user_created = [
        e for e in history if e.type == "message.created" and e.payload.get("role") == "user"
    ]
    assert len(user_created) == 1
    turn_id = user_created[0].payload["id"]
    # The user message correlates to its own turn (#711).
    assert user_created[0].payload["turn_id"] == turn_id
    return turn_id


def test_message_events_carry_turn_id_and_stream_source_batch(app_client) -> None:
    """#711: every assistant message.created / message.part.* / message.completed event
    carries the SAME turn_id (== the user message id) plus a stream_source, so a consumer
    joins assistant prose to the execution trajectory without heuristics."""

    app, client, _ = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "correlate me"}]},
    )
    _wait_for_turn_settlement(app, sid)

    history = app.state.bus._history.get(sid, [])
    turn_id = _turn_id_of(history)

    part_events = [
        e
        for e in history
        if e.type in {"message.part.added", "message.part.delta", "message.part.completed"}
    ]
    assert part_events
    for e in part_events:
        assert e.payload["turn_id"] == turn_id, e.type
        assert e.payload["stream_source"] in {"live", "batch"}, e.type

    asst_created = [
        e for e in history if e.type == "message.created" and e.payload.get("role") == "assistant"
    ]
    assert asst_created
    assert all(e.payload["turn_id"] == turn_id for e in asst_created)

    completed = [e for e in history if e.type == "message.completed"]
    assert completed
    assert all(e.payload["turn_id"] == turn_id for e in completed)


def test_live_streamed_events_carry_turn_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    """#711 on the live path: lazily-created streamed assistant message + its deltas/
    completed events all correlate to the originating user turn."""

    install_scripted_module(
        monkeypatch,
        _Script(
            [("Hel", "", "answer"), ("lo", "", "answer")],
            result=_Pred(answer="Hello", selected_expert="", routing_rationale=""),
        ),
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = enter_client(app)
    sid = _run_turn(client, app)

    history = app.state.bus._history.get(sid, [])
    turn_id = _turn_id_of(history)

    deltas = [e for e in history if e.type == "message.part.delta"]
    assert deltas
    for e in deltas:
        assert e.payload["turn_id"] == turn_id
        assert e.payload["stream_source"] == "live"

    asst_created = [
        e for e in history if e.type == "message.created" and e.payload.get("role") == "assistant"
    ]
    assert asst_created
    assert all(e.payload["turn_id"] == turn_id for e in asst_created)


def test_streamed_visible_answer_survives_to_wire_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    """#881: a visible thinking/text part carries the model's prose to the wire
    BYTE-FOR-BYTE. A sentence that NAMES a typed workflow_state field — exactly the
    kind the deleted transcript cleaner used to strip — must survive UNEDITED
    across the streamed-chunk buffer -> close -> persist path. This goes RED the
    instant any prose scrub returns to the streamed-text path."""

    # The chunks split the "workflow_state shows ..." sentence across a boundary:
    # the persisted part is simply the concatenation, verbatim.
    sub_chunks = [
        "I identified MTA1 as the nearest ranked station. ",
        "The typed workflow_state shows ",
        "station_catalog is complete. Coverage exists in the region.",
    ]
    verbatim = "".join(sub_chunks)
    install_scripted_module(
        monkeypatch,
        _Script(
            [(chunk, "data", "answer") for chunk in sub_chunks]
            # Agent change -> closes the "data" answer part (stored verbatim).
            + [("Final orchestrator answer.", "main", "answer")],
            result=_Pred(
                answer="Final orchestrator answer.", selected_expert="", routing_rationale=""
            ),
        ),
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = enter_client(app)
    sid = _run_turn(client, app, "go")

    data_parts = [
        p
        for p in _last_assistant(client, sid)["parts"]
        if p["type"] == "text" and p.get("agent_id") == "data" and (p["text"] or "").strip()
    ]
    assert data_parts, "expected the sub-agent (data) streamed answer part"
    # BYTE-FOR-BYTE: the persisted visible part is the model's text verbatim —
    # the workflow_state sentence is intact, nothing scrubbed or truncated.
    assert data_parts[-1]["text"] == verbatim


def test_streamed_field_buffer_cleared_at_turn_end_and_turn_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enter_client: Callable[[Any], TestClient]
) -> None:
    """iowarp/clio-agent#757: ``app.state.live_streamed_field_text`` must be
    cleared at turn end, and the finalize thinking-part suppression must match
    only against the CURRENT turn's streamed text.

    Turn 1 streams contract reasoning live; turn 2 streams nothing but its
    finalize ``reasoning`` repeats turn 1's phrasing. Before the fix the buffer
    survived turn 1, so turn 2's thinking part was wrongly suppressed as
    "already streamed" and the dict grew forever.
    """
    from .conftest import complete_turn

    repeated = "I will inspect the HDF5 schema before answering the user."

    @dataclass
    class _ReasoningPred:
        answer: str = ""
        selected_expert: str = ""
        routing_rationale: str = ""
        reasoning: str = ""

    calls = {"n": 0}

    def script(**kwargs: Any) -> _ReasoningPred:
        del kwargs
        calls["n"] += 1
        if calls["n"] == 1:
            # Turn 1: the reasoning channel streams live -> recorded in the buffer.
            emit_live_text(repeated, "main", "reasoning")
            emit_live_text("turn one answer", "main", "answer")
            return _ReasoningPred(answer="turn one answer")
        # Turn 2: nothing streams; the finalize reasoning repeats turn 1's phrasing.
        return _ReasoningPred(answer="turn two answer", reasoning=repeated)

    install_scripted_module(monkeypatch, script)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent("fallback"))
    client = enter_client(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    complete_turn(client, sid, "turn one")
    _wait_for_turn_settlement(app, sid)
    store = getattr(app.state, "live_streamed_field_text", {}) or {}
    assert store.get(sid) in (None, {}), (
        f"live_streamed_field_text must be cleared at turn end, got: {store.get(sid)!r}"
    )

    assistant2 = complete_turn(client, sid, "turn two")
    _wait_for_turn_settlement(app, sid)
    thinking_texts = [p["text"] for p in assistant2["parts"] if p["type"] == "thinking"]
    assert thinking_texts == [repeated], (
        "turn 2's thinking part must NOT be suppressed by turn 1's streamed text"
    )
    store = getattr(app.state, "live_streamed_field_text", {}) or {}
    assert store.get(sid) in (None, {})
