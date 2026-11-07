"""A fake ``github-copilot-sdk`` (the ``copilot`` package) for the Copilot provider tests.

No runtime process, no network. :func:`install` puts ``copilot`` and the submodules
CLIO imports into ``sys.modules``, points the provider at a fixed runtime path and a
GitHub token, and returns a :class:`FakeSDK` recorder:

* every ``CopilotClient`` records its keyword arguments, start/stop, the sessions it
  created (with their keyword arguments) and the ones it deleted;
* every ``session.send(prompt, attachments=...)`` is recorded and answered from a
  script (the same reply each time by default): each event is delivered to the
  session's handlers, then ``session.idle`` -- unless the turn ends in
  ``session.error`` or is a :data:`HANG` (nothing at all, for stall tests);
* ``get_auth_status``, ``list_models`` and ``rpc.account.get_quota`` answer from the
  recorder's fields.

Events are duck-typed like the SDK's: ``.type`` is the wire type, ``.data`` the payload.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.machinery import ModuleSpec
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from dspy.lm15 import Message, Request, Response

TOKEN = "ghu_fake_github_token"
RUNTIME = "/fake/copilot-runtime"
#: A scripted turn that sends nothing back (not even ``session.idle``).
HANG: list[Any] = []


def event(kind: str, **data: Any) -> SimpleNamespace:
    """One session event of wire type ``kind``."""
    return SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data))


def text(chunk: str) -> SimpleNamespace:
    return event("assistant.message_delta", delta_content=chunk, parent_tool_call_id=None)


def reasoning(chunk: str) -> SimpleNamespace:
    return event("assistant.reasoning_delta", delta_content=chunk)


def usage(**fields: Any) -> SimpleNamespace:
    fields.setdefault("input_tokens", 2)
    fields.setdefault("output_tokens", 3)
    return event("assistant.usage", **fields)


def error(**fields: Any) -> SimpleNamespace:
    fields.setdefault("message", "runtime said no")
    return event("session.error", **fields)


def answer(reply: str = "Answer", **usage_fields: Any) -> list[Any]:
    """A plain streamed reply: two deltas, the assembled message, the usage."""
    half = len(reply) // 2
    return [
        text(reply[:half]),
        text(reply[half:]),
        event("assistant.message", content=reply, parent_tool_call_id=None),
        usage(**usage_fields),
    ]


def model_info(
    model_id: str,
    *,
    name: str | None = None,
    state: str | None = "enabled",
    vision: bool = False,
    reasoning: bool = False,
    prompt_tokens: int | None = 128_000,
    context_tokens: int | None = 200_000,
    output_tokens: int | None = 16_000,
    multiplier: float | None = 1.0,
    efforts: list[str] | None = None,
) -> SimpleNamespace:
    """A ``ModelInfo`` as ``list_models()`` returns it."""
    return SimpleNamespace(
        id=model_id,
        name=name or model_id.upper(),
        capabilities=SimpleNamespace(
            supports=SimpleNamespace(vision=vision, reasoning_effort=reasoning),
            limits=SimpleNamespace(
                max_prompt_tokens=prompt_tokens,
                max_context_window_tokens=context_tokens,
                max_output_tokens=output_tokens,
            ),
        ),
        policy=None if state is None else SimpleNamespace(state=state, terms=""),
        billing=None if multiplier is None else SimpleNamespace(multiplier=multiplier),
        supported_reasoning_efforts=efforts,
        default_reasoning_effort=(efforts or [None])[0],
    )


class JsonRpcError(Exception):
    """Stands in for ``copilot._jsonrpc.JsonRpcError``."""


class ProcessExitedError(Exception):
    """Stands in for ``copilot._jsonrpc.ProcessExitedError``."""


class StopError(Exception):
    """Stands in for ``copilot.client.StopError``."""


@dataclass
class FakeSDK:
    """What the fake module recorded, and the answers it gives."""

    reply: Callable[[], list[Any]] = answer
    script: list[list[Any] | BaseException] = field(default_factory=list)
    clients: list[Any] = field(default_factory=list)
    authenticated: bool = True
    status_message: str = ""
    models: list[Any] = field(default_factory=lambda: [model_info("gpt-5")])
    list_error: BaseException | None = None
    start_error: BaseException | None = None
    quota: dict[str, Any] = field(default_factory=dict)
    quota_error: BaseException | None = None
    quota_tokens: list[str] = field(default_factory=list)

    def sessions(self) -> list[Any]:
        return [session for client in self.clients for session in client.sessions]

    def sends(self) -> list[tuple[str, Any, str]]:
        """Every ``(prompt, attachments, session_id)`` sent, across all clients."""
        return [
            (prompt, attachments, session.session_id)
            for session in self.sessions()
            for prompt, attachments in session.sends
        ]

    def next_turn(self) -> list[Any]:
        if self.script:
            turn = self.script.pop(0)
            if isinstance(turn, BaseException):
                raise turn
            return turn
        return self.reply()


def _modules(state: FakeSDK) -> dict[str, ModuleType]:
    class RuntimeConnection:
        @staticmethod
        def for_stdio(*, path: str) -> SimpleNamespace:
            return SimpleNamespace(kind="stdio", path=path)

    class PermissionDecisionReject:
        def __init__(self, *, feedback: str) -> None:
            self.feedback = feedback

    class AccountGetQuotaRequest:
        def __init__(self, *, git_hub_token: str) -> None:
            self.git_hub_token = git_hub_token

    class Session:
        def __init__(self, session_id: str, kwargs: dict[str, Any]) -> None:
            self.session_id = session_id
            self.kwargs = kwargs
            self.sends: list[tuple[str, Any]] = []
            self.handlers: list[Callable[[Any], None]] = []
            self.aborted = 0
            self.disconnected = False

        def on(self, handler: Callable[[Any], None]) -> Callable[[], None]:
            self.handlers.append(handler)
            return lambda: self.handlers.remove(handler)

        async def send(self, prompt: str, *, attachments: Any = None) -> str:
            self.sends.append((prompt, attachments))
            turn = state.next_turn()
            for item in turn:
                for handler in list(self.handlers):
                    handler(item)
            ended = turn and getattr(turn[-1].type, "value", "") == "session.error"
            if turn is not HANG and not ended:
                for handler in list(self.handlers):
                    handler(event("session.idle"))
            return "message-id"

        async def abort(self) -> None:
            self.aborted += 1

        async def disconnect(self) -> None:
            self.disconnected = True

    class Account:
        async def get_quota(self, request: Any, *, timeout: float) -> Any:
            del timeout
            state.quota_tokens.append(request.git_hub_token)
            if state.quota_error is not None:
                raise state.quota_error
            return SimpleNamespace(quota_snapshots=state.quota)

    class CopilotClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.sessions: list[Session] = []
            self.deleted: list[str] = []
            self.rpc = SimpleNamespace(account=Account())
            state.clients.append(self)

        async def start(self) -> None:
            if state.start_error is not None:
                raise state.start_error
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

        async def force_stop(self) -> None:
            self.stopped = True

        async def get_auth_status(self) -> Any:
            return SimpleNamespace(
                isAuthenticated=state.authenticated,
                authType="token",
                host="https://github.com",
                login="octocat" if state.authenticated else None,
                statusMessage=state.status_message,
            )

        async def list_models(self) -> list[Any]:
            if state.list_error is not None:
                raise state.list_error
            return list(state.models)

        async def create_session(self, *, session_id: str, **kwargs: Any) -> Session:
            session = Session(session_id, kwargs)
            self.sessions.append(session)
            return session

        async def delete_session(self, session_id: str) -> None:
            self.deleted.append(session_id)

    root = ModuleType("copilot")
    root.__path__ = []  # a package, so the submodules below import
    root.CopilotClient = CopilotClient  # type: ignore[attr-defined]
    root.RuntimeConnection = RuntimeConnection  # type: ignore[attr-defined]
    generated = ModuleType("copilot.generated")
    generated.__path__ = []
    rpc = ModuleType("copilot.generated.rpc")
    rpc.PermissionDecisionReject = PermissionDecisionReject  # type: ignore[attr-defined]
    rpc.AccountGetQuotaRequest = AccountGetQuotaRequest  # type: ignore[attr-defined]
    jsonrpc = ModuleType("copilot._jsonrpc")
    jsonrpc.JsonRpcError = JsonRpcError  # type: ignore[attr-defined]
    jsonrpc.ProcessExitedError = ProcessExitedError  # type: ignore[attr-defined]
    client = ModuleType("copilot.client")
    client.StopError = StopError  # type: ignore[attr-defined]
    modules = {
        "copilot": root,
        "copilot.generated": generated,
        "copilot.generated.rpc": rpc,
        "copilot._jsonrpc": jsonrpc,
        "copilot.client": client,
    }
    for name, module in modules.items():
        # ``importlib.util.find_spec`` (the readiness check) needs a spec.
        module.__spec__ = ModuleSpec(name, None, is_package=hasattr(module, "__path__"))
    return modules


def install(
    monkeypatch: pytest.MonkeyPatch, *, token: str | None = TOKEN, **config: Any
) -> FakeSDK:
    """Install the fake SDK, a runtime path and (unless ``token=None``) a GitHub sign-in."""
    from clio_agent.providers.copilot import auth, sessions  # noqa: PLC0415

    state = FakeSDK(**config)
    for name, module in _modules(state).items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sessions, "runtime_path", lambda: RUNTIME)
    monkeypatch.setattr("clio_agent.providers.copilot.runtime.runtime_path", lambda: RUNTIME)
    if token is None:
        auth.set_token_source(None)
    else:
        auth.set_token_source(lambda: (token, 3600))
    return state


def reset() -> None:
    """Empty the Copilot pool, the conversation registry and the token source."""
    from clio_agent.providers import claude_code_conversation  # noqa: PLC0415
    from clio_agent.providers.copilot import auth  # noqa: PLC0415
    from clio_agent.providers.copilot.sessions import SESSION_POOL  # noqa: PLC0415

    claude_code_conversation.forward_resets().clear_for_tests()
    SESSION_POOL.reset_for_tests()
    auth.set_token_source(None)


def request(*messages: Message, system: str = "", model: str = "gpt-5") -> Request:
    """A request for the engine (one user message when none is given)."""
    return Request(
        model=f"copilot/{model}",
        system=system or None,
        messages=messages or (Message.user("hello"),),
    )


async def drive(request_: Request, **engine: Any) -> Response:
    """Run one call through the real engine and pooled transport."""
    from clio_agent.providers.copilot.engine import AsyncCopilotEngine  # noqa: PLC0415

    model = request_.model.removeprefix("copilot/")
    engine.setdefault("cwd", "/w")
    engine.setdefault("idle_timeout_s", 5.0)
    return await AsyncCopilotEngine(model, **engine).complete(request_)
