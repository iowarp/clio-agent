"""Run an isolated production CLIO service for dashboard and Best of N review."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests._cte_isolation import isolate_cte_env, reap_private_daemon  # noqa: E402

EVIDENCE = Path(os.environ["CLIO_WORKFLOW_EVIDENCE"])
WORKSPACE = EVIDENCE / "workspace"
WORKSPACE.mkdir(parents=True, exist_ok=True)
ISOLATION = isolate_cte_env(EVIDENCE / "private-core" / uuid.uuid4().hex, os.environ)
os.environ["CLIO_GACT_CORS_ORIGINS"] = "http://127.0.0.1:5215"

from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities  # noqa: E402

from clio_agent.arc.memory import ARCMemory  # noqa: E402
from clio_agent.arc.storage import make_arc_store  # noqa: E402
from clio_agent.gact import context as ctx  # noqa: E402
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities  # noqa: E402
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id  # noqa: E402
from clio_agent.gact.a2ui_producer import build_publish_dashboard_report_tool  # noqa: E402
from clio_agent.gact.app import build_app  # noqa: E402

app = build_app(
    sessions_path=WORKSPACE / "sessions.json",
    arc=ARCMemory(
        store=make_arc_store(
            backend="cte",
            data_dir=WORKSPACE / "arc",
            namespace=f"workflow_review_{uuid.uuid4().hex}",
        )
    ),
)
app.state.workspaces.update("ws_default", root_path=str(WORKSPACE))
sessions = {
    name: app.state.sessions.create(
        workspace_id="ws_default", title=title, approval_mode="auto-edits"
    )
    for name, title in {
        "dashboard": "Morning bike imbalance — synthetic example",
        "drafts": "Compare report introductions — Best of N",
    }.items()
}
for session in sessions.values():
    remember_client_capabilities(
        app,
        session.id,
        A2UIClientCapabilities.model_validate(
            {
                "v0.9": {"supportedCatalogIds": [workspace_catalog_id()]},
            }
        ),
    )
references = ROOT / "src/clio_agent/gact/builtin_skills/create-dashboard/references"
for name in ["bike-station-report.json", "bike-stations.csv"]:
    shutil.copy2(references / name, WORKSPACE / name)
tokens = [
    ctx.set_app(app),
    ctx.set_session_id(sessions["dashboard"].id),
    ctx.set_turn_id_token("seed-report"),
    ctx.set_react_scope("seed-report", "react"),
    ctx.set_react_session(sessions["dashboard"].id),
]
try:
    report = build_publish_dashboard_report_tool().func(definition_path="bike-station-report.json")
    if report.get("ok") is False:
        raise RuntimeError(report)
finally:
    for token in reversed(tokens):
        ctx.reset(token)
manifest = {
    "sessions": {key: value.id for key, value in sessions.items()},
    "report": report,
    "iowarp_core": importlib.metadata.version("iowarp-core"),
    "backend_port": 18628,
    "ui_port": 5215,
    "source": str(ROOT),
    "pid": os.getpid(),
    "native_profile": str(ISOLATION.root),
}
(EVIDENCE / "runtime.json").write_text(
    json.dumps(manifest, indent=2, default=str), encoding="utf-8"
)
print(json.dumps(manifest, default=str), flush=True)

server = uvicorn.Server(
    uvicorn.Config(app, host="127.0.0.1", port=18628, timeout_graceful_shutdown=5)
)


@app.post("/review/close")
def close_review() -> dict[str, bool]:
    """Close only this isolated recording service through normal application shutdown."""
    server.should_exit = True
    return {"closing": True}


if __name__ == "__main__":
    try:
        server.run()
    finally:
        reap_private_daemon(ISOLATION.state_dir)
