"""Installed vs bundled CLI selection for the Claude Code SDK transport (Codex runs none).

Fake binaries live in a temp HOME/PATH. The selection logic, the npm-launcher
resolution and the native-binary check run for real against those files; the
``--version`` probe runs for real against a script that answers it (one test)
and is replaced by a path->version table elsewhere, since a fake file that
passes the native-executable check cannot also execute.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from clio_agent.providers.components import client_binary as cb

EXE = ".exe" if os.name == "nt" else ""
NATIVE_MAGIC = b"MZ\x90\x00" if os.name == "nt" else b"\x7fELF"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    (tmp_path / "bin").mkdir()
    # Keep the POSIX well-known system locations out of the test machine's view.
    real_claude = cb._claude_candidates
    monkeypatch.setattr(
        cb,
        "_claude_candidates",
        lambda: [p for p in real_claude() if str(p).startswith(str(tmp_path))],
    )
    cb.reset_client_cache()
    yield
    cb.reset_client_cache()


def _native(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(NATIVE_MAGIC + b"\x00" * 60)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _versions(monkeypatch: pytest.MonkeyPatch, table: dict[Path, str]) -> list[str]:
    calls: list[str] = []
    resolved = {os.path.normcase(str(p.resolve())): v for p, v in table.items()}

    def _probe(path: str) -> str:
        calls.append(path)
        return resolved.get(os.path.normcase(str(Path(path).resolve())), "")

    monkeypatch.setattr(cb, "probe_version", _probe)
    return calls


# --- --version parsing / probing -----------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("codex-cli 0.157.1", "0.157.1"),
        ("2.1.281 (Claude Code)", "2.1.281"),
        ("codex-cli 0.158.0-alpha.2\n", "0.158.0-alpha.2"),
        ("no version here", ""),
    ],
)
def test_parse_version_reads_both_clis_replies(reply: str, expected: str) -> None:
    assert cb.parse_version(reply) == expected


def test_probe_version_runs_the_binary_for_real(tmp_path: Path) -> None:
    """A real subprocess: a script that answers ``--version`` like the Codex CLI."""
    if os.name == "nt":
        script = tmp_path / "fake-codex.cmd"
        script.write_text("@echo codex-cli 0.157.1\r\n", encoding="utf-8")
    else:
        script = tmp_path / "fake-codex"
        script.write_text("#!/bin/sh\necho 'codex-cli 0.157.1'\n", encoding="utf-8")
        script.chmod(0o755)
    assert cb.probe_version(str(script)) == "0.157.1"


def test_probe_version_is_empty_for_a_missing_or_failing_binary(tmp_path: Path) -> None:
    assert cb.probe_version(str(tmp_path / "nope" / f"codex{EXE}")) == ""
    if os.name == "nt":
        failing = tmp_path / "fail.cmd"
        failing.write_text("@exit /b 3\r\n", encoding="utf-8")
    else:
        failing = tmp_path / "fail"
        failing.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        failing.chmod(0o755)
    assert cb.probe_version(str(failing)) == ""


def test_native_check_rejects_script_shims(tmp_path: Path) -> None:
    shim = tmp_path / ("codex.cmd" if os.name == "nt" else "codex")
    shim.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    assert not cb.is_native_executable(shim)
    assert cb.is_native_executable(_native(tmp_path / f"real{EXE}"))


# --- Claude Code: installed only when newer ---------------------------------------------


def _claude_installed(tmp_path: Path) -> Path:
    return _native(tmp_path / "home" / ".local" / "bin" / f"claude{EXE}")


def _claude_bundled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, present: bool = True
) -> Path | None:
    path = (
        _native(tmp_path / "site" / "claude_agent_sdk" / "_bundled" / f"claude{EXE}")
        if present
        else None
    )
    monkeypatch.setattr(cb, "bundled_claude_path", lambda: path)
    return path


def test_claude_prefers_an_installed_cli_newer_than_the_bundled_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed, bundled = _claude_installed(tmp_path), _claude_bundled(monkeypatch, tmp_path)
    assert bundled is not None
    _versions(monkeypatch, {installed: "2.1.281", bundled: "2.1.276"})
    selection = cb.claude_client()
    assert selection.client is not None
    assert (selection.client.source, selection.client.version) == ("installed", "2.1.281")
    assert selection.reason == "claude_installed_newer"


def test_claude_keeps_the_bundled_cli_when_the_installed_one_is_not_newer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SABOTAGE: always prefer the installed CLI -> source flips -> red."""
    installed, bundled = _claude_installed(tmp_path), _claude_bundled(monkeypatch, tmp_path)
    assert bundled is not None
    _versions(monkeypatch, {installed: "2.1.270", bundled: "2.1.276"})
    selection = cb.claude_client()
    assert selection.client is not None
    assert (selection.client.source, selection.client.path) == ("bundled", str(bundled))
    assert selection.reason == "claude_bundled_not_older"
    assert selection.to_wire()["installed_version"] == "2.1.270"


