"""Structured SDK errors mirroring the GACT §14 error taxonomy.

Every non-2xx HTTP response is raised as a :class:`ClioAPIError`
subclass chosen from the envelope's taxonomy tag (SPEC §14.2), with
the raw envelope fields attached. Transport-level failures raise
:class:`ClioConnectionError` instead.

Mapping rules encoded here (SPEC §6.0):

* The discriminator key is ``error``; v0.1 backends called it
  ``code`` — both are read.
* Legacy tolerance: clio still emits ``internal_error`` on some 404s
  and 422s. When the tag is unclassifiable (``internal_error`` /
  ``request_error`` / unknown), the HTTP status code decides the
  class instead.

Example:
    >>> try:
    ...     client.sessions.get("sess_missing")
    ... except NotFoundError as exc:
    ...     print(exc.status_code, exc.error, exc.recoverable)
"""

from __future__ import annotations

from typing import Any

import httpx

from clio_agent.sdk.types import ErrorInfo


class ClioSDKError(Exception):
    """Base class for every error the SDK raises on purpose."""


class ClioConnectionError(ClioSDKError):
    """The backend could not be reached (or the stream dropped and the
    reconnect budget was exhausted)."""


class ClioAPIError(ClioSDKError):
    """A GACT endpoint answered with an error envelope (SPEC §6.0/§14)."""

    def __init__(self, status_code: int, info: ErrorInfo) -> None:
        super().__init__(f"[{status_code}] {info.error}: {info.message}")
        self.status_code = status_code
        self.info = info

    @property
    def error(self) -> str:
        """Machine-readable taxonomy tag (open set — SPEC §14.2)."""

        return self.info.error

    @property
    def details(self) -> dict[str, Any]:
        return self.info.details

    @property
    def recoverable(self) -> bool:
        return self.info.recoverable

    @property
    def retry_after_s(self) -> int | None:
        return self.info.retry_after_s


class NotFoundError(ClioAPIError):
    """404 — resource missing (canonical tag ``not_found``)."""


class InvalidRequestError(ClioAPIError):
    """400/422 — ``validation_error`` / ``bad_request``."""


class ConflictError(ClioAPIError):
    """409 — state conflict (e.g. rollback while running)."""


class PermissionDeniedError(ClioAPIError):
    """401/403 (and the 409 ``permission_error`` on undeletable
    resources) — policy or scope rejected the request."""


class UnsupportedError(ClioAPIError):
    """405/501 — ``unsupported`` / ``not_implemented``."""


class ServiceUnavailableError(ClioAPIError):
    """503 readiness family — ``agent_not_available``,
    ``provider_configuring``, ``arc_unavailable``, …"""


class InternalServerError(ClioAPIError):
    """5xx unclassified backend failure."""


# Tag → class. Open set: unknown tags fall through to the status map.
_TAG_MAP: dict[str, type[ClioAPIError]] = {
    "not_found": NotFoundError,
    "validation_error": InvalidRequestError,
    "bad_request": InvalidRequestError,
    "conflict": ConflictError,
    "permission_error": PermissionDeniedError,
    "unsupported": UnsupportedError,
    "not_implemented": UnsupportedError,
    "agent_not_available": ServiceUnavailableError,
    "provider_configuring": ServiceUnavailableError,
    "arc_unavailable": ServiceUnavailableError,
    "compaction_unavailable": ServiceUnavailableError,
    "dependency_missing": ServiceUnavailableError,
    "upstream_unavailable": ServiceUnavailableError,
    "agent_unavailable": ServiceUnavailableError,
    "upstream_error": InternalServerError,
    "memory_update_failed": InternalServerError,
}

# Tags that carry no classification signal — the status decides.
_UNCLASSIFIED_TAGS = frozenset({"", "internal_error", "request_error"})

_STATUS_MAP: dict[int, type[ClioAPIError]] = {
    400: InvalidRequestError,
    401: PermissionDeniedError,
    403: PermissionDeniedError,
    404: NotFoundError,
    405: UnsupportedError,
    409: ConflictError,
    422: InvalidRequestError,
    429: ServiceUnavailableError,
    501: UnsupportedError,
    503: ServiceUnavailableError,
}


def _parse_error_info(response: httpx.Response) -> ErrorInfo:
    """Extract the §6.0 envelope; degrade honestly when absent."""

    try:
        body = response.json()
    except ValueError:
        body = None
    inner = body.get("error") if isinstance(body, dict) else None
    if isinstance(inner, dict):
        # v0.1 backends used `code` as the discriminator (SPEC §6.0).
        if "error" not in inner and "code" in inner:
            inner = {**inner, "error": inner["code"]}
        return ErrorInfo.model_validate(inner)
    # Not an envelope. Do not invent taxonomy: report the raw body under
    # an explicit non-taxonomy tag so callers can see what happened.
    return ErrorInfo(
        error="",
        message=response.text[:2000],
        details={"non_envelope_body": True},
        recoverable=False,
    )


def error_from_response(response: httpx.Response) -> ClioAPIError:
    """Build the typed error for a non-2xx response.

    Tag-first, status-fallback: the taxonomy tag picks the class when
    it is classifiable; the legacy ``internal_error``-on-404/422
    emissions (SPEC §6.0 drift note) fall back to the HTTP status.
    """

    info = _parse_error_info(response)
    cls = _TAG_MAP.get(info.error)
    if cls is None or info.error in _UNCLASSIFIED_TAGS:
        cls = _STATUS_MAP.get(
            response.status_code,
            InternalServerError if response.status_code >= 500 else ClioAPIError,
        )
    return cls(response.status_code, info)
