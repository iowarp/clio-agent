"""Operation history survives reconnects without exposing raw runtime configuration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact import blueprint_operations as operations


def test_history_reports_interruption_and_projects_only_public_receipt_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operations, "operation_directory", lambda: tmp_path)
    (tmp_path / "unfinished.json").write_text(
        json.dumps(
            {
                "id": "unfinished",
                "label": "Reload",
                "status": "preparing",
                "target": {"source_id": "src_a"},
                "result": {
                    "installed": [
                        {
                            "id": "demo",
                            "metadata": {"env": {"SECRET": "hidden"}},
                            "install": {"checksum": "abc"},
                        }
                    ]
                },
            }
        )
    )
    app = SimpleNamespace(state=SimpleNamespace())
    rows = operations.list_blueprint_operations(app)
    assert rows[0]["status"] == "interrupted"
    assert "hidden" not in json.dumps(rows)
    assert rows[0]["installed"][0]["checksum"] == "abc"
    app.state.blueprint_active_operations = {"unfinished"}
    assert operations.list_blueprint_operations(app)[0]["status"] == "preparing"
