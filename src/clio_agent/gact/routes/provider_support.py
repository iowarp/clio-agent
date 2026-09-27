"""Provider support restore routes: what CLIO brought back after an update, and retry.

* ``GET /v1/providers/support/restores`` -- every provider's latest restore job
  (``restoring`` -> ``restored`` | ``failed`` with a typed error), whether a
  pass is running, and any install that could not be recorded (that support
  would not survive the next update).
* ``POST /v1/providers/support/restore`` -- plan again against the running
  environment and restore what is still missing (the retry); 409 while a pass
  is running.

The restore is :mod:`clio_agent.providers.support_restore`; the record is
:mod:`clio_agent.providers.support_record`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.providers.support_record import last_record_failure
from clio_agent.providers.support_restore import RESTORER


def _restores_wire() -> dict[str, Any]:
    return {
        "running": RESTORER.running,
        "restores": [job.to_wire() for job in RESTORER.jobs()],
        "record_failures": [outcome.to_wire() for outcome in last_record_failure()],
        "record_error": RESTORER.record_error,
    }


def register_provider_support_routes(app: FastAPI) -> None:
    """Register the provider support restore routes."""

    @app.get("/v1/providers/support/restores")
    async def provider_support_restores() -> dict[str, Any]:
        """The latest restore of every provider's recorded support."""
        return _restores_wire()

    @app.post("/v1/providers/support/restore", status_code=202)
    async def restore_provider_support() -> dict[str, Any]:
        """Restore recorded provider support the running environment lacks (retry)."""
        from clio_agent.gact import provider_support_boot  # noqa: PLC0415

        try:
            provider_support_boot.plan_and_start(app)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="provider_support_restore_running", message=str(exc), recoverable=True
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        return _restores_wire()


__all__ = ["register_provider_support_routes"]
