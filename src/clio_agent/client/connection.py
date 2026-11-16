"""Attach to a CLIO server or start one, and let go of it: a client's server lifecycle.

Opening a :class:`~clio_agent.client.ClioClient`:

1. **Attach** when a server answers ``GET /v1/health`` at ``options.url``.
2. Otherwise **start** one on this machine (``clio_agent.serve.ensure_server``, which
   runs ``clio-agent serve`` on the configured port) when ``options.launch`` is true
   and the URL is local. A remote URL that does not answer, or ``launch=False`` with
   nothing running, raises :class:`ServerUnavailable` naming the URL and the fix.
3. **Register** with the server's attached-client count (``/v1/server/clients``), so
   the desktop app and other scripts can share it.
4. Check a model provider is configured (:class:`ProviderNotConfigured` otherwise).
5. Print one banner to stderr.

Closing releases the registration. A server this client started is stopped only when
no other client is attached; it then stops itself, and this waits for its process
tree to exit (``clio_agent.serve_exit``). An attached server is never stopped.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from clio_agent.client.errors import ClioSDKError

if TYPE_CHECKING:
    from clio_agent.client.options import ClioOptions

#: Seconds to wait for a provider that is still configuring before calling it missing.
PROVIDER_SETTLE_S = 60.0
#: Seconds to wait for a server that answers health but is still starting its services.
READY_TIMEOUT_S = 300.0
#: Seconds one provider read may take (it can probe the provider's sign-in).
PROVIDER_READ_TIMEOUT_S = 180.0


class ServerSetupError(ClioSDKError):
    """The script's own setup failed: no server, no provider, a bad agent. The message
    says what went wrong and what to do."""


class ServerUnavailable(ServerSetupError):
    """No CLIO server answers at the URL, and none may be started there."""


class ServerStartFailed(ServerSetupError):
    """A local CLIO server could not be started."""


class ProviderNotConfigured(ServerSetupError):
    """The server has no model provider configured."""


@dataclass
class Connection:
    """How this client holds its server."""

    base_url: str
    started: bool
    client_id: str | None = None
    port: int = 0


def _answers(base_url: str, token: str | None) -> bool:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.get(base_url + "/v1/health", headers=headers, timeout=2.0)
    except httpx.HTTPError:
        return False
    return response.status_code in (200, 503)


def _start(options: ClioOptions) -> bool:
    """Start a local server on the options' port; True when this call spawned it."""

    from clio_agent import serve  # noqa: PLC0415

    host = urlsplit(options.base_url).hostname or "127.0.0.1"
    host = "127.0.0.1" if host == "localhost" else host
    try:
        serve.ensure_server(options.port, host, stop_with_last_client=True)
    except serve.ServeError as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        log = payload.get("log")
        raise ServerStartFailed(
            f"Could not start a CLIO server on port {options.port} ({exc.reason}): {exc}."
            + (f" Its log is {log}." if log else "")
            + " Check that clio-agent is installed in this Python environment "
            "(pip install clio-agent) and that the port is free, or start CLIO yourself "
            "(clio-agent serve) and open the client with launch=False."
        ) from exc
    action = serve.last_action() or {}
    return action.get("reason") == "spawned"


def _register(http: httpx.Client) -> str | None:
    """Register with the attached-client count, once the server's services are up.

    ``/v1/health`` answers as soon as the process serves, before its task runtime
    (and so every session route) is ready: a 503 that says so is waited out, bounded
    by :data:`READY_TIMEOUT_S`, and the wait is announced once on stderr.
    """

    deadline = time.monotonic() + READY_TIMEOUT_S
    announced = False
    while True:
        response = http.post(
            "/v1/server/clients",
            json={"kind": "sdk", "pid": os.getpid(), "label": " ".join(sys.argv)[:200]},
        )
        if response.status_code != 503 or time.monotonic() > deadline:
            break
        if not announced:
            print("clio: waiting for the CLIO server to finish starting...", file=sys.stderr)
            announced = True
        time.sleep(1.0)
    if response.status_code == 404:
        return None  # an older server without the count: attach only, never stop it
    if not response.is_success:
        from clio_agent.client.errors import error_from_response  # noqa: PLC0415

        raise error_from_response(response)
    return str(response.json()["client_id"])


