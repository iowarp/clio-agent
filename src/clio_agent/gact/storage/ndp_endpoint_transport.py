"""Finite HTTP requests over direct URLs or verified SSH direct-tcpip channels."""

from __future__ import annotations

import http.client
import io
from collections.abc import Iterator
from itertools import chain
from typing import Any, cast

import httpx
import paramiko

from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.storage.ndp_endpoint_models import EndpointConnectionInput
from clio_agent.gact.storage.sftp import connect_profile, connect_route


class _ChannelReader(io.RawIOBase):
    def __init__(self, channel: paramiko.Channel) -> None:
        self.channel = channel

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self.channel.recv(len(buffer))
        buffer[: len(data)] = data
        return len(data)


class _ChannelSocket:
    """Give http.client a standard buffered stream that supports incremental read1."""

    def __init__(self, channel: paramiko.Channel) -> None:
        self.channel = channel

    def makefile(self, mode: str) -> io.BufferedReader:
        return io.BufferedReader(_ChannelReader(self.channel))

    def sendall(self, data: bytes) -> None:
        self.channel.sendall(data)

    def close(self) -> None:
        """Leave channel closure to the response owner.

        HTTPConnection closes its socket as soon as it sees an HTTP/1.0 or
        Connection: close response. Unlike a real socket makefile, our reader
        has no independent descriptor; closing here would truncate the body.
        """


class _SSHBody(httpx.SyncByteStream):
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: Any,
        client: Any,
        channel: paramiko.Channel,
    ) -> None:
        self.response, self.connection, self.client = response, connection, client
        self.channel = channel

    def __iter__(self) -> Iterator[bytes]:
        while chunk := self.response.read1(8192):
            yield chunk

    def close(self) -> None:
        try:
            self.response.close()
            self.connection.close()
        finally:
            try:
                self.channel.close()
            finally:
                self.client.close()


class EndpointSSHTransport(httpx.BaseTransport):
    """Reuse CLIO's shared SSH routes without a listening local tunnel."""

    def __init__(self, profile: str | SshRoute, timeout: float) -> None:
        self.profile, self.timeout = profile, timeout

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Open exactly one channel and close its SSH owner with the response."""
        client = (
            connect_route(self.profile)
            if isinstance(self.profile, SshRoute)
            else connect_profile(self.profile)
        )
        connection = http.client.HTTPConnection(request.url.host, request.url.port or 80)
        channel = None
        try:
            transport = client.get_transport()
            if transport is None:
                raise OSError("The SSH connection closed before opening the Endpoint")
            channel = transport.open_channel(
                "direct-tcpip",
                (request.url.host, request.url.port or 80),
                ("127.0.0.1", 0),
                timeout=self.timeout,
            )
            channel.settimeout(self.timeout)
            connection.sock = cast(Any, _ChannelSocket(channel))
            if not isinstance(request.stream, httpx.SyncByteStream):
                raise ValueError("Endpoint SSH requests require a synchronous byte stream")
            chunks = iter(request.stream)
            body: Iterator[bytes] | None = chunks
            if (
                "content-length" not in request.headers
                and "transfer-encoding" not in request.headers
            ):
                # An empty iterator makes http.client frame even a body-free GET
                # as chunked. A closing peer may reset with that terminator unread.
                # Peek once without buffering streamed uploads or changing framing.
                first_chunk = next((chunk for chunk in chunks if chunk), None)
                body = chain((first_chunk,), chunks) if first_chunk is not None else None
            connection.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                body=body,
                headers=dict(request.headers),
                encode_chunked="content-length" not in request.headers,
            )
            response = connection.getresponse()
            return httpx.Response(
                response.status,
                headers=response.getheaders(),
                stream=_SSHBody(response, connection, client, channel),
                request=request,
                extensions={"ndp_ssh_channel": channel},
            )
        except BaseException as exc:
            try:
                connection.close()
                if channel is not None:
                    channel.close()
            finally:
                client.close()
            if isinstance(exc, TimeoutError):
                raise TimeoutError(
                    f"NDP Endpoint did not send response headers within {self.timeout:g} seconds. "
                    "The request budget includes server polling and identity validation. "
                    "Use a longer finite request budget or a shorter poll."
                ) from exc
            raise


def endpoint_client(connection: EndpointConnectionInput, seconds: float = 15) -> httpx.Client:
    """Create a request owner; do not inherit proxy credentials or follow redirects."""
    route = connection.ssh_route or connection.ssh_profile
    transport = EndpointSSHTransport(route, seconds) if route else None
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(seconds, connect=10),
        follow_redirects=False,
        trust_env=False,
    )
