"""An unreadable stored transcript is a typed 503, never a bare 500 or an empty list (#1172).

clio-core can list a transcript blob and still fail to serve it. Both transcript
read paths (the whole ledger, :func:`~clio_agent.gact.transcript_projection.materialize_ledger`,
and a bounded page, :func:`~clio_agent.gact.transcript_pages.read_message_page`) run
under :func:`typed_transcript_read`, which turns those blob failures into the
existing :class:`~clio_agent.gact.messages.LedgerReadError` with one of two causes:

* ``storage_unreachable`` -- the store did not answer the read; retrying may work;
* ``transcript_corrupt`` -- the stored bytes cannot be decoded even after the
  framed-length retry; retrying will not help.

:func:`install_unreadable_transcript_handler` answers ``503 messages_unavailable``
with that cause on every route that reads a transcript. Blob and tag names stay in
the server log, never in the response.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from clio_agent.arc.blob_frame import CORRUPT_BLOB_ERRORS, TRANSIENT_BLOB_ERRORS
from clio_agent.gact.messages import LedgerReadError

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: The store did not answer the read; retrying may succeed.
STORAGE_UNREACHABLE = "storage_unreachable"
#: The stored transcript cannot be decoded; retrying will not help.
TRANSCRIPT_CORRUPT = "transcript_corrupt"
#: The GACT error code a client sees.
MESSAGES_UNAVAILABLE = "messages_unavailable"


def _unreadable(session_id: str, cause: str, exc: Exception) -> LedgerReadError:
    logger.warning(
        "transcript unreadable reason=%s cause=%s session=%s detail=%s",
        MESSAGES_UNAVAILABLE,
        cause,
        session_id,
        exc,
    )
    error = LedgerReadError(f"session {session_id} transcript is unreadable ({cause})")
    error.cause = cause
    return error


@contextmanager
def typed_transcript_read(session_id: str) -> Iterator[None]:
    """Raise clio-core blob failures inside the block as a typed :class:`LedgerReadError`."""

    try:
        yield
    except TRANSIENT_BLOB_ERRORS as exc:
        raise _unreadable(session_id, STORAGE_UNREACHABLE, exc) from exc
    except CORRUPT_BLOB_ERRORS as exc:
        raise _unreadable(session_id, TRANSCRIPT_CORRUPT, exc) from exc


def install_unreadable_transcript_handler(app: "FastAPI") -> None:
    """Serve :class:`LedgerReadError` app-wide as a typed 503 ``messages_unavailable``.

    The session exists and its transcript is known to be stored, so this is neither
    a 404 nor an empty transcript. A corrupt transcript is not recoverable by retry;
    a client can offer to remove the session instead.
    """

    from fastapi import Request  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo  # noqa: PLC0415

    @app.exception_handler(LedgerReadError)
    async def _messages_unavailable(request: Request, exc: LedgerReadError) -> JSONResponse:
        corrupt = exc.cause == TRANSCRIPT_CORRUPT
        info = ErrorInfo(
            error=MESSAGES_UNAVAILABLE,
            message=(
                "This conversation's stored messages are damaged and cannot be read."
                if corrupt
                else "This conversation's stored messages cannot be read right now. Retry shortly."
            ),
            details={"session_id": request.path_params.get("sid"), "cause": exc.cause or None},
            recoverable=not corrupt,
        )
        return JSONResponse(
            status_code=503,
            content=ErrorEnvelope(error=info).model_dump(exclude_none=True),
            headers={} if corrupt else {"Retry-After": "1"},
        )


__all__ = [
    "MESSAGES_UNAVAILABLE",
    "STORAGE_UNREACHABLE",
    "TRANSCRIPT_CORRUPT",
    "install_unreadable_transcript_handler",
    "typed_transcript_read",
]
