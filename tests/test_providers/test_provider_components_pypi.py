"""The PyPI update check for the user-updatable provider components.

Driven by RECORDED PyPI JSON (``fixtures/pypi/*.json``, trimmed real replies of
2026-09-26). The facts they pin are real: claude-agent-sdk 0.2.157 and 0.2.160
shipped no Windows wheel, so the newest release installable on Windows is
0.2.159 while Linux gets 0.2.160.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from packaging.tags import Tag

from clio_agent.providers.components import pypi, status
from clio_agent.providers.components.registry import PROVIDER_COMPONENTS, USER_UPDATABLE_COMPONENTS

FIXTURES = Path(__file__).parent / "fixtures" / "pypi"
WINDOWS = frozenset({Tag("py3", "none", "win_amd64"), Tag("py3", "none", "any")})
LINUX = frozenset({Tag("py3", "none", "manylinux_2_17_x86_64"), Tag("py3", "none", "any")})


def _recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_the_registry_declares_exactly_the_three_sdks() -> None:
    assert USER_UPDATABLE_COMPONENTS == {"openai-codex", "openai-codex-cli-bin", "claude-agent-sdk"}
    assert PROVIDER_COMPONENTS["codex"].lockstep is True


def test_windows_skips_claude_releases_without_a_windows_wheel() -> None:
    """SABOTAGE: accept sdists / any wheel -> 0.2.160 (no win wheel) wins -> red."""
    index = pypi.parse_release_index(
        "claude-agent-sdk",
        _recorded("claude-agent-sdk"),
        supported=WINDOWS,
        python_version="3.12.0",
    )
    assert index.latest == "0.2.159"
    assert "0.2.157" not in index.installable and "0.2.160" not in index.installable
    assert index.installable["0.2.159"].filename.endswith("win_amd64.whl")
    assert len(index.installable["0.2.159"].sha256) == 64


def test_linux_gets_the_newest_claude_release() -> None:
    index = pypi.parse_release_index(
        "claude-agent-sdk", _recorded("claude-agent-sdk"), supported=LINUX, python_version="3.12.0"
    )
    assert index.latest == "0.2.160"


def test_prereleases_yanked_files_and_python_mismatches_are_not_installable() -> None:
    payload = copy.deepcopy(_recorded("openai-codex"))
    wheel = copy.deepcopy(payload["releases"]["0.157.1"][0])
    payload["releases"]["0.158.0rc1"] = [
        {**wheel, "filename": "openai_codex-0.158.0rc1-py3-none-any.whl"}
    ]
    payload["releases"]["0.158.0"] = [
        {**wheel, "filename": "openai_codex-0.158.0-py3-none-any.whl", "yanked": True}
    ]
    payload["releases"]["0.159.0"] = [
        {**wheel, "filename": "openai_codex-0.159.0-py3-none-any.whl", "requires_python": ">=3.14"}
    ]
    index = pypi.parse_release_index(
        "openai-codex", payload, supported=WINDOWS, python_version="3.12.0"
    )
    assert index.latest == "0.157.1"


def test_a_reply_without_releases_is_a_typed_failure() -> None:
    with pytest.raises(pypi.PyPILookupError) as caught:
        pypi.parse_release_index(
            "openai-codex", {"info": {}}, supported=WINDOWS, python_version="3.12.0"
        )
    assert caught.value.code == "pypi_malformed"


# --- TTL cache -------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_lookup_serves_the_cache_within_the_ttl_and_refetches_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: drop the cache -> every read re-fetches -> the fetch count grows."""
    monkeypatch.setattr(pypi, "ttl_s", lambda: 60.0)
    fetched: list[str] = []

    def _fetch(url: str) -> dict[str, Any]:
        fetched.append(url)
        return _recorded("openai-codex")

    clock = _Clock()
    lookup = pypi.ReleaseLookup(fetch=_fetch, clock=clock)
    assert lookup.releases("openai-codex").latest == "0.157.1"
    lookup.releases("openai-codex")
    assert len(fetched) == 1
    lookup.releases("openai-codex", refresh=True)
    assert len(fetched) == 2
    clock.now += 61
    lookup.releases("openai-codex")
    assert len(fetched) == 3
    assert fetched[0].endswith("/openai-codex/json")


def test_a_failure_is_cached_too_and_raised_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pypi, "ttl_s", lambda: 60.0)
    calls = {"n": 0}

    def _fetch(_url: str) -> dict[str, Any]:
        calls["n"] += 1
        raise pypi.PyPILookupError("pypi_unreachable", "offline")

    lookup = pypi.ReleaseLookup(fetch=_fetch, clock=_Clock())
    for _ in range(2):
        with pytest.raises(pypi.PyPILookupError) as caught:
            lookup.releases("openai-codex")
        assert caught.value.code == "pypi_unreachable"
    assert calls["n"] == 1


# --- the real HTTP fetch against a local index ---------------------------------------