def _check_provider(http: httpx.Client) -> dict[str, Any]:
    deadline = time.monotonic() + PROVIDER_SETTLE_S
    while True:
        # A fresh home's first read re-probes the provider's sign-in: allow it time.
        body = http.get("/v1/providers/lm", timeout=PROVIDER_READ_TIMEOUT_S).json()
        if body.get("configured"):
            return body
        if body.get("state") != "configuring" or time.monotonic() > deadline:
            detail = body.get("error") or body.get("status_message") or "none is configured"
            raise ProviderNotConfigured(
                f"The CLIO server has no model provider ({detail}). Set CLIO_LM_PROVIDER "
                "and the provider's key in the environment before the server starts (for "
                "example CLIO_LM_PROVIDER=anthropic with ANTHROPIC_API_KEY), or sign in to "
                "a provider in CLIO."
            )
        time.sleep(1.0)


def open_connection(options: ClioOptions, http: httpx.Client) -> Connection:
    """Attach or start, register and check the provider (see the module docstring)."""

    base_url = options.base_url
    started = False
    if not _answers(base_url, options.resolved_token()):
        if not options.is_local:
            raise ServerUnavailable(
                f"No CLIO server answers at {base_url}. A client starts servers only on "
                "this machine: deploy CLIO there and check the URL, or use a local url."
            )
        if not options.launch:
            raise ServerUnavailable(
                f"No CLIO server answers at {base_url}, and launch=False. Start CLIO "
                "(open the desktop app, or run clio-agent serve), or allow launch=True."
            )
        started = _start(options)
        token = options.resolved_token()  # the started server's own record
        if token:
            http.headers["Authorization"] = f"Bearer {token}"
    connection = Connection(base_url, started, port=options.port)
    try:
        connection.client_id = _register(http)
        provider = _check_provider(http)
    except BaseException:
        close_connection(connection, http)
        raise
    if options.banner:
        print_banner(connection, options, provider)
    return connection


def close_connection(connection: Connection, http: httpx.Client) -> dict[str, Any]:
    """Release this client; stop a server it started once no one else is attached.

    Returns what happened: ``{"released", "attached", "stopped"}``.
    """

    remaining: int | None = None
    if connection.client_id is not None:
        try:
            response = http.delete(f"/v1/server/clients/{connection.client_id}")
            if response.is_success:
                remaining = int(response.json().get("attached", 0))
        except httpx.HTTPError:
            remaining = None  # the server is gone: there is nothing left to release
        connection.client_id = None
    outcome: dict[str, Any] = {"released": True, "attached": remaining, "stopped": None}
    if connection.started and remaining == 0:
        from clio_agent.serve_exit import await_server_exit  # noqa: PLC0415

        # The server stops itself when its last client leaves; wait for its tree.
        outcome["stopped"] = await_server_exit(connection.port)
    elif connection.started and remaining is None:
        from clio_agent import serve  # noqa: PLC0415

        # The release was not acknowledged: stop the server we started ourselves.
        outcome["stopped"] = serve.stop_server(connection.port)
    return outcome


def print_banner(connection: Connection, options: ClioOptions, provider: dict[str, Any]) -> None:
    """The one startup banner, on stderr."""

    how = (
        f"started a CLIO server on port {connection.port}"
        if connection.started
        else f"attached to the CLIO server at {connection.base_url}"
    )
    model = provider.get("model") or "the session default"
    tools = (
        "every tool and plugin is approved"
        if options.approval_mode in ("bypass", "spotter-ai")
        else f"plugins are approved, and any tool {options.approval_mode!r} would ask about "
        "is denied"
    )
    print(
        f"clio: {how} (model provider: {provider.get('provider') or 'configured'}, "
        f"{model}).\n"
        f"clio: nothing will be asked: {tools}; questions are answered 'the user is not "
        f"available'. Approval mode: {options.approval_mode}.",
        file=sys.stderr,
        flush=True,
    )


def print_trusted(plugin: str) -> None:
    """Announce a plugin trusted during a run, on stderr."""

    print(f"clio: trusted plugin {plugin} for this run.", file=sys.stderr, flush=True)


__all__ = [
    "Connection",
    "ProviderNotConfigured",
    "ServerSetupError",
    "ServerStartFailed",
    "ServerUnavailable",
    "close_connection",
    "open_connection",
    "print_banner",
    "print_trusted",
]
