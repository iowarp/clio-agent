"""The document runtime's package tools keep every cache inside CLIO's own cache root."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.runtime import document_runtime


class _Stop(Exception):
    """Ends the run once the pnpm step has its environment."""


def test_npm_and_pnpm_caches_stay_in_clios_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live (Linux Desktop 0.9.6.6): npm installed pnpm with its default cache, ~/.npm,
    and 31 MB of it outlived a full uninstall. npm ignores XDG, so it is named."""
    seen: dict[str, dict[str, str]] = {}

    def prepared_pnpm(_tools: Path, _node: str, env: dict[str, str], _progress: object) -> Path:
        seen["env"] = env
        raise _Stop

    monkeypatch.setattr(document_runtime, "_prepared_pnpm", prepared_pnpm)
    monkeypatch.setattr(document_runtime, "_adopt_prelaid", lambda *_args: False)
    cache = tmp_path / "clio-cache"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(_Stop):
        document_runtime._javascript_runtime(cache, workspace, {"node": "/usr/bin/node"})

    env = seen["env"]
    for name in ("npm_config_cache", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        assert Path(env[name]).is_relative_to(cache), name
