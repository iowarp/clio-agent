"""Read an API key's rate-limit headers off the calls it already makes (M-D12).

The lm15 providers read a response's headers only when the call fails, so a
successful Anthropic or OpenAI API-key call drops the ``anthropic-ratelimit-*``
and ``x-ratelimit-*`` headers the Usage view needs. These transports are the
stock lm15 ones that hand each response's headers to
:func:`~clio_agent.providers.usage.capture.record_api_key_headers` before the
provider reads the body. :func:`recording_engines` builds the engine pair
``dspy.LM`` runs on, with the same router settings DSPy would build itself.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from dspy._vendor.lm15.router import RouterConfig
from dspy._vendor.lm15.transports import (
    AsyncTransportResponse,
    StdlibAsyncTransport,
    StdlibTransport,
    TransportRequest,
    TransportResponse,
)
from dspy.clients.engines.lm15_engine import AsyncLM15Engine, LM15Engine, timeouts_for

from clio_agent.providers.usage.capture import record_api_key_headers

#: Fleet accounts whose API keys answer with rate-limit headers.
RECORDED_ACCOUNTS = frozenset({"anthropic", "openai"})


class RecordingTransport(StdlibTransport):
    """The stock sync transport, recording each response's rate-limit headers."""

    def __init__(self, *, account: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.account = account
        self._settings = kwargs

    def stream(self, request: TransportRequest) -> TransportResponse:
        response = super().stream(request)
        record_api_key_headers(self.account, response.headers)
        return response

    def copy(self) -> RecordingTransport:
        return RecordingTransport(account=self.account, **self._settings)


class _RecordingAsyncTransport(StdlibAsyncTransport):
    def __init__(self, *, account: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.account = account

    async def _do_stream(self, request: TransportRequest) -> AsyncTransportResponse:
        response = await super()._do_stream(request)
        record_api_key_headers(self.account, response.headers)
        return response


class RecordingAsyncTransport:
    """One recording async transport per event loop: lm15's pools are loop-bound."""

    def __init__(self, *, account: str, **kwargs: Any) -> None:
        self.account = account
        self._kwargs = kwargs
        self._per_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, _RecordingAsyncTransport
        ] = weakref.WeakKeyDictionary()

    def _current(self) -> _RecordingAsyncTransport:
        loop = asyncio.get_running_loop()
        transport = self._per_loop.get(loop)
        if transport is None:
            transport = _RecordingAsyncTransport(account=self.account, **self._kwargs)
            self._per_loop[loop] = transport
        return transport

    def stream(self, request: TransportRequest) -> Any:
        return self._current().stream(request)

    async def aclose(self) -> None:
        transport = self._per_loop.pop(asyncio.get_running_loop(), None)
        if transport is not None:
            await transport.aclose()


def recording_engines(
    account: str, model: str, *, api_key: str = "", api_base: str = "", timeout: Any = None
) -> tuple[LM15Engine, AsyncLM15Engine]:
    """The sync and async engines for ``model`` on ``account``'s key, recording usage."""

    provider = LM15Engine(RouterConfig(env={}), model_type="chat").resolve(model).provider
    timeouts = timeouts_for(timeout)
    settings: dict[str, Any] = {
        "api_keys": {provider: api_key} if api_key else None,
        "base_urls": {provider: api_base} if api_base else None,
        "timeouts": timeouts,
    }
    sync_config = RouterConfig(**settings)
    transport_kwargs = sync_config.transport_kwargs()
    sync = LM15Engine(
        RouterConfig(**settings, transport=RecordingTransport(account=account, **transport_kwargs)),
        model_type="chat",
    )
    async_ = AsyncLM15Engine(
        RouterConfig(
            **settings, transport=RecordingAsyncTransport(account=account, **transport_kwargs)
        ),
        model_type="chat",
    )
    return sync, async_


__all__ = [
    "RECORDED_ACCOUNTS",
    "RecordingAsyncTransport",
    "RecordingTransport",
    "recording_engines",
]
