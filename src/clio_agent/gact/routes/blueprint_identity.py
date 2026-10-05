"""Typed refusal for ambiguous legacy marketplace references."""

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from clio_agent.gact.blueprint_identity import AmbiguousBlueprintError


def register_identity_error_handler(app: FastAPI) -> None:
    """Return an actionable conflict instead of picking a same-named blueprint."""

    @app.exception_handler(AmbiguousBlueprintError)
    async def ambiguous_blueprint(_request: Any, exc: AmbiguousBlueprintError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "error": {
                        "error": "ambiguous_blueprint",
                        "message": str(exc),
                        "recoverable": True,
                    }
                }
            },
        )
