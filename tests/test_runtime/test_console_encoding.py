"""Console output never crashes a cp1252 (non-UTF-8) console.

Regression: ``CLIO_ARC_STORE=local`` printed the ``⚑ DEGRADED TO LOCAL BACKEND``
banner to a cp1252 stdout opened with ``errors="strict"`` (a redirected Windows
console), the ``UnicodeEncodeError`` aborted ARC boot (``arc_boot_failed``), and
every ``trace`` line (``⚑ TAG ...``) carries the same glyph.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
from typing import Any

import pytest

from clio_agent.arc import init_degradation
from clio_agent.runtime import trace
from clio_agent.runtime.console_encoding import ensure_utf8_console


def _cp1252_strict() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)


def _written(stream: Any) -> str:
    stream.flush()
    return stream.buffer.getvalue().decode("utf-8")


def _install_cp1252_console(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
    """Install strict cp1252 stdout/stderr, exactly what a redirected Windows console gets.

    Called from the test body, not a fixture: pytest's capture re-assigns
    ``sys.stdout`` / ``sys.stderr`` between fixture setup and the test call.
    """

    out, err = _cp1252_strict(), _cp1252_strict()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def test_the_unconfigured_stream_really_crashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: without the fix the glyph cannot be written to this stream."""

    _install_cp1252_console(monkeypatch)
    with pytest.raises(UnicodeEncodeError):
        print("⚑ probe")


def test_the_local_store_banner_and_trace_lines_survive_a_cp1252_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, err = _install_cp1252_console(monkeypatch)
    # A handler built BEFORE the reconfigure (as uvicorn's and trace's are) holds
    # the same stream object, which reconfigure() changes in place.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    clio_logger = logging.getLogger("clio_agent")
    clio_logger.addHandler(handler)
    monkeypatch.setattr(init_degradation, "_local_banner_emitted", False)
    monkeypatch.setattr(trace, "EVENT_ON", True)
    monkeypatch.setattr(trace, "_ONLY", None)
    try:
        ensure_utf8_console()
        init_degradation.warn_local_backend_selected()
        trace.event("CONSOLE-PROBE", "arrow=%s section=%s", "→", "§")
    finally:
        clio_logger.removeHandler(handler)

    assert sys.stdout.encoding == "utf-8"
    assert sys.stderr.encoding == "utf-8"
    stdout_text = _written(out)
    assert f"⚑ {init_degradation.LOCAL_BACKEND_BANNER}" in stdout_text
    assert "Unit-test convenience ONLY — never" in stdout_text
    stderr_text = _written(err)
    assert "⚑ CONSOLE-PROBE arrow=→ section=§" in stderr_text


def test_ensure_is_idempotent_and_replaces_unencodable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _err = _install_cp1252_console(monkeypatch)
    ensure_utf8_console()
    ensure_utf8_console()
    # A lone surrogate (e.g. a surrogateescape'd filename) is the one thing UTF-8
    # itself cannot encode; errors="replace" writes "?" instead of raising.
    print("path=\udcff ok")

    assert sys.stdout.errors == "replace"
    assert "path=? ok" in _written(out)


class _FrozenStream(io.StringIO):
    """A cp1252 stream with no ``reconfigure`` (a third-party wrapper)."""

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "cp1252"

    def __getattribute__(self, name: str) -> Any:
        if name == "reconfigure":
            raise AttributeError(name)
        return super().__getattribute__(name)


def test_a_stream_that_cannot_be_reconfigured_is_reported_not_skipped_silently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    frozen = _FrozenStream()
    monkeypatch.setattr(sys, "stdout", frozen)
    monkeypatch.setattr(sys, "stderr", _cp1252_strict())
    logger = logging.getLogger("clio_agent.runtime.console_encoding")
    logger.addHandler(caplog.handler)
    try:
        ensure_utf8_console()
    finally:
        logger.removeHandler(caplog.handler)

    messages = [record.getMessage() for record in caplog.records]
    assert any("reason=console_encoding_unchanged" in m and "stream=stdout" in m for m in messages)
    assert sys.stderr.encoding == "utf-8"


def test_installing_the_console_log_handler_reconfigures_the_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``trace.configure()`` is the console setup both entry paths run first.

    ``clio-agent`` (``ui/cli.py::main``) and the GACT server
    (``gact/app.py::run_server``) call ``trace.configure()`` before building the
    server or ARC, so its handler install must leave the streams UTF-8.
    """

    out, err = _install_cp1252_console(monkeypatch)
    clio_logger = logging.getLogger("clio_agent")
    monkeypatch.setattr(clio_logger, "handlers", [])
    monkeypatch.setattr(clio_logger, "propagate", clio_logger.propagate)
    monkeypatch.setattr(clio_logger, "level", clio_logger.level)

    trace.configure(level="low", only=[])
    trace.event("CONSOLE-PROBE", "installed")

    assert sys.stdout.encoding == "utf-8"
    assert sys.stderr.encoding == "utf-8"
    assert "WARNING | ⚑ CONSOLE-PROBE installed" in _written(err)
    assert _written(out) == ""


_BANNER_SCRIPT = """
import sys
{ensure}
from clio_agent.arc.init_degradation import warn_local_backend_selected
warn_local_backend_selected()
"""


@pytest.mark.parametrize("fixed", [True, False])
def test_a_real_cp1252_process_prints_the_banner_only_when_fixed(fixed: bool) -> None:
    """A real interpreter whose std streams are strict cp1252 pipes."""

    ensure = (
        "from clio_agent.runtime.console_encoding import ensure_utf8_console\nensure_utf8_console()"
        if fixed
        else ""
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [sys.executable, "-c", _BANNER_SCRIPT.format(ensure=ensure)],
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    if fixed:
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert "⚑ DEGRADED TO LOCAL BACKEND" in proc.stdout.decode("utf-8")
    else:
        assert proc.returncode != 0
        assert b"UnicodeEncodeError" in proc.stderr
