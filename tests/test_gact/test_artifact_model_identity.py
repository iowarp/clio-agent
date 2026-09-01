"""Artifact identity in the MODEL lane — the id the wire lane already carried.

Live-qualification defect (session ``sess_44c51720ec91``, isolated backend): a tool
result that DESIGNATES a workspace file (designation-by-result — ``ndp_stage_resource``
returning ``local_path``) mints an artifact at tool completion, the wire lane gets
``artifact.created`` + a ``resource_link`` part carrying ``artifact_id``, and the
model-visible structured result carries NOTHING. The trace shows the model observing
``{"ok": true, "local_path": "...MTA1.CI.LY_.30.csv", "size_bytes": 50424246}`` while
``GET /v1/sessions/{sid}/artifacts`` already held
``artifact_567f9d920a2b4f3aa9822cb50712d3f7`` for that exact file, with the
``resource_link`` arriving at part seq 59-61 — AFTER the answer at seq 58. The agent
therefore cannot cite ``artifact://<artifact-id>`` (the ``clio.time-series.v1``
``dataUri`` grammar the visualize skill requires), honestly refuses to invent one, and
the artifact-backed chart can never be built.

Every model-lane test here drives the REAL execution boundary
(``SyncMCPToolExecutor.call_tool`` against a FastMCP server) with the REAL tool
observer installed, so the assertion binds the shipped path, not a helper. Each key
lock carries a sabotage note naming the neutralization that turns it red.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.model_identity import (
    ARTIFACTS_RESULT_KEY,
    annotate_workflow_state_artifacts,
    artifact_id_uri,
    merge_artifact_identity,
    take_call_artifacts,
)
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.delegation import _produced_turn_workflow_state
from clio_agent.gact.tool_observer import _make_tool_observer
from clio_agent.gact.workflow_state.schema import GENERIC_WORKFLOW_STATE_SCHEMA
from clio_agent.tools.execution import (
    SyncMCPToolExecutor,
    ToolRuntimeHooks,
    set_tool_runtime_fallback,
    set_tool_runtime_resolver,
)

_CSV_BODY = "time,east,north,up\n0,0.0,0.0,0.0\n1,0.1,0.2,0.3\n2,0.2,0.4,0.6\n"


def _workspace_session(client: TestClient, root: Path) -> tuple[str, str]:
    wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return wid, sid


def _designating_server() -> FastMCP:
    """A server whose tools designate outputs through BOTH designation channels."""

    server = FastMCP("designation-demo")

    @server.tool()
    def stage_resource(url: str, output_dir: str) -> dict[str, Any]:
        """Stage a remote resource: the written path rides ONLY the RESULT (GAP A)."""
        target = Path(output_dir) / "MTA1.CI.LY_.30.csv"
        target.write_text(_CSV_BODY, encoding="utf-8")
        return {"ok": True, "local_path": str(target), "size_bytes": target.stat().st_size}

    @server.tool()
    def render_plot(data_path: str, output_path: str) -> dict[str, Any]:
        """Render a plot: the written path rides an output ARG (the arg channel)."""
        Path(output_path).write_bytes(b"\x89PNG plotted")
        return {"success": True, "data_points": 3}

    @server.tool()
    def register_thing(output_path: str) -> dict[str, Any]:
        """A tool that DECLARES its own ``artifacts`` list (the create_artifact shape)."""
        Path(output_path).write_text("declared", encoding="utf-8")
        return {"message": "registered", "artifacts": [{"artifact_id": "tool_declared_id"}]}

    return server


@pytest.fixture()
def boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The REAL boundary: a live app + observer driving a real SyncMCPToolExecutor."""

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid, sid = _workspace_session(client, tmp_path)
        set_tool_runtime_resolver(None)
        set_tool_runtime_fallback(ToolRuntimeHooks(tool_observer=_make_tool_observer(app)))
        executor = SyncMCPToolExecutor(_designating_server(), timeout=10.0, client_factory=Client)
        try:
            yield client, app, wid, sid, executor
        finally:
            set_tool_runtime_fallback(ToolRuntimeHooks())
            executor.close()


# --------------------------------------------------------------------------- #
# 1. The defect itself: designation-by-RESULT (the ndp_stage_resource shape).
# --------------------------------------------------------------------------- #


