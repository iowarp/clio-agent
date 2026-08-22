"""Integration with the installed Flowcept runtime, isolated in a subprocess."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_real_flowcept_offline_runtime_captures_clio_records(tmp_path: Path) -> None:
    settings = tmp_path / "flowcept-settings.yaml"
    settings.write_text(
        """
flowcept_version: 1.0.3
project:
  db_flush_mode: offline
  enrich_messages: false
  dump_buffer:
    enabled: false
    path: flowcept-buffer.jsonl
    append_id_to_path: false
    append_workflow_id_to_path: false
    delete_previous_file: false
log:
  log_file_level: disable
  log_stream_level: disable
instrumentation:
  enabled: true
telemetry_capture: {}
experiment: {}
mq:
  enabled: false
kv_db:
  enabled: false
databases:
  mongodb:
    enabled: false
  lmdb:
    enabled: false
db_buffer: {}
agent: {}
web_server: {}
sys_metadata: {}
extra_metadata: {}
adapters: {}
""".strip(),
        encoding="utf-8",
    )
    script = r"""
from clio_agent.gact.provenance.flowcept import FlowceptProviderConfig, FlowceptProvenanceProvider
from clio_agent.gact.semantic_events import SemanticEvent

provider = FlowceptProvenanceProvider(
    FlowceptProviderConfig(settings_path=SETTINGS, privacy="metadata", check_safe_stops=False)
)
created = SemanticEvent(
    event_type="session.created",
    session_id="sess_offline",
    workspace_id="ws_science",
    trace_id="session:sess_offline",
    payload={"agent": {"id": "earthscope"}, "parent_session_id": ""},
)
provider.emit(created)
provider.emit(
    SemanticEvent(
        event_type="lm.response.completed",
        session_id="sess_offline",
        workspace_id="ws_science",
        trace_id="trace_1",
        turn_id="turn_1",
        actor={"agent_id": "earthscope"},
        payload={"prompt": "DO NOT EXPORT", "response": "PRIVATE RESPONSE"},
    )
)
buffer = list(provider._runtime.get_buffer())
types = {str(row.get("type")) for row in buffer}
assert {"workflow", "agent", "task"}.issubset(types), buffer
serialized = repr(buffer)
assert "DO NOT EXPORT" not in serialized
assert "PRIVATE RESPONSE" not in serialized
assert "lm.response.completed" in serialized
provider.emit(
    SemanticEvent(
        event_type="turn.completed",
        session_id="sess_offline",
        workspace_id="ws_science",
        trace_id="trace_1",
        turn_id="turn_1",
        status="completed",
    )
)
terminal_buffer = provider._runtime.get_buffer()
assert any(
    row.get("type") == "workflow"
    and getattr(row.get("status"), "value", row.get("status")) == "FINISHED"
    for row in terminal_buffer
), terminal_buffer
provider.emit(
    SemanticEvent(
        event_type="turn.started",
        session_id="sess_offline",
        workspace_id="ws_science",
        trace_id="trace_2",
        turn_id="turn_2",
        status="started",
    )
)
reopened_buffer = provider._runtime.get_buffer()
assert any(
    row.get("type") == "workflow"
    and getattr(row.get("status"), "value", row.get("status")) == "RUNNING"
    for row in reopened_buffer
), reopened_buffer
provider.close()
""".replace("SETTINGS", repr(str(settings)))
    env = os.environ.copy()
    env["FLOWCEPT_SETTINGS_PATH"] = str(settings)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
