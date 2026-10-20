"""Stream controls must survive large previews without inventing a continuation."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from clio_agent.gact.storage.ndp_endpoint_capture import stream_continuation
from clio_agent.gact.storage.ndp_endpoint_errors import raise_endpoint_error
from clio_agent.gact.storage.ndp_endpoint_models import EndpointRequest


def test_complete_large_batch_has_separate_control_metadata() -> None:
    """The next cursor remains usable even when the record preview is truncated."""
    request = EndpointRequest(
        operation_id="GET /streams/{stream_id}/segment", parameters={"stream_id": "stream_a"}
    )
    raw = json.dumps(
        {
            "stream_id": "stream_a",
            "complete": True,
            "count": 100,
            "records": [{"value": "data" * 100} for _ in range(100)],
            "next_cursor": "next-checkpoint",
            "next_positions": {"0": 100},
        }
    ).encode()
    assert b"next-checkpoint" not in raw[:16000]
    result = stream_continuation(raw, "completed", request)
    assert result is not None and result.next_cursor == "next-checkpoint"
    assert result.count == 100 and result.next_positions == {"0": 100}
    assert stream_continuation(raw, "byte_limit", request) is None
    assert stream_continuation(raw[:-100], "completed", request) is None
    other = request.model_copy(update={"parameters": {"stream_id": "different"}})
    assert stream_continuation(raw, "completed", other) is None


@pytest.mark.parametrize("token", [None, "valid-saved-token"])
def test_authentication_errors_distinguish_anonymous_and_saved_account(token: str | None) -> None:
    """Anonymous denial is not evidence that the account issuer or roles are broken."""
    request = EndpointRequest(operation_id="GET /user/info")
    response = httpx.Response(
        401,
        json={"detail": "Not authenticated"},
        request=httpx.Request("GET", "https://endpoint.test/user/info"),
    )
    with pytest.raises(PermissionError) as failure:
        raise_endpoint_error(response, request, time.monotonic() + 2, token)
    assert ("saved NDP account" if token is None else "access policy") in str(failure.value)


def test_readiness_error_retains_the_failing_dependency() -> None:
    """Preserve the actual deployed service cause without blaming the NDP account."""
    request = EndpointRequest(operation_id="GET /ready")
    response = httpx.Response(
        503,
        json={
            "checks": {
                "catalog": {"status": "up"},
                "kafka": {"status": "down", "error": "Invalid Kafka client configuration"},
            }
        },
        request=httpx.Request("GET", "https://endpoint.test/ready"),
    )
    with pytest.raises(ValueError, match="kafka: Invalid Kafka client configuration"):
        raise_endpoint_error(response, request, time.monotonic() + 2)
