"""Tests for the removed ``clio-agent-api`` deprecation shim.

The legacy REST API server was removed in favor of the unified GACT server
(``clio-agent-gact``). The ``clio-agent-api`` console script now resolves to a
shim that prints a migration pointer and exits non-zero.
"""

from __future__ import annotations

import pytest

from clio_agent.ui import _deprecated_api


def test_main_exits_nonzero() -> None:
    """The shim must raise SystemExit with a non-zero code."""
    with pytest.raises(SystemExit) as excinfo:
        _deprecated_api.main()
    assert excinfo.value.code == 2
    assert excinfo.value.code != 0


def test_main_prints_gact_pointer(capsys: pytest.CaptureFixture[str]) -> None:
    """The shim must point the user at clio-agent-gact and /v1/health."""
    with pytest.raises(SystemExit):
        _deprecated_api.main()
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "clio-agent-gact" in combined
    assert "/v1/health" in combined
    assert "clio-agent-api" in combined


def test_legacy_api_module_is_gone() -> None:
    """The legacy REST API module must no longer be importable."""
    with pytest.raises(ModuleNotFoundError):
        __import__("clio_agent.ui.api")