def test_result_designated_output_identity_reaches_the_model(boundary, tmp_path: Path) -> None:
    _client, app, wid, _sid, executor = boundary

    model_text = executor.call_tool(
        "stage_resource", {"url": "https://ndp/x", "output_dir": str(tmp_path)}
    )

    staged = tmp_path / "MTA1.CI.LY_.30.csv"
    match = get_registry(app).find_version_by_path(wid, str(staged))
    assert match is not None, "the result-designated path must still mint (S5 GAP A)"
    minted_id = match[1].artifact_id

    observed = json.loads(model_text)
    # The tool's OWN facts are untouched: the merge is additive, never a rewrite.
    assert observed["ok"] is True
    assert observed["local_path"] == str(staged)
    # ...and the identity the wire lane already carried is now IN the model's hands.
    # Sabotage: drop the merge_artifact_identity step from
    # tool_hooks.assemble_model_observation -> the key is absent -> red (the live defect).
    assert observed[ARTIFACTS_RESULT_KEY] == [
        {"artifact_id": minted_id, "uri": f"artifact://{minted_id}", "path": str(staged)}
    ]


# --------------------------------------------------------------------------- #
# 2. SIBLING CHANNEL: the output-ARG designation must be covered identically.
# --------------------------------------------------------------------------- #


def test_arg_designated_output_identity_reaches_the_model(boundary, tmp_path: Path) -> None:
    _client, app, wid, _sid, executor = boundary
    source = tmp_path / "source.csv"
    source.write_text(_CSV_BODY, encoding="utf-8")
    out = tmp_path / "timeseries.png"

    model_text = executor.call_tool(
        "render_plot", {"data_path": str(source), "output_path": str(out)}
    )

    match = get_registry(app).find_version_by_path(wid, str(out))
    assert match is not None
    observed = json.loads(model_text)
    # Sabotage: restrict designated_result_paths to the RESULT channel only ->
    # the arg-channel plot loses its id -> red (the recurring sibling defect).
    assert observed[ARTIFACTS_RESULT_KEY] == [
        {
            "artifact_id": match[1].artifact_id,
            "uri": artifact_id_uri(match[1].artifact_id),
            "path": str(out),
        }
    ]


def test_tool_declared_artifacts_list_is_never_overwritten(boundary, tmp_path: Path) -> None:
    """A tool that declares its OWN ``artifacts`` (create_artifact) keeps its result."""

    _client, _app, _wid, _sid, executor = boundary
    out = tmp_path / "declared.csv"

    observed = json.loads(executor.call_tool("register_thing", {"output_path": str(out)}))

    # Sabotage: drop the ``ARTIFACTS_RESULT_KEY in parsed`` guard in
    # merge_artifact_identity -> the tool's own declaration is clobbered -> red.
    assert observed[ARTIFACTS_RESULT_KEY] == [{"artifact_id": "tool_declared_id"}]


# --------------------------------------------------------------------------- #
# 3. The A2UI acceptance shape: the id the model now has RESOLVES.
# --------------------------------------------------------------------------- #


