"""The delivery ledger records what HAPPENED, not what was planned."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.messaging import _dspy_files_from_parts, _dspy_images_from_parts
from clio_agent.gact.native_delivery_outcome import (
    NATIVE_ATTACHMENT_SKIP_REASONS,
    note_delivery_outcome,
    settle_native_deliveries,
)
from clio_agent.gact.parts import Part
from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.resource_delivery import (
    ResourceDeliveryRecord,
    ResourceDeliveryStore,
)
from tests._config_layer import set_config

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _app(tmp_path: Path) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            resource_store=ResourceStore(root=tmp_path / "resources", max_resource_bytes=1 << 20),
            resource_delivery_store=ResourceDeliveryStore(path=tmp_path / "deliveries.json"),
        )
    )


def _upload(app: Any, *, workspace_id: str, name: str, content: bytes, mime: str) -> Any:
    record, _replay = app.state.resource_store.create_or_resume(
        workspace_id=workspace_id,
        name=name,
        declared_size=len(content),
        claimed_mime=mime,
    )
    return app.state.resource_store.append(record.id, offset=0, data=content)


def _plan(app: Any, record: Any, *, message_id: str) -> ResourceDeliveryRecord:
    return app.state.resource_delivery_store.append(
        ResourceDeliveryRecord(
            workspace_id=record.workspace_id,
            resource_id=record.id,
            resource_revision=record.revision,
            resource_sha256=record.sha256,
            message_id=message_id,
            provider_id="claude_code",
            model_id="sonnet",
            representation="native",
            evidence_source="discovery_overlay",
            reason_code="native_image_input",
            reason="live model handshake reports image input",
        )
    )


def _part(record: Any) -> Part:
    return Part(
        type="resource_ref",
        resource_id=record.id,
        resource_revision=str(record.revision),
        name=record.name,
        media_type=record.detected_mime,
        metadata={"delivery": {"representation": "native"}},
    )


def test_a_planned_row_starts_unsettled() -> None:
    """The plan is a decision; nothing has happened yet."""

    planned = ResourceDeliveryRecord(
        workspace_id="ws",
        resource_id="res",
        resource_revision=1,
        resource_sha256="a" * 64,
        message_id="m",
        provider_id="claude_code",
        model_id="sonnet",
        representation="native",
        evidence_source="discovery_overlay",
        reason="planned",
    )
    assert planned.delivery_confirmed is None
    assert planned.delivery_reason_code == ""


def test_a_delivered_attachment_settles_confirmed(tmp_path: Path) -> None:
    app = _app(tmp_path)
    record = _upload(app, workspace_id="ws", name="cell.png", content=_PNG, mime="image/png")
    _plan(app, record, message_id="m_ok")
    part = _part(record)

    images = _dspy_images_from_parts([part], app=app, workspace_id="ws")
    settle_native_deliveries(app, workspace_id="ws", message_id="m_ok", parts=[part])

    assert len(images) == 1
    row = app.state.resource_delivery_store.list("ws")[0]
    assert row.delivery_confirmed is True
    assert row.delivery_reason_code == ""
    assert row.delivery_settled_at


def test_a_stale_revision_decline_reaches_the_ledger_with_its_own_reason(
    tmp_path: Path,
) -> None:
    """Five decline conditions returned None with no reason; each now names itself.

    The resource advances AFTER the plan was made, so the planned revision's bytes
    are gone -- and the plan must not be silently satisfied with different ones.
    """

    app = _app(tmp_path)
    record = _upload(app, workspace_id="ws", name="cell.png", content=_PNG, mime="image/png")
    _plan(app, record, message_id="m_bad")
    part = _part(record)
    app.state.resource_store._records[record.id].revision = record.revision + 1

    images = _dspy_images_from_parts([part], app=app, workspace_id="ws")
    settle_native_deliveries(app, workspace_id="ws", message_id="m_bad", parts=[part])

    assert images == []
    row = app.state.resource_delivery_store.list("ws")[0]
    assert row.delivery_confirmed is False
    assert row.delivery_reason_code == "resource_revision_mismatch"
    assert row.delivery_reason == NATIVE_ATTACHMENT_SKIP_REASONS["resource_revision_mismatch"]


def test_a_not_ready_resource_decline_names_itself(tmp_path: Path) -> None:
    app = _app(tmp_path)
    record = _upload(app, workspace_id="ws", name="cell.png", content=_PNG, mime="image/png")
    _plan(app, record, message_id="m_state")
    part = _part(record)
    app.state.resource_store._records[record.id].state = "quarantined"

    assert _dspy_images_from_parts([part], app=app, workspace_id="ws") == []
    settle_native_deliveries(app, workspace_id="ws", message_id="m_state", parts=[part])
    row = app.state.resource_delivery_store.list("ws")[0]
    assert row.delivery_confirmed is False
    assert row.delivery_reason_code == "resource_not_ready"


def test_a_media_type_mismatch_decline_names_itself(tmp_path: Path) -> None:
    app = _app(tmp_path)
    record = _upload(
        app, workspace_id="ws", name="notes.txt", content=b"plain text", mime="text/plain"
    )
    _plan(app, record, message_id="m_mime")
    part = _part(record)

    assert _dspy_images_from_parts([part], app=app, workspace_id="ws") == []
    settle_native_deliveries(app, workspace_id="ws", message_id="m_mime", parts=[part])
    row = app.state.resource_delivery_store.list("ws")[0]
    assert row.delivery_confirmed is False
    assert row.delivery_reason_code == "resource_media_type_mismatch"


def test_a_vanished_resource_decline_names_itself(tmp_path: Path) -> None:
    app = _app(tmp_path)
    record = _upload(app, workspace_id="ws", name="cell.png", content=_PNG, mime="image/png")
    _plan(app, record, message_id="m_gone")
    part = _part(record)
    app.state.resource_store.delete(record.workspace_id, record.id)

    assert _dspy_images_from_parts([part], app=app, workspace_id="ws") == []
    settle_native_deliveries(app, workspace_id="ws", message_id="m_gone", parts=[part])
    row = app.state.resource_delivery_store.list("ws")[0]
    assert row.delivery_confirmed is False
    assert row.delivery_reason_code == "resource_missing"


def test_an_oversized_resource_is_refused_before_it_is_read(tmp_path: Path) -> None:
    """The check runs on the RECORDED size -- no read, no base64 expansion."""

    set_config("resources.native_image_max_bytes", 8)
    app = _app(tmp_path)
    record = _upload(app, workspace_id="ws", name="big.png", content=_PNG, mime="image/png")
    _plan(app, record, message_id="m_big")
    part = _part(record)

    reads: list[Any] = []
    original_content_path = app.state.resource_store.content_path

    def _tracking_content_path(rec: Any) -> Any:
        reads.append(rec.id)
        return original_content_path(rec)

    app.state.resource_store.content_path = _tracking_content_path

    images = _dspy_images_from_parts([part], app=app, workspace_id="ws")
    settle_native_deliveries(app, workspace_id="ws", message_id="m_big", parts=[part])

    assert images == []
    assert reads == [], "the file must never be opened for an over-bound attachment"
    row = app.state.resource_delivery_store.list("ws")[0]
    assert row.delivery_confirmed is False
    assert row.delivery_reason_code == "resource_over_attachment_bound"


def test_a_pdf_decline_is_recorded_the_same_way(tmp_path: Path) -> None:
    app = _app(tmp_path)
    record = _upload(
        app, workspace_id="ws", name="paper.pdf", content=b"%PDF-1.4\n", mime="application/pdf"
    )
    _plan(app, record, message_id="m_pdf")
    part = _part(record)
    app.state.resource_store._records[record.id].revision = record.revision + 1

    assert _dspy_files_from_parts([part], app=app, workspace_id="ws") == []
    settle_native_deliveries(app, workspace_id="ws", message_id="m_pdf", parts=[part])
    row = app.state.resource_delivery_store.list("ws")[0]
    assert row.delivery_confirmed is False
    assert row.delivery_reason_code == "resource_revision_mismatch"


def test_an_uncatalogued_skip_reason_is_refused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with pytest.raises(ValueError, match="Unknown native attachment skip reason"):
        note_delivery_outcome(
            app, resource_id="res", revision="1", kind="image", reason="probably_fine"
        )