@pytest.fixture
def local_index() -> Iterator[str]:
    """A real HTTP server answering like PyPI's JSON API (fixtures + a 404 + a 500)."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            name = self.path.strip("/").split("/")[0]
            if name == "broken":
                self.send_response(500)
                self.end_headers()
                return
            path = FIXTURES / f"{name}.json"
            if not path.is_file():
                self.send_response(404)
                self.end_headers()
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_http_fetch_reads_the_json_api(monkeypatch: pytest.MonkeyPatch, local_index: str) -> None:
    monkeypatch.setattr(pypi, "index_url", lambda: local_index)
    lookup = pypi.ReleaseLookup()
    assert lookup.releases("openai-codex").latest == "0.157.1"


@pytest.mark.parametrize(
    ("name", "code"), [("not-a-package", "pypi_not_found"), ("broken", "pypi_http_error")]
)
def test_http_fetch_failures_are_typed(
    monkeypatch: pytest.MonkeyPatch, local_index: str, name: str, code: str
) -> None:
    monkeypatch.setattr(pypi, "index_url", lambda: local_index)
    with pytest.raises(pypi.PyPILookupError) as caught:
        pypi.ReleaseLookup().releases(name)
    assert caught.value.code == code


def test_an_unreachable_index_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pypi, "index_url", lambda: "http://127.0.0.1:9")
    with pytest.raises(pypi.PyPILookupError) as caught:
        pypi.ReleaseLookup().releases("openai-codex")
    assert caught.value.code == "pypi_unreachable"


# --- update_available per provider -----------------------------------------------------


@pytest.fixture
def windows_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pypi, "sys_tags", lambda: iter(WINDOWS))
    monkeypatch.setattr(pypi, "_python_version", lambda: "3.12.0")


def _recorded_lookup() -> pypi.ReleaseLookup:
    return pypi.ReleaseLookup(fetch=lambda url: _recorded(url.rstrip("/").split("/")[-2]))


def test_codex_reports_an_update_for_the_whole_lockstep_group(
    monkeypatch: pytest.MonkeyPatch, windows_tags: None
) -> None:
    monkeypatch.setattr(status, "installed_version", lambda _name: "0.147.0")
    result = status.provider_component_status("codex", lookup=_recorded_lookup())
    assert result is not None
    wire = result.to_wire()
    assert wire["update_available"] is True
    assert wire["target_version"] == "0.157.1"
    assert [
        (c["distribution"], c["installed_version"], c["latest_version"]) for c in wire["components"]
    ] == [
        ("openai-codex", "0.147.0", "0.157.1"),
        ("openai-codex-cli-bin", "0.147.0", "0.157.1"),
    ]
    assert wire["release_notes_url"].startswith("https://github.com/openai/codex")
    assert wire["error"] is None


def test_the_lockstep_target_is_the_newest_version_every_member_can_install(
    monkeypatch: pytest.MonkeyPatch, windows_tags: None
) -> None:
    """A newer openai-codex whose cli-bin has no wheel here is not offered."""
    monkeypatch.setattr(status, "installed_version", lambda _name: "0.147.0")
    codex = copy.deepcopy(_recorded("openai-codex"))
    wheel = copy.deepcopy(codex["releases"]["0.157.1"][0])
    codex["releases"]["0.158.0"] = [{**wheel, "filename": "openai_codex-0.158.0-py3-none-any.whl"}]

    def _fetch(url: str) -> dict[str, Any]:
        name = url.rstrip("/").split("/")[-2]
        return codex if name == "openai-codex" else _recorded(name)

    result = status.provider_component_status("codex", lookup=pypi.ReleaseLookup(fetch=_fetch))
    assert result is not None and result.target_version == "0.157.1"


def test_claude_on_windows_targets_the_newest_release_with_a_windows_wheel(
    monkeypatch: pytest.MonkeyPatch, windows_tags: None
) -> None:
    monkeypatch.setattr(status, "installed_version", lambda _name: "0.2.156")
    result = status.provider_component_status("claude_code", lookup=_recorded_lookup())
    assert result is not None
    assert (result.update_available, result.target_version) == (True, "0.2.159")


def test_no_update_when_current_or_newer_or_not_installed(
    monkeypatch: pytest.MonkeyPatch, windows_tags: None
) -> None:
    for installed, expect_installed in (("0.157.1", True), ("0.158.0", True), ("", False)):
        monkeypatch.setattr(status, "installed_version", lambda _name, v=installed: v)
        result = status.provider_component_status("codex", lookup=_recorded_lookup())
        assert result is not None
        assert result.update_available is False
        assert result.installed is expect_installed


def test_an_unreachable_index_reports_a_typed_error_and_no_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status, "installed_version", lambda _name: "0.147.0")

    def _fetch(_url: str) -> dict[str, Any]:
        raise pypi.PyPILookupError("pypi_unreachable", "offline")

    result = status.provider_component_status("codex", lookup=pypi.ReleaseLookup(fetch=_fetch))
    assert result is not None
    wire = result.to_wire()
    assert wire["update_available"] is False
    assert wire["error"]["code"] == "pypi_unreachable"
    assert wire["components"][0]["installed_version"] == "0.147.0"


def test_a_provider_without_components_has_no_status() -> None:
    assert status.provider_component_status("openai") is None
