"""Fixtures for the browser tests: the fixture site, a Chromium, and an isolated service.

The tests drive a real Chromium against the fixture site
(``tests/browser/fixtures/site/server.py``). They need the Chromium build the pinned
Playwright release names, found in CLIO's browser cache or under ``PLAYWRIGHT_BROWSERS_PATH``;
without it they skip rather than download 120 MB in a test run.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

SITE = Path(__file__).resolve().parents[2] / "browser" / "fixtures" / "site" / "server.py"


def _site_module() -> Any:
    spec = importlib.util.spec_from_file_location("browser_fixture_site", SITE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """The fixture site on a free port: ``{"a": url, "b": url, "port": n, "state": dir}``."""
    module = _site_module()
    state = tmp_path_factory.mktemp("fixture-site")
    server = module.FixtureServer(0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    yield {
        "a": f"http://a.fixture.localhost:{port}",
        "b": f"http://b.fixture.localhost:{port}",
        "port": port,
        "state": state,
        "server": server,
    }
    server.shutdown()
    server.server_close()


@pytest.fixture()
def tls_site(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """``https://c.fixture.localhost`` with its own self-signed certificate (addendum B)."""
    import shutil

    if shutil.which("openssl") is None:
        pytest.skip("the TLS fixture makes its certificate with openssl")
    module = _site_module()
    tls = module.TlsFixture(0, tmp_path / "tls-site")
    thread = threading.Thread(target=tls.server.serve_forever, daemon=True)
    thread.start()
    yield {
        "c": f"https://{module.TLS_HOST}:{tls.server.server_port}",
        "tls": tls,
        "fingerprint": lambda: module.fingerprint(tls.folder / "cert.pem"),
    }
    tls.server.shutdown()
    tls.server.server_close()


def _installed_chromium() -> Path | None:
    from clio_agent.gact.browser import install

    if not install.playwright_version():
        return None
    candidates = [install.browsers_root()]
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        candidates.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    for root in candidates:
        found = install.chromium_executable(root)
        if found is not None:
            return found
    return None


@pytest.fixture(scope="session")
def chromium_path() -> Path:
    found = _installed_chromium()
    if found is None:
        pytest.skip("no Chromium for the pinned Playwright release is installed")
    return found


@pytest.fixture()
def browser(chromium_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A fresh browser service with its state and scratch under ``tmp_path``."""
    from clio_agent.gact.browser import install, service

    monkeypatch.setattr(install, "ensure_chromium", lambda: chromium_path)
    monkeypatch.setattr(install, "_sandbox_path", lambda: tmp_path / "sandbox.json")
    monkeypatch.setattr(service, "_state_root", lambda: tmp_path / "browser-state")
    scratch = tmp_path / "scratch"

    def scratch_dir(session_id: str, *, create: bool = True) -> Path:
        folder = scratch / session_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    monkeypatch.setattr("clio_agent.runtime.session_scratch.session_scratch_dir", scratch_dir)
    monkeypatch.setenv("CLIO_BROWSER_ALLOW_UNSANDBOXED", "1")
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "*.fixture.localhost")
    instance = service.BrowserService()
    monkeypatch.setattr(service, "_SERVICE", instance)
    yield instance
    instance.close_all()
