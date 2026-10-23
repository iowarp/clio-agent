"""Repeated arguments must retain each raw model observation and capture hash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.live_verification.mermaid_capabilities.collect import collect_run


@pytest.mark.parametrize("corrupt", [False, True])
def test_repeated_capture_arguments_preserve_distinct_observations(
    tmp_path: Path,
    corrupt: bool,
) -> None:
    record = tmp_path / "run.json"
    record.write_text(
        json.dumps({"session_id": "s", "stage": "test", "model": "test", "workspace": {}}),
        encoding="utf-8",
    )
    events = []
    for index, content in enumerate([b"first capture", b"second capture"]):
        capture = tmp_path / f"{index}.png"
        capture.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        events.append(
            {
                "event_type": "react.step.completed",
                "payload": {
                    "tool_calls": [
                        {
                            "id": f"call_{index}",
                            "name": "capture_a2ui_surface",
                            "args": {"viewer": "same"},
                            "observation": {
                                "capture": {"request_id": str(index)},
                                "path": str(capture),
                                "sha256": "wrong" if corrupt else digest,
                            },
                        }
                    ]
                },
            }
        )
    (tmp_path / "s.semantic.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events),
        encoding="utf-8",
    )
    if corrupt:
        with pytest.raises(ValueError, match="hash mismatch"):
            collect_run(record, tmp_path)
    else:
        result = collect_run(record, tmp_path)
        assert len(result["captures"]) == 2
        assert result["captures"][0]["sha256"] != result["captures"][1]["sha256"]