def test_claude_uses_the_installed_cli_when_the_wheel_bundles_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed = _claude_installed(tmp_path)
    _claude_bundled(monkeypatch, tmp_path, present=False)
    _versions(monkeypatch, {installed: "2.1.100"})
    selection = cb.claude_client()
    assert selection.client is not None and selection.client.source == "installed"
    assert selection.reason == "claude_bundled_missing"


def test_claude_without_an_installed_cli_uses_the_bundled_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundled = _claude_bundled(monkeypatch, tmp_path)
    assert bundled is not None
    _versions(monkeypatch, {bundled: "2.1.276"})
    selection = cb.claude_client()
    assert selection.client == cb.ClientBinary(str(bundled), "2.1.276", "bundled")
    assert selection.reason == "claude_installed_not_found"


def test_claude_never_selects_the_npm_cmd_shim_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "bin" / "claude.cmd").write_text("@echo 9.9.9\r\n", encoding="utf-8")
    bundled = _claude_bundled(monkeypatch, tmp_path)
    assert bundled is not None
    _versions(monkeypatch, {bundled: "2.1.276", tmp_path / "bin" / "claude.cmd": "9.9.9"})
    selection = cb.claude_client()
    assert selection.client is not None and selection.client.source == "bundled"


def test_claude_code_runtime_pins_the_same_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The transport's ``cli_path`` and the discovery sign-in check use ONE selection."""
    from clio_agent.providers import claude_code_runtime
    from clio_agent.providers.model_discovery import claude_code as md_claude

    installed, bundled = _claude_installed(tmp_path), _claude_bundled(monkeypatch, tmp_path)
    assert bundled is not None
    _versions(monkeypatch, {installed: "2.1.281", bundled: "2.1.276"})
    assert claude_code_runtime.resolve_cli_path() == str(installed)
    assert md_claude._resolve_claude_binary() == str(installed)
    info = claude_code_runtime.claude_code_runtime_info()
    assert (info["source"], info["cli_version"]) == ("installed", "2.1.281")


def test_codex_runs_no_cli_so_it_has_no_client_fact() -> None:
    """Codex's one transport is direct: no binary is selected or reported for it."""
    assert cb.provider_client("codex") is None
    assert cb.provider_client_fact("codex", refresh=True) is None
    assert not hasattr(cb, "codex_client")


def test_selection_is_cached_until_reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SABOTAGE: drop the cache -> every call re-probes -> the call count grows."""
    installed, bundled = _claude_installed(tmp_path), _claude_bundled(monkeypatch, tmp_path)
    assert bundled is not None
    calls = _versions(monkeypatch, {installed: "2.1.281", bundled: "2.1.276"})
    cb.claude_client()
    first = len(calls)
    cb.claude_client()
    assert len(calls) == first
    cb.reset_client_cache()
    cb.claude_client()
    assert len(calls) == first * 2
