"""Validation and normalization of MCP server installation specs."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def stdio_server_spec(body: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized stdio spec or raise a typed 422 error."""

    command = body.get("command")
    args = body.get("args") or []
    env = body.get("env") or {}
    if not command:
        _bad_request("stdio transport requires 'command'")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        _bad_request("stdio transport 'args' must be a list of strings")
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        _bad_request("stdio transport 'env' must map names to string values")
    return {"transport": "stdio", "command": command, "args": args, "env": env}


def _bad_request(message: str) -> None:
    raise HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="bad_request",
                message=message,
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )
