"""Build a labelled synthetic example through CLIO's real artifact tools."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def seed_example(app: FastAPI, root: Path) -> dict[str, Any]:
    """Publish a dashboard and three computed evidence branches joining in a report."""
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    from clio_agent.gact import context as ctx
    from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
    from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
    from clio_agent.gact.a2ui_producer import build_publish_dashboard_report_tool
    from clio_agent.gact.artifacts.designation_tool import build_create_artifact_tool
    from clio_agent.gact.artifacts.transforms import record_transform
    from clio_agent.gact.types import AgentDef
    from clio_agent.tools.execution import tool_workspace_context

    root.mkdir(parents=True, exist_ok=True)
    workspace = app.state.workspaces.create(name="Bike stations · synthetic", root_path=str(root))
    session = app.state.sessions.create(
        workspace_id=workspace.id, title="Morning bike imbalance · synthetic example"
    )
    site = Path(__file__).resolve().parents[1]
    shutil.copy2(site / "public/datasets/bike-stations.csv", root / "bike-stations.csv")
    shutil.copy2(
        site / "video/showcase-data/bike-station-report.json", root / "bike-station-report.json"
    )
    remember_client_capabilities(
        app,
        session.id,
        A2UIClientCapabilities.model_validate(
            {"v0.9": {"supportedCatalogIds": [workspace_catalog_id()]}}
        ),
    )
    tokens = [
        ctx.set_app(app),
        ctx.set_session_id(session.id),
        ctx.set_turn_id_token("website-authored-example"),
        ctx.set_react_scope("website-authored-example", "react"),
        ctx.set_react_session(session.id),
    ]
    create = build_create_artifact_tool(AgentDef(id="main", title="Walkthrough authoring"))
    published: list[dict[str, Any]] = []

    def register(**arguments: Any) -> dict[str, Any]:
        result: dict[str, Any] = create.func(**arguments)
        if result.get("rejected") or not result.get("artifacts"):
            raise RuntimeError(result)
        record_transform(
            app,
            session.id,
            tool_name="create_artifact",
            args=arguments,
            call_id=f"website-create-{len(published)}",
            ok=True,
            result=result,
            minted=[],
            workspace_id=workspace.id,
            turn_id="website-authored-example",
        )
        published.append(result)
        return result

    try:
        with tool_workspace_context(root):
            register(name="bike-stations.csv", kind="dataset", path="bike-stations.csv")
            with (root / "bike-stations.csv").open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            summary = "station,area,net_bikes\n" + "".join(
                f"{row['name']},{row['category']},{int(row['at_9']) - int(row['at_6'])}\n"
                for row in rows
            )
            register(
                name="station-balance.csv",
                kind="dataset",
                content=summary,
                used=["bike-stations.csv"],
                annotation="Computed from 09:00 minus 06:00 dock counts; fictional stations.",
            )
            snapshots = [
                ("06:00", "at_6"),
                ("07:00", "at_7"),
                ("08:00", "at_8"),
                ("08:15", "at_815"),
                ("08:30", "at_830"),
                ("09:00", "at_9"),
            ]
            register(
                name="availability-check.csv",
                kind="dataset",
                content="station,first_empty,last_count\n"
                + "".join(
                    f"{row['name']},{next((time for time, key in snapshots if int(row[key]) == 0), 'never')},{row['at_9']}\n"
                    for row in rows
                ),
                used=["bike-stations.csv"],
                annotation="First empty snapshot per fictional station; no trip inference.",
            )
            register(
                name="capacity-check.csv",
                kind="dataset",
                content="station,capacity,peak_count,within_capacity\n"
                + "".join(
                    f"{row['name']},{row['capacity']},{max(int(row[key]) for _, key in snapshots)},"
                    f"{all(0 <= int(row[key]) <= int(row['capacity']) for _, key in snapshots)}\n"
                    for row in rows
                ),
                used=["bike-stations.csv"],
                annotation="Checks every count against its station capacity; fictional data.",
            )
            report = register(
                name="morning-findings.md",
                kind="report",
                content=(
                    "# Morning bike imbalance\n\n"
                    "**Synthetic worked example.** This walkthrough was authored with CLIO's "
                    "production tools; it is not a new model run.\n\n"
                    "## Where bikes leave and accumulate\n\n"
                    "Fernhill is empty by 08:15. Hill stations lose 37 bikes between 06:00 "
                    "and 09:00; Downtown stations gain 37. The five-station total stays at "
                    "54 bikes at every snapshot.\n\n"
                    "## What the data supports\n\n"
                    "These are dock counts, not trips. Matching area changes do not identify "
                    "individual journeys or prove why the stations emptied.\n\n"
                    "## Follow the evidence\n\n"
                    "station-balance.csv contains net changes; availability-check.csv finds "
                    "the first empty snapshot; capacity-check.csv checks count bounds. All "
                    "three use bike-stations.csv, with the input relationships recorded in Lineage."
                ),
                used=["station-balance.csv", "availability-check.csv", "capacity-check.csv"],
            )
            dashboard = build_publish_dashboard_report_tool().func(
                definition_path="bike-station-report.json"
            )
            if dashboard.get("ok") is False:
                raise RuntimeError(dashboard)
    finally:
        for token in reversed(tokens):
            ctx.reset(token)
    return {
        "session_id": session.id,
        "workspace_id": workspace.id,
        "report": report,
        "dashboard": dashboard,
        "synthetic": True,
        "authoring": "production tools, no model run",
    }