def test_model_artifact_uri_drives_a2ui_time_series_preview(boundary, tmp_path: Path) -> None:
    """The whole point: the merged id builds a VALID ``clio.time-series.v1`` dataUri
    and that dataUri resolves against the bounded table-preview route."""

    from clio_schemas.a2ui_v091 import TimeSeriesComponent

    client, _app, _wid, _sid, executor = boundary
    observed = json.loads(
        executor.call_tool("stage_resource", {"url": "https://ndp/x", "output_dir": str(tmp_path)})
    )
    entry = observed[ARTIFACTS_RESULT_KEY][0]

    # (a) the URI the model can now write validates against the TRUSTED catalog's
    #     dataUri grammar (``^artifact://artifact_[A-Za-z0-9_-]+$``).
    component = TimeSeriesComponent(
        id="root", dataUri=entry["uri"], xKey="time", yKeys=["east", "north", "up"]
    )
    assert component.dataUri == f"artifact://{entry['artifact_id']}"

    # (b) ...and the id inside it resolves to the minted artifact's real bytes.
    # Sabotage: emit the logical version URI (artifact://<ws>/<name>@vN) as ``uri``
    # -> the component rejects it AND the id no longer addresses the preview -> red.
    preview = client.get(
        f"/v1/artifacts/{entry['artifact_id']}/table-preview",
        params={"columns": "time,east,north,up", "limit": 10},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["total_rows"] == 3
    assert body["sampled_rows"] == 3
    assert body["truncated"] is False
    assert body["rows"][0] == {"time": "0", "east": "0.0", "north": "0.0", "up": "0.0"}


# --------------------------------------------------------------------------- #
# 4. The workflow_state lane (the forwarding that already records local_path).
# --------------------------------------------------------------------------- #


def test_workflow_state_gains_artifact_id_beside_local_path(boundary, tmp_path: Path) -> None:
    _client, app, wid, sid, executor = boundary
    executor.call_tool("stage_resource", {"url": "https://ndp/x", "output_dir": str(tmp_path)})
    staged = tmp_path / "MTA1.CI.LY_.30.csv"
    minted_id = get_registry(app).find_version_by_path(wid, str(staged))[1].artifact_id

    # The turn's produced state, exactly as the model authored it (path, no identity).
    pred = type("Pred", (), {})()
    pred.workflow_state = {
        "acquisition": {"status": "staged", "analysis_ready": True, "local_path": str(staged)},
        "resolution": {"status": "resolved", "city": "Los Angeles"},
    }

    produced = _produced_turn_workflow_state(
        pred, [], app, sid, schema=GENERIC_WORKFLOW_STATE_SCHEMA
    )

    # Sabotage: drop app/sid from turn_finalize's _produced_turn_workflow_state call
    # -> the annotation never runs -> red (the next turn re-stages instead of reusing).
    assert produced["acquisition"]["artifact_id"] == minted_id
    assert produced["acquisition"]["artifact_uri"] == artifact_id_uri(minted_id)
    # Untouched fields survive verbatim; a section with no registered path gains nothing.
    assert produced["acquisition"]["local_path"] == str(staged)
    assert produced["resolution"] == {"status": "resolved", "city": "Los Angeles"}


def test_workflow_state_annotation_is_an_exact_join_and_add_only(boundary, tmp_path: Path) -> None:
    _client, app, _wid, sid, executor = boundary
    executor.call_tool("stage_resource", {"url": "https://ndp/x", "output_dir": str(tmp_path)})

    state = {
        # A path that names NO registered artifact: annotated with nothing (precision).
        "acquisition": {"local_path": str(tmp_path / "never_staged.csv")},
        # An identity the model already wrote is authoritative and never overwritten.
        "visualization": {
            "output_path": str(tmp_path / "MTA1.CI.LY_.30.csv"),
            "artifact_id": "model_authored_id",
        },
        "notes": "not a section",
    }

    annotated = annotate_workflow_state_artifacts(app, sid, state)

    # Sabotage: resolve by basename instead of the registry's absolute-path matcher
    # -> the unstaged sibling picks up a neighbour's id -> red.
    assert "artifact_id" not in annotated["acquisition"]
    assert annotated["visualization"]["artifact_id"] == "model_authored_id"
    assert annotated["notes"] == "not a section"


# --------------------------------------------------------------------------- #
# 5. Bounds + isolation: an identity is never attributed to a call that did not
#    produce it, and a publication never leaks across calls on the same thread.
# --------------------------------------------------------------------------- #


def test_publication_is_consumed_once_and_call_scoped(boundary, tmp_path: Path) -> None:
    _client, _app, _wid, _sid, executor = boundary
    executor.call_tool("stage_resource", {"url": "https://ndp/x", "output_dir": str(tmp_path)})

    # The boundary already consumed this call's publication; a later reader on this
    # thread inherits nothing, and a mismatched call id never claims another's id.
    # Sabotage: drop the call_id stamp/clear in take_call_artifacts -> a subsequent,
    # unrelated tool result would inherit this staged CSV's identity -> red.
    assert take_call_artifacts("call_someone_else") == []
    assert take_call_artifacts("") == []


def test_merge_annotates_a_non_object_result_without_losing_its_bytes() -> None:
    entries = [{"artifact_id": "artifact_a", "uri": "artifact://artifact_a", "path": "/w/a.csv"}]

    merged = merge_artifact_identity("plain tool text", entries)

    # A non-JSON-object result keeps its bytes verbatim and gains ONE visible note
    # (the boundary's own ``[path-repair]`` idiom) — never a silent drop.
    # Sabotage: return model_text unchanged for the non-object shape -> red.
    assert merged.startswith("plain tool text\n[artifacts] ")
    assert json.loads(merged.split("[artifacts] ", 1)[1]) == {ARTIFACTS_RESULT_KEY: entries}
    assert merge_artifact_identity("plain tool text", []) == "plain tool text"
