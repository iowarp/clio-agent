"""Tests for the ``versions`` block on ``GET /v1/capabilities`` (slice A5).

:mod:`clio_agent.gact.version_info` is exercised both directly (fast,
in-process unit coverage of the marketplace-pin resolution) and through the
live route via :class:`fastapi.testclient.TestClient` (house convention for
GACT route coverage), per the module's own contract: never raise into
capabilities, and never shell out (slice A4's ``git ls-remote`` update check
stays a separate, explicit call -- capabilities must stay cheap/offline).
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent import __version__
from clio_agent.gact import version_info as version_info_module
from clio_agent.gact.app import build_app
from clio_agent.gact.version_info import build_version_info

_DEFAULT_ROW = {
    "id": "src_default",
    "source": "https://github.com/iowarp/clio-agent-marketplace.git",
    "ref": "main",
    "commit": "abc123installedcommit",
    "pinned_commit": "def456pinnedcommit",
    "is_default": True,
}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_capabilities_exposes_versions_block(client: TestClient) -> None:
    """``GET /v1/capabilities`` carries a ``versions`` block with real scalars."""

    resp = client.get("/v1/capabilities")
    assert resp.status_code == 200, resp.text
    versions = resp.json()["versions"]

    assert versions is not None
    assert versions["clio_agent"] == __version__
    assert versions["python"] == platform.python_version()
    assert versions["backend_build"]
    assert versions["gact_contract"]
    # No blueprint source registered in this isolated test ledger.
    assert versions["marketplace"] is None


def test_marketplace_version_reflects_installed_source_row(monkeypatch) -> None:
    """The default (``is_default``) source row populates ``marketplace``."""

    monkeypatch.setattr(version_info_module, "load_agent_blueprint_sources", lambda: [_DEFAULT_ROW])

    info = build_version_info()

    assert info.marketplace is not None
    assert info.marketplace.source == _DEFAULT_ROW["source"]
    assert info.marketplace.ref == "main"
    assert info.marketplace.installed_commit == "abc123installedcommit"
    assert info.marketplace.pinned_commit == "def456pinnedcommit"
    assert info.marketplace.source_id == "src_default"


def test_marketplace_version_ignores_non_default_rows(monkeypatch) -> None:
    """A registered source without ``is_default`` never surfaces as the pin."""

    row = {**_DEFAULT_ROW, "is_default": False, "id": "src_user_added"}
    monkeypatch.setattr(version_info_module, "load_agent_blueprint_sources", lambda: [row])

    info = build_version_info()

    assert info.marketplace is None


def test_marketplace_version_none_when_no_source_row(monkeypatch) -> None:
    """An empty ledger types ``marketplace=None``, never an empty placeholder row."""

    monkeypatch.setattr(version_info_module, "load_agent_blueprint_sources", lambda: [])

    info = build_version_info()

    assert info.marketplace is None


def test_marketplace_version_none_when_ledger_read_fails(monkeypatch) -> None:
    """A broken ledger read degrades to ``marketplace=None``, never raises."""

    def _boom() -> list[dict[str, Any]]:
        raise OSError("ledger unreadable")

    monkeypatch.setattr(version_info_module, "load_agent_blueprint_sources", _boom)

    info = build_version_info()

    assert info.marketplace is None


def test_blank_source_fields_type_as_unknown(monkeypatch) -> None:
    """A blank field on the default row types as ``"unknown"``, not ``""``."""

    row = {"id": "", "source": "", "ref": "", "commit": "", "pinned_commit": "", "is_default": True}
    monkeypatch.setattr(version_info_module, "load_agent_blueprint_sources", lambda: [row])

    info = build_version_info()

    assert info.marketplace is not None
    assert info.marketplace.source == "unknown"
    assert info.marketplace.ref == "unknown"
    assert info.marketplace.installed_commit == "unknown"
    assert info.marketplace.pinned_commit == "unknown"
    assert info.marketplace.source_id == "unknown"


def test_capabilities_stays_offline(monkeypatch, client: TestClient) -> None:
    """The versions probe never shells out -- capabilities stays a pure read.

    ``subprocess.run``/``Popen`` are monkeypatched to raise so any accidental
    ``git`` probe on the capabilities path (e.g. an ls-remote update check,
    which slice A4 keeps as an explicit separate call) would fail the request;
    the route must still 200 with a populated ``versions`` block.
    """

    calls: list[str] = []

    def _boom_run(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("run")
        raise AssertionError("build_version_info must not shell out via subprocess.run")

    def _boom_popen(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("popen")
        raise AssertionError("build_version_info must not shell out via subprocess.Popen")

    monkeypatch.setattr(subprocess, "run", _boom_run)
    monkeypatch.setattr(subprocess, "Popen", _boom_popen)

    resp = client.get("/v1/capabilities")

    assert resp.status_code == 200, resp.text
    assert resp.json()["versions"] is not None
    assert calls == []
