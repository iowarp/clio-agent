"""Scientific objects stream without a fixed size limit; the disk decides, not a cap."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from clio_agent.gact.storage.ndp_source import NDPSource
from tests.test_gact.test_ndp_source import dataset


@pytest.fixture
def streaming_peer() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            assert "Authorization" not in self.headers
            self.send_response(200)
            if self.path == "/large":
                self.send_header("Content-Length", str(513 * 1024 * 1024))
            elif self.path == "/huge":
                self.send_header("Content-Length", str(9 * 1024**3))
            self.end_headers()
            try:
                # A prefix-only reader closes the actual socket. This test
                # verifies streaming admission, not completion of a 513 MiB file.
                for _ in range(16):
                    self.wfile.write(b"x" * 65536)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_large_scientific_object_opens_without_eager_download(
    streaming_peer: tuple[str, list[str]],
) -> None:
    base, requests = streaming_peer
    selected = dataset(base + "/large")
    selected.resources[0].size = 513 * 1024 * 1024
    source = NDPSource(selected)
    with source.open_read(source.entries()[0]) as reader:
        assert reader.read(12) == b"x" * 12
        assert reader.raw.total <= 65536  # type: ignore[attr-defined]
    assert requests == ["/large"]
    with pytest.raises(PermissionError, match="read-only"):
        source.apply("data.csv", Path("unused"), None)


def test_a_resource_over_8_gib_still_streams_and_reads_by_range(
    streaming_peer: tuple[str, list[str]],
) -> None:
    # An 8 GiB cap refused even byte-range reads, which touch no disk.
    base, _ = streaming_peer
    selected = dataset(base + "/huge")
    selected.resources[0].size = 9 * 1024**3
    source = NDPSource(selected)
    with source.open_read(source.entries()[0]) as reader:
        assert reader.read(12) == b"x" * 12
    assert source.range_reader(source.entries()[0]).total_size() == 9 * 1024**3
