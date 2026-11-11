"""The LibreOffice download races TDF's mirrors and leaves one that turns slow.

Each mirror is a real local HTTP server with Range support, so the race, the throughput
floor, the resume and the truncation checks run over sockets as they do against TDF.
"""

from __future__ import annotations

import hashlib
import http.server
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from clio_agent.runtime.document_stack import office, office_fetch
from clio_agent.runtime.document_stack.process import DocumentError

NAME = "LibreOffice_test_deb.tar.gz"
PAYLOAD = hashlib.sha256(b"seed").digest() * (1024 * 1024 // 32)  # 1 MiB
CHECKSUM = hashlib.sha256(PAYLOAD).hexdigest()
# office.time is the time module itself: the servers keep the real sleep when it is patched.
_sleep = time.sleep


class Mirror:
    """One server's behaviour: its rate after ``slow_after`` bytes, truncation, status."""

    def __init__(
        self,
        *,
        rate: int | None = None,
        slow_after: int | None = None,
        slow_rate: int = 32 * 1024,
        truncate_at: int | None = None,
        status: int = 0,
        payload: bytes = PAYLOAD,
    ) -> None:
        self.rate = rate
        self.slow_after = slow_after
        self.slow_rate = slow_rate
        self.truncate_at = truncate_at
        self.status = status
        self.payload = payload
        self.ranges: list[str] = []
        self.server: http.server.ThreadingHTTPServer | None = None

    @property
    def base(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}/"


def _handler(mirror: Mirror, tdf: dict[str, object] | None) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if tdf is not None:
                self._redirector()
                return
            mirror.ranges.append(self.headers.get("Range") or "")
            if mirror.status:
                self.send_error(mirror.status)
                return
            data = mirror.payload
            start, end = 0, len(data) - 1
            requested = self.headers.get("Range")
            if requested:
                first, _, last = requested.removeprefix("bytes=").partition("-")
                start, end = int(first), int(last) if last else len(data) - 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            else:
                self.send_response(200)
            stop = end + 1 if mirror.truncate_at is None else min(end + 1, mirror.truncate_at)
            self.send_header("Content-Length", str(stop - start))
            self.end_headers()
            try:
                for offset in range(start, stop, 8192):
                    block = data[offset : min(offset + 8192, stop)]
                    if mirror.slow_after is not None and offset >= mirror.slow_after:
                        _sleep(len(block) / mirror.slow_rate)
                    elif mirror.rate:
                        _sleep(len(block) / mirror.rate)
                    self.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _redirector(self) -> None:
            assert tdf is not None
            if self.path.endswith(".meta4"):
                body = str(tdf["meta4"]).encode()
                self.send_response(int(tdf.get("meta4_status", 200)))  # type: ignore[call-overload]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            target = tdf["redirect"]
            assert isinstance(target, Mirror)
            self.send_response(302)
            self.send_header("Location", target.base + "pick/" + NAME)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def _serve(mirror: Mirror, tdf: dict[str, object] | None = None) -> Mirror:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(mirror, tdf))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    mirror.server = server
    return mirror


def _metalink(mirrors: list[Mirror], checksum: str = CHECKSUM, size: int = len(PAYLOAD)) -> str:
    urls = "".join(
        f'<url location="us" priority="{index + 1}">{m.base}tdf/{NAME}</url>'
        for index, m in enumerate(mirrors)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
        f'<file name="{NAME}"><size>{size}</size><hash type="sha-256">{checksum}</hash>'
        '<pieces length="262144" type="sha-1"><hash>00</hash></pieces>'
        f"{urls}</file></metalink>"
    )


class Fixture:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.started: list[Mirror] = []
        self.tdf: dict[str, object] = {}
        self.archive = self.add(Mirror(slow_after=0, slow_rate=8 * 1024))

    def add(self, mirror: Mirror) -> Mirror:
        self.started.append(_serve(mirror))
        return mirror

    def setup(self, mirrors: list[Mirror], *, redirect: Mirror | None = None) -> str:
        self.tdf.update(meta4=_metalink(mirrors), redirect=redirect or mirrors[0])
        redirector = _serve(Mirror(), self.tdf)
        self.started.append(redirector)
        self.monkeypatch.setattr(office_fetch, "_TDF", redirector.base)
        self.monkeypatch.setattr(office_fetch, "_TDF_ARCHIVE", self.archive.base)
        return f"{redirector.base}libreoffice/stable/{office.VERSION}/deb/x86_64/{NAME}"

    def close(self) -> None:
        for mirror in self.started:
            assert mirror.server is not None
            mirror.server.shutdown()
            mirror.server.server_close()


@pytest.fixture
def mirrors(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fixture]:
    monkeypatch.setattr(office_fetch, "_RACE_BYTES", 64 * 1024)
    monkeypatch.setattr(office_fetch, "FLOOR_WINDOW_S", 0.5)
    monkeypatch.setattr(office_fetch.time, "sleep", lambda seconds: None)  # retry backoff
    fixture = Fixture(monkeypatch)
    yield fixture
    fixture.close()


def _records(caplog: pytest.LogCaptureFixture, reason: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if f"reason={reason}" in r.getMessage()]


def test_race_keeps_the_winners_bytes_and_continues_there(
    mirrors: Fixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    slow = mirrors.add(Mirror(slow_after=0))
    fast = mirrors.add(Mirror())
    missing = mirrors.add(Mirror(status=404))
    url = mirrors.setup([slow, fast, missing], redirect=slow)
    caplog.set_level(logging.INFO, logger=office_fetch.logger.name)

    target = tmp_path / "office.part"
    office_fetch.download(url, target, CHECKSUM)

    assert target.read_bytes() == PAYLOAD
    assert fast.ranges == [f"bytes=0-{64 * 1024 - 1}", f"bytes={64 * 1024}-"]
    assert missing.ranges == [f"bytes=0-{64 * 1024 - 1}"]
    (race,) = _records(caplog, "office_mirror_race")
    assert f"winner={fast.base[7:-1]}" in race
    assert f"{missing.base[7:-1]}:failed" in race
    (done,) = _records(caplog, "office_download_done")
    assert f"mirror={fast.base[7:-1]}" in done
    assert f"bytes={len(PAYLOAD)}" in done


def test_a_mirror_that_turns_slow_is_left_and_the_bytes_resume(
    mirrors: Fixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Fast for the race, then a trickle: the CI failure mode (mirror.fcix.net from Azure).
    turns_slow = mirrors.add(Mirror(rate=4 << 20, slow_after=128 * 1024))
    steady = mirrors.add(Mirror(rate=4 << 20))
    url = mirrors.setup([turns_slow, steady])
    mirrors.monkeypatch.setattr(office_fetch, "_RACE_MIRRORS", 1)
    caplog.set_level(logging.INFO, logger=office_fetch.logger.name)

    target = tmp_path / "office.part"
    began = time.monotonic()
    office_fetch.download(url, target, CHECKSUM)

    assert target.read_bytes() == PAYLOAD
    assert time.monotonic() - began < 10  # the slow mirror alone would take 28 s
    (switch,) = _records(caplog, "office_mirror_switch")
    assert f"from={turns_slow.base[7:-1]}" in switch
    resumed = [r for r in steady.ranges if r.startswith("bytes=") and r != "bytes=0-"]
    offset = int(resumed[-1].removeprefix("bytes=").rstrip("-"))
    assert offset >= 128 * 1024  # resumed, not restarted
    (done,) = _records(caplog, "office_download_done")
    assert "switches=1" in done and "retries=0" in done


def test_a_body_that_ends_early_resumes_on_the_next_source(
    mirrors: Fixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    short = mirrors.add(Mirror(truncate_at=300 * 1024))
    whole = mirrors.add(Mirror(slow_after=0, slow_rate=4 * 1024 * 1024))
    url = mirrors.setup([short, whole])
    mirrors.monkeypatch.setattr(office_fetch, "_RACE_MIRRORS", 1)
    caplog.set_level(logging.INFO, logger=office_fetch.logger.name)

    target = tmp_path / "office.part"
    office_fetch.download(url, target, CHECKSUM)

    assert target.read_bytes() == PAYLOAD
    (retry,) = _records(caplog, "office_download_retry")
    assert f"ended at {300 * 1024} of {len(PAYLOAD)} bytes" in retry
    assert f"bytes={300 * 1024}-" in [*whole.ranges, *mirrors.archive.ranges]


def test_without_a_metalink_the_redirector_alone_is_used(
    mirrors: Fixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    picked = mirrors.add(Mirror())
    url = mirrors.setup([picked])
    mirrors.tdf["meta4_status"] = 503
    target = tmp_path / "office.part"
    office_fetch.download(url, target, CHECKSUM)
    assert target.read_bytes() == PAYLOAD
    assert picked.ranges == [""]
    assert _records(caplog, "office_metalink_unavailable")


def test_a_metalink_that_disagrees_with_the_pin_is_ignored(
    mirrors: Fixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    other = mirrors.add(Mirror())
    picked = mirrors.add(Mirror())
    url = mirrors.setup([other], redirect=picked)
    mirrors.tdf["meta4"] = _metalink([other], checksum="0" * 64)
    office_fetch.download(url, tmp_path / "office.part", CHECKSUM)
    assert other.ranges == []
    assert picked.ranges == [""]
    assert "differs from the pin" in _records(caplog, "office_metalink_unavailable")[0]


def test_wrong_bytes_from_every_source_fail_closed(mirrors: Fixture, tmp_path: Path) -> None:
    wrong = PAYLOAD[::-1]
    sources = [mirrors.add(Mirror(payload=wrong)) for _ in range(2)]
    mirrors.archive.payload = wrong
    url = mirrors.setup(sources)
    target = tmp_path / "office.part"
    limit = office_fetch._MAX_ATTEMPTS
    with pytest.raises(DocumentError, match=f"all {limit} download attempts failed"):
        office_fetch.download(url, target, CHECKSUM)
    assert not target.exists()


@pytest.mark.parametrize("growth", ["slow", "fast"])
def test_a_slow_installer_prefetch_is_left_for_the_apps_own_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, growth: str
) -> None:
    folder = tmp_path / "downloads"
    folder.mkdir()
    partial = folder / (NAME + ".part")
    partial.write_bytes(b"")
    monkeypatch.setenv(office.INSTALLER_DOWNLOADS, str(folder))
    monkeypatch.setattr(office_fetch, "FLOOR_WINDOW_S", 0.5)
    probes: list[str] = []

    def mirror_rate(url: str, checksum: str) -> tuple[float, int]:
        probes.append(url)
        return 1_000_000.0, len(PAYLOAD)

    monkeypatch.setattr(office, "_mirror_rate", mirror_rate)
    stop = threading.Event()

    def write() -> None:
        step, pause = (512, 0.05) if growth == "slow" else (64 * 1024, 0.05)
        with partial.open("ab") as output:
            for offset in range(0, len(PAYLOAD), step):
                if stop.is_set():
                    return
                output.write(PAYLOAD[offset : offset + step])
                output.flush()
                _sleep(pause)
        partial.rename(folder / NAME)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        found = office.installer_download(NAME, CHECKSUM, "https://example.invalid/" + NAME)
    finally:
        stop.set()
        writer.join(5)
    assert probes == ["https://example.invalid/" + NAME]
    assert found == (None if growth == "slow" else folder / NAME)
