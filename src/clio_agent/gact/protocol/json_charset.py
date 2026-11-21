"""Declare UTF-8 on every JSON response.

JSON is UTF-8 (RFC 8259), but Starlette sends ``application/json`` without a charset,
and some clients then decode the body as ISO-8859-1: Windows PowerShell 5.1's
``Invoke-RestMethod`` turned an ask's "8°" into "8Â°" and "289 × 135" into "289 Ã 135".
Naming the charset makes every client read the text the server sent.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def with_utf8_charset(value: bytes) -> bytes:
    """``value`` with ``charset=utf-8`` when it is a JSON media type that names none."""
    media = value.split(b";", 1)[0].strip().lower()
    if not (media == b"application/json" or media.endswith(b"+json")):
        return value
    if b"charset=" in value.lower():
        return value
    return value + b"; charset=utf-8"


class JsonCharsetMiddleware:
    """Rewrites only the response's ``content-type`` header; bodies stream untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_charset(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (name, with_utf8_charset(value) if name.lower() == b"content-type" else value)
                    for name, value in message.get("headers", [])
                ]
            await send(message)

        await self.app(scope, receive, send_with_charset)


def install_json_charset(app: FastAPI) -> None:
    """Install last so it wraps every response, the error envelope's and CORS's too."""

    app.add_middleware(JsonCharsetMiddleware)
