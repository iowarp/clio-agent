"""Small payment gateway fixture used by the tool UI qualification campaign."""

from collections.abc import Callable


def forward_charge(
    request_headers: dict[str, str],
    send: Callable[[dict[str, str]], int],
    max_attempts: int,
) -> int:
    """Forward one charge request and retry a transient upstream failure."""
    status = send(request_headers)
    for _ in range(1, max_attempts):
        if status < 500:
            return status
        retry_headers = {
            key: value
            for key, value in request_headers.items()
            if key.lower() != "authorization"
        }
        status = send(retry_headers)
    return status

