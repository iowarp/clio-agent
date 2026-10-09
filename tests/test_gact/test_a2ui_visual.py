"""Matching-view capture, ownership, stale rejection and native image delivery."""

from __future__ import annotations

import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from clio_agent.gact.a2ui_producer import (
    build_capture_a2ui_surface_tool,
    build_create_a2ui_surface_tool,
    build_inspect_a2ui_surface_tool,
    build_update_a2ui_components_tool,
    build_update_a2ui_data_model_tool,
)
from clio_agent.gact.a2ui_visual import (
    A2UIVisualFeedback,
    CaptureReply,
    ViewerReport,
    VisualFeedbackError,
    decode_png,
)
from clio_agent.gact.agents.auto_tools import build_auto_react_tools
from clio_agent.gact.agents.clio_react_record import result_part
from clio_agent.gact.types import AgentDef
from clio_agent.tools.execution import tool_workspace_context
from tests.test_gact.test_a2ui_producer import _advertise_workspace_catalog, _session


def png() -> str:
    """A real, bounded PNG with recognizable pixels for native hydration checks."""
    out = io.BytesIO()
    Image.new("RGB", (32, 16), "red").save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def viewer(viewer_id: str = "viewer-a", **changes: Any) -> ViewerReport:
    """One test renderer's report (browser behavior is covered separately)."""
    return ViewerReport.model_validate(
        {
            "viewer_id": viewer_id,
            "surface_id": "surface",
            "revision": 1,
            "view_revision": 0,
            "visible": True,
            "ready": True,
            "state": {"camera": {"zoom": 4}},
            **changes,
        }
    )


