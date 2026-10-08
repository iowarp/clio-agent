"""Installation is complete only after both package sets and Office are available."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import document_install as installer


@pytest.mark.parametrize("javascript", ["ready", "failed"])
def test_installer_records_only_complete_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, javascript: str
) -> None:
    cache = tmp_path / "cache"
    (cache / "locked").mkdir(parents=True)
    result: dict[str, Any] = {
        "status": "ready",
        "runtime_id": "locked",
        "javascript": {"status": javascript},
        "native_tools": {},
        "capabilities": {},
    }
    monkeypatch.setattr(installer, "prepare_document_runtime", lambda *a, **kw: result)
    monkeypatch.setattr(installer, "prepare_office_runtime", lambda: "private soffice")
    monkeypatch.setattr(installer, "ensure_github_cli", lambda: tmp_path / "gh")
    monkeypatch.setattr(
        installer, "ensure_bundled_codex_windows_helpers", lambda: {"status": "available"}
    )
    monkeypatch.setattr(
        installer, "prepare_existing_windows_fence", lambda: {"status": "available"}
    )
    stages: list[str] = []
    receipt = cache / "locked" / "installed.json"
    if javascript == "failed":
        with pytest.raises(installer.DocumentRuntimeError, match="Node/pnpm"):
            installer.install_document_runtime(
                tmp_path / "workspace", cache_root=cache, progress=stages.append
            )
        assert not receipt.exists()
        assert stages == []
    else:
        installed = installer.install_document_runtime(
            tmp_path / "workspace", cache_root=cache, progress=stages.append
        )
        assert installed["capabilities"]["office_render_recalculate"] == "available"
        assert installed["native_tools"]["gh"]["authentication"] == "clio_account"
        assert (
            json.loads(receipt.read_text())["native_tools"]["soffice"]["path"] == "private soffice"
        )
        assert stages == [
            "Preparing and checking the Office renderer...",
            "Office rendering is ready. Preparing the GitHub command-line tool...",
            "Preparing and checking protected execution helpers...",
            "All managed runtime packages are installed and verified.",
        ]
