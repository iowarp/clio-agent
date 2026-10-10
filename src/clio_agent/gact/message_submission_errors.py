"""HTTP error projections for message acceptance."""

from fastapi import HTTPException

from clio_agent.gact.a2ui_capabilities import A2UICapabilitiesError
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def session_not_found(sid: str) -> HTTPException:
    """Build the typed session not found response."""
    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def identity_conflict(sid: str, message_id: str) -> HTTPException:
    """Build the typed identity conflict response."""
    return HTTPException(
        status_code=409,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="message_identity_conflict",
                message="client message id already names another message",
                details={"session_id": sid, "message_id": message_id},
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def a2ui_client_metadata_error(exc: A2UICapabilitiesError) -> HTTPException:
    """Build the typed a2ui client metadata error response."""
    return HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=exc.reason, message=str(exc), recoverable=True)
        ).model_dump(exclude_none=True),
    )