def claim(bridge: A2UIVisualFeedback, report: ViewerReport, sid: str = "sid") -> dict[str, Any]:
    """Wait for a real request to be assigned by the bridge, with a bounded budget."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        requests = bridge.report(sid, report)
        if requests:
            return requests[0]
        time.sleep(0.005)
    raise AssertionError("No capture request was assigned.")


def reply(job: dict[str, Any], report: ViewerReport, **changes: Any) -> CaptureReply:
    """Complete exactly the claimed request, with optional sabotage overrides."""
    return CaptureReply.model_validate(
        {
            "request_id": job["request_id"],
            "viewer_id": report.viewer_id,
            "revision": report.revision,
            "view_revision": report.view_revision,
            "png_base64": png(),
            **changes,
        }
    )


def test_two_viewers_cannot_race_or_complete_each_others_requests() -> None:
    bridge = A2UIVisualFeedback()
    first, other = viewer(), viewer("viewer-b", state={"camera": {"zoom": 8}})
    bridge.report("sid", first)
    bridge.report("sid", other)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(bridge.capture, "sid", "surface", 1, viewer_id=first.viewer_id)
        job = claim(bridge, first)
        assert bridge.report("sid", other) == []
        with pytest.raises(VisualFeedbackError, match="Another viewer"):
            bridge.complete("sid", reply(job, other))
        with pytest.raises(VisualFeedbackError, match="expired or is unknown"):
            bridge.complete("different-session", reply(job, first))
        bridge.complete("sid", reply(job, first))
        data, evidence = future.result(timeout=2)
        assert data == base64.b64decode(png())
        assert evidence["viewer"]["state"] == first.state
        assert evidence["width"] == 32 and evidence["height"] == 16
    with pytest.raises(VisualFeedbackError, match="expired or is unknown"):
        bridge.complete("sid", reply(job, first))


@pytest.mark.parametrize(
    "change", [{"view_revision": 1}, {"revision": 2, "view_revision": 1}, {"visible": False}]
)
def test_human_or_producer_changes_during_capture_refuse(change: dict[str, Any]) -> None:
    bridge = A2UIVisualFeedback()
    report = viewer()
    bridge.report("sid", report)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(bridge.capture, "sid", "surface", 1)
        claim(bridge, report)
        bridge.report("sid", viewer(**change))
        with pytest.raises(VisualFeedbackError):
            future.result(timeout=2)


def test_unavailable_stale_timeout_failure_and_malformed_pixels() -> None:
    bridge = A2UIVisualFeedback()
    with pytest.raises(VisualFeedbackError, match="Open the requested"):
        bridge.capture("sid", "surface", 1)
    bridge.report("sid", viewer())
    with pytest.raises(VisualFeedbackError, match="expected definition"):
        bridge.capture("sid", "surface", 2, view_revision=0)
    with pytest.raises(VisualFeedbackError, match="capture budget"):
        bridge.capture("sid", "surface", 1, timeout=0.02)
    for changes, reason in [
        ({"error": "Map tiles failed."}, "tiles failed"),
        ({"png_base64": "AA=="}, "cannot identify"),
    ]:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(bridge.capture, "sid", "surface", 1)
            job = claim(bridge, viewer())
            bridge.complete("sid", reply(job, viewer(), **changes))
            with pytest.raises(VisualFeedbackError, match=reason):
                future.result(timeout=2)
    with pytest.raises(VisualFeedbackError):
        decode_png("not base64")


def test_changed_state_requires_new_epoch_and_updated_renderer_can_catch_up() -> None:
    bridge = A2UIVisualFeedback()
    old = viewer()
    bridge.report("sid", old)
    with pytest.raises(VisualFeedbackError, match="new view epoch"):
        bridge.report("sid", viewer(state={"camera": {"zoom": 8}}))
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(bridge.capture, "sid", "surface", 2, timeout=2)
        updated = viewer(revision=2, view_revision=1)
        job = claim(bridge, updated)
        bridge.complete("sid", reply(job, updated))
        _, evidence = future.result(timeout=2)
        assert evidence["viewer"]["revision"] == 2


def test_capture_is_only_auto_attached_for_a_vision_capable_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _session(tmp_path, monkeypatch)
    definition = AgentDef(
        id="visual-main", title="CLIO", metadata={"definition_kind": "builtin_main"}
    )
    text_tools = build_auto_react_tools(definition, a2ui_producers=True, supports_vision=False)
    image_tools = build_auto_react_tools(definition, a2ui_producers=True, supports_vision=True)
    assert "capture_a2ui_surface" not in {tool.name for tool in text_tools}
    assert "capture_a2ui_surface" in {tool.name for tool in image_tools}


def test_unready_view_never_returns_previous_pixels() -> None:
    bridge = A2UIVisualFeedback()
    bridge.report("sid", viewer(ready=False))
    with pytest.raises(VisualFeedbackError, match="did not become ready"):
        bridge.capture("sid", "surface", 1, timeout=0.02)


def test_saved_view_control_requires_its_owner_and_advancing_epoch() -> None:
    bridge = A2UIVisualFeedback()
    before = viewer(artifact_id="artifact_saved")
    bridge.report("sid", before)
    arguments = {
        "artifact_id": "artifact_saved",
        "viewer_id": before.viewer_id,
        "view_revision": 0,
        "path": "/tab",
        "value": "detail",
    }
    with pytest.raises(VisualFeedbackError, match="Open and inspect"):
        bridge.control("other", "surface", 1, **arguments)
    with pytest.raises(VisualFeedbackError, match="selected saved view changed"):
        bridge.control("sid", "surface", 2, **arguments)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(bridge.control, "sid", "surface", 1, **arguments)
        job = claim(bridge, before)
        assert job["data_model_update"] == {"path": "/tab", "value": "detail"}
        with pytest.raises(VisualFeedbackError, match="operation in progress"):
            bridge.control("sid", "surface", 1, **arguments)
        with pytest.raises(VisualFeedbackError, match="viewer epochs"):
            bridge.complete("sid", reply(job, before, png_base64="", previous_view_revision=0))
        after = viewer(artifact_id="artifact_saved", view_revision=2, state={"tab": "detail"})
        bridge.report("sid", after)
        with pytest.raises(VisualFeedbackError, match="viewer epochs"):
            bridge.complete("sid", reply(job, after, previous_view_revision=1))
        bridge.complete("sid", reply(job, after, png_base64="", previous_view_revision=0))
        result = future.result(timeout=2)
        assert result["controlled"] is True
        assert result["viewer"]["state"] == {"tab": "detail"}
        assert result["viewer"]["view_revision"] == 2


def test_saved_view_control_cannot_survive_artifact_replacement() -> None:
    bridge = A2UIVisualFeedback()
    before = viewer(artifact_id="artifact_saved")
    bridge.report("sid", before)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            bridge.control,
            "sid",
            "surface",
            1,
            artifact_id="artifact_saved",
            viewer_id=before.viewer_id,
            view_revision=0,
            path="/tab",
            value="detail",
        )
        claim(bridge, before)
        bridge.report("sid", viewer(artifact_id="artifact_replacement", view_revision=1))
        with pytest.raises(VisualFeedbackError, match="view changed"):
            future.result(timeout=2)


def test_saved_view_control_reports_renderer_failure_instead_of_success() -> None:
    bridge = A2UIVisualFeedback()
    before = viewer(artifact_id="artifact_saved")
    bridge.report("sid", before)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            bridge.control,
            "sid",
            "surface",
            1,
            artifact_id="artifact_saved",
            viewer_id=before.viewer_id,
            view_revision=0,
            path="/tab",
            value="detail",
        )
        job = claim(bridge, before)
        bridge.complete(
            "sid",
            reply(
                job,
                before,
                png_base64="",
                previous_view_revision=0,
                error="The view changed before control.",
            ),
        )
        with pytest.raises(VisualFeedbackError, match="before control"):
            future.result(timeout=2)


def test_real_routes_tool_artifact_and_native_media_roundtrip(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    created = build_create_a2ui_surface_tool()(
        surface_id="surface",
        components=[{"id": "root", "component": "Text", "text": {"path": "/caption"}}],
    )
    report = viewer(revision=created["revision"])
    path = f"/v1/sessions/{sid}/a2ui/visual-feedback"
    with TestClient(app) as client:
        assert client.post(path, json=report.model_dump()).status_code == 200
        inspected = build_inspect_a2ui_surface_tool()(surface_id="surface")
        assert inspected["controls"][0]["path"] == "/caption"
        assert inspected["viewers"][0]["viewer_id"] == report.viewer_id
        assert client.post(path, json={"viewer_id": "bad"}).status_code == 422
        assert (
            client.post(
                "/v1/sessions/missing/a2ui/visual-feedback", json=report.model_dump()
            ).status_code
            == 404
        )

        def capture() -> dict[str, Any]:
            # The scoped context is real; the fake renderer exists only in this test.
            with tool_workspace_context(tmp_path):
                return build_capture_a2ui_surface_tool()(
                    surface_id="surface", expected_revision=created["revision"]
                )

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(capture)
            job = claim(app.state.a2ui_visual, report, sid)
            assert client.post(path, json=reply(job, report).model_dump()).status_code == 200
            result = future.result(timeout=5)
        assert result.get("ok") is not False, result
        assert result["artifact_id"].startswith("artifact_")
        assert "base64" not in str(result)
        with tool_workspace_context(tmp_path):
            native = result_part("call", "capture_a2ui_surface", result, False)
        assert native.content[0].media_type == "image/png"
        assert base64.b64decode(native.content[0].data) == base64.b64decode(png())
        assert result["sha256"] in native.content[1].text
        assert job["request_id"] in native.content[1].text
        # The normal session fold does not necessarily retain a tool-specific
        # workspace ContextVar. Hydrate from the session, not the server cwd.
        with tool_workspace_context(None):
            folded = result_part("call", "capture_a2ui_surface", result, False)
        assert folded.content[0].media_type == "image/png"
        assert folded.content[0].data == native.content[0].data


def test_conditional_updates_reject_a_concurrent_revision(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    created = build_create_a2ui_surface_tool()(
        surface_id="surface", components=[{"id": "root", "component": "Text", "text": "First"}]
    )
    update = build_update_a2ui_data_model_tool()
    applied = update(
        surface_id="surface", path="/x", value=1, expected_revision=created["revision"]
    )
    assert applied.get("ok") is not False
    for tool, args in [
        (update, {"path": "/x", "value": 2}),
        (
            build_update_a2ui_components_tool(),
            {"components": [{"id": "root", "component": "Text", "text": "Lost"}]},
        ),
    ]:
        rejected = tool(surface_id="surface", expected_revision=created["revision"], **args)
        assert rejected["reason"] == "a2ui_view_stale", rejected
        assert app.state.a2ui_store.get(sid, "surface").revision == applied["revision"]
