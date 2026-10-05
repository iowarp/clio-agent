"""The local return helper must not leak codes into logs or active page content."""

import threading
from http.server import HTTPServer

import httpx
import pytest

from scripts.storage_oauth_return import ReturnHandler


def test_private_loopback_page_escapes_callback_and_does_not_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise an actual HTTP callback with markup in a code and no outgoing requests."""
    with HTTPServer(("127.0.0.1", 0), ReturnHandler) as server:
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        response = httpx.get(
            f"http://127.0.0.1:{server.server_port}/clio-storage-return",
            params={
                "code": "private-code</textarea><script>bad</script>",
                "state": "private-state",
            },
        )
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "private-code" in response.text
    assert "&amp;state=" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    output = capsys.readouterr()
    assert "private-code" not in output.out + output.err
