"""#801: every user-facing optimizer entry point returns the uniform
structured not-implemented response (reason code + pointer to #633).

The optimizer stays as a research surface (owner decision resolving #768);
nothing is deleted, but no path may silently half-run or pretend. These
tests pin the shared stub payload and its projection onto each surface:
the gact ``/optimize`` command row, the ``optimizer_command``
capability-gap row, and the ``--tune`` CLI hook.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from clio_agent.optimizer.stub import (
    OPTIMIZER_NOT_IMPLEMENTED_REASON,
    OPTIMIZER_TRACKING_ISSUE,
    optimizer_not_implemented_payload,
)

_CLI_PATH = Path(__file__).resolve().parents[2] / "src" / "clio_agent" / "ui" / "cli.py"


class TestStubPayload:
    """The canonical payload every entry point derives from."""

    def test_reason_code_and_issue_pointer(self):
        assert OPTIMIZER_NOT_IMPLEMENTED_REASON == "optimizer_not_implemented"
        assert OPTIMIZER_TRACKING_ISSUE.endswith("/issues/633")

    def test_payload_shape(self):
        payload = optimizer_not_implemented_payload()
        assert payload == {
            "implemented": False,
            "reason": OPTIMIZER_NOT_IMPLEMENTED_REASON,
            "message": payload["message"],
            "tracking_issue": OPTIMIZER_TRACKING_ISSUE,
        }
        assert "633" in payload["message"]
        assert "not implemented" in payload["message"]


class TestSurfaceUniformity:
    """All optimizer entry points project the same reason code + pointer."""

    def test_gact_optimize_command_row_uses_stub(self):
        from clio_agent.gact.runtime.commands import BACKEND_COMMANDS

        row = next(c for c in BACKEND_COMMANDS if c["id"] == "/optimize")
        assert row["status"] == "unavailable"
        assert row["enabled"] is False
        assert row["error"] == OPTIMIZER_NOT_IMPLEMENTED_REASON
        assert OPTIMIZER_TRACKING_ISSUE in row["disabled_reason"]

    def test_capability_gap_row_uses_stub(self):
        from clio_agent.gact.runtime.capabilities import _CAPABILITY_GAP_DEFINITIONS

        gap = _CAPABILITY_GAP_DEFINITIONS["optimizer_command"]
        assert gap["status"] == "unavailable"
        assert gap["advertised"] is True
        assert gap["reason"] == OPTIMIZER_NOT_IMPLEMENTED_REASON
        assert gap["tracking_issue"] == OPTIMIZER_TRACKING_ISSUE
        assert OPTIMIZER_TRACKING_ISSUE in gap["description"]

    def test_command_row_and_capability_gap_agree(self):
        from clio_agent.gact.runtime.capabilities import _CAPABILITY_GAP_DEFINITIONS
        from clio_agent.gact.runtime.commands import BACKEND_COMMANDS

        row = next(c for c in BACKEND_COMMANDS if c["id"] == "/optimize")
        gap = _CAPABILITY_GAP_DEFINITIONS["optimizer_command"]
        assert row["error"] == gap["reason"]


class TestCliTuneHook:
    """``--tune`` prints the structured payload and exits without half-running."""

    def test_tune_json_emits_uniform_payload(self):
        proc = subprocess.run(
            [sys.executable, str(_CLI_PATH), "--tune", "data", "--json"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 2, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["implemented"] is False
        assert payload["reason"] == OPTIMIZER_NOT_IMPLEMENTED_REASON
        assert payload["tracking_issue"] == OPTIMIZER_TRACKING_ISSUE
        assert payload["expert_id"] == "data"
