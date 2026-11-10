"""On macOS, CLIO's uv children never fall back to the Command Line Tools' python3 stub."""

from __future__ import annotations

import os

import pytest

import clio_agent


def test_macos_defaults_uv_to_managed_pythons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UV_MANAGED_PYTHON", raising=False)
    monkeypatch.setattr("clio_agent.sys.platform", "darwin")

    clio_agent._default_uv_managed_python_on_macos()

    assert os.environ["UV_MANAGED_PYTHON"] == "1"


def test_an_explicit_preference_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UV_MANAGED_PYTHON", "0")
    monkeypatch.setattr("clio_agent.sys.platform", "darwin")

    clio_agent._default_uv_managed_python_on_macos()

    assert os.environ["UV_MANAGED_PYTHON"] == "0"


def test_other_platforms_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UV_MANAGED_PYTHON", raising=False)
    monkeypatch.setattr("clio_agent.sys.platform", "linux")

    clio_agent._default_uv_managed_python_on_macos()

    assert "UV_MANAGED_PYTHON" not in os.environ
