"""Adversarial-review fixes for S5's action dispatcher (issue #1372 follow-up).

Failing-first reproducers for the reviewer's 11 findings: the idempotency
race (#1, BLOCKING), delivery exceptions stranding records at ``received``
(#2, BLOCKING), unbounded VALIDATION_FAILED re-drive on an unknown surface
(#3, BLOCKING), repair accounting only counting actually-delivered attempts
(#4), duplicate-of-a-failed-record re-raising the original refusal (#5), the
idempotency lookup running before the data-model guards (#6), threading the
real ``deps`` instead of ``_CancelDepsShim`` (#7), and the error-record
payload/reason-text fixes (#8, #9). Plus the S7 composability-review
follow-ups: server-side ``context_schema`` enforcement (#12).
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.app import build_app
from clio_agent.gact.protocol.constants import A2UI_V091

from .test_a2ui_v3 import HEADERS, _create_message, _session_client

WORKSPACE_CATALOG_ID = workspace_catalog_id()


def _install_context_schema_event(app: Any, monkeypatch: MonkeyPatch) -> None:
    """Patch the workspace catalog's sidecar to declare a context_schema'd event.

    A SYNTHETIC sidecar (never an installed pack fixture -- see finding #13),
    so this carries no risk of leaking a fixture pack into the developer's
    real Agent Blueprint registry.
    """

    from clio_schemas.a2ui.sidecar import CatalogSidecar

    registry = app.state.a2ui_catalogs
    original_get = registry.get

    def patched_get(catalog_id: str, protocol_version: str = A2UI_V091) -> Any:
        entry = original_get(catalog_id, protocol_version)
        if entry is None or catalog_id != WORKSPACE_CATALOG_ID:
            return entry
        events = {
            name: route.model_dump(exclude_none=True)
            for name, route in entry.sidecar.events.items()
        }
        events["stations.search"] = {
            "destination": "agent",
            "context_schema": {
                "type": "object",
                "required": ["searchId", "stationIds"],
                "properties": {
                    "searchId": {"type": "string"},
                    "stationIds": {"type": "array", "minItems": 1},
                },
            },
        }
        new_sidecar = CatalogSidecar.model_validate(
            {**entry.sidecar.model_dump(by_alias=True, exclude_none=True), "events": events}
        )
        return replace(entry, sidecar=new_sidecar)

    monkeypatch.setattr(registry, "get", patched_get)


def _stub_spawn(app: Any, monkeypatch: MonkeyPatch) -> list[Any]:
    spawned: list[Any] = []

    def _spawn(coro: Any, **_kwargs: Any) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(app.state.turn_runner, "spawn", _spawn)
    return spawned


def _event(name: str, surface_id: str, context: dict[str, Any], timestamp: str) -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "action": {
            "name": name,
            "surfaceId": surface_id,
            "sourceComponentId": "source",
            "timestamp": timestamp,
            "context": context,
        },
    }


def _error_envelope(surface_id: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"version": "v0.9.1", "error": {"code": code, "surfaceId": surface_id, **extra}}


# --------------------------------------------------------------------------- #
# #1 BLOCKING: idempotency race                                              #
# --------------------------------------------------------------------------- #


def test_concurrent_duplicate_submission_persists_exactly_one_record(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="race")
    sid = session.id
    app.state.a2ui_store.apply_batch(sid, [_create_message()])
    spawned = _stub_spawn(app, monkeypatch)

    import clio_agent.gact.a2ui_actions.dispatcher as dispatcher_module

    real_compute = dispatcher_module.compute_idempotency_key
    barrier = threading.Barrier(2)

    def delayed_compute(*args: Any, **kwargs: Any) -> str:
        barrier.wait(timeout=5)
        time.sleep(0.15)
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(dispatcher_module, "compute_idempotency_key", delayed_compute)

    action = _event("earthscope.stations.selected", "surface_1", {"x": 1}, "2026-09-17T00:00:00Z")
    results: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        async def _call() -> Any:
            return await app.state.dispatch_a2ui_action(
                sid, {"message": action}, protocol_version=A2UI_V091
            )

        try:
            results.append(asyncio.run(_call()))
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert len(results) == 2
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert len(surface.actions) == 1, f"expected exactly one record, got {len(surface.actions)}"
    assert len(spawned) == 1, "exactly one turn must have started, not two"
    assert results[0]["action_id"] == results[1]["action_id"]


# --------------------------------------------------------------------------- #
# #2 BLOCKING: delivery exceptions strand records at received                #
# --------------------------------------------------------------------------- #


def test_run_retry_unknown_message_fails_the_record_and_still_404s(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event("run.retry", "surface_1", {"message_id": "msg_nope"}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 404, response.text
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["state"] == "failed"
    assert record["delivery"] == "rejected"


def test_agent_delivery_exception_fails_the_record_and_still_500s(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("simulated turn-start failure")

    monkeypatch.setattr("clio_agent.gact.turn._start_background_user_turn", _boom)
    action = _event("agent.submit", "surface_1", {"text": "go"}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 500
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["state"] == "failed"
    assert record["delivery"] == "rejected"


# --------------------------------------------------------------------------- #
# #3 BLOCKING: VALIDATION_FAILED on an unknown surface never re-drives       #
# --------------------------------------------------------------------------- #


def test_three_ghost_surface_errors_are_three_two_hundreds_zero_turns(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    spawned = _stub_spawn(app, monkeypatch)
    envelope = _error_envelope("ghost", "VALIDATION_FAILED", path="/x", message="boom")

    responses = [
        client.post(f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope})
        for _ in range(3)
    ]

    assert [r.status_code for r in responses] == [200, 200, 200]
    for r in responses:
        assert r.json()["reason"] == "a2ui_error_surface_unknown"
        assert r.json()["delivery"] in ("", "rejected")
    assert not spawned


# --------------------------------------------------------------------------- #
# #4: repair accounting only counts actually-delivered attempts              #
# --------------------------------------------------------------------------- #


def test_uncorrelated_waiting_user_repair_does_not_burn_the_repair_budget(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    app.state.agent = object()
    _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    app.state.sessions.update(sid, status="waiting_user")
    envelope = _error_envelope("surface_1", "VALIDATION_FAILED", path="/x", message="boom")

    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )
    assert first.status_code == 409
    assert first.json()["error"]["error"] == "a2ui_waiting_user_uncorrelated"

    app.state.sessions.update(sid, status="idle")
    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )

    assert second.status_code == 200, second.text
    assert second.json()["delivery"] == "start", "the repair budget must not have been burned"
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert surface.state != "failed"


# --------------------------------------------------------------------------- #
# #5: duplicate of a failed record re-raises the original refusal            #
# --------------------------------------------------------------------------- #


def test_duplicate_of_a_failed_record_reraises_the_original_refusal(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event("run.retry", "surface_1", {"message_id": "msg_nope"}, "2026-09-17T00:00:00Z")

    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert first.status_code == 404
    assert second.status_code == 404, second.text
    assert second.json()["error"]["error"] == first.json()["error"]["error"]
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert len(surface.actions) == 1, (
        "a duplicate of a failed record must not re-deliver or re-record"
    )


# --------------------------------------------------------------------------- #
# #6: idempotency lookup runs before the data-model guards                   #
# --------------------------------------------------------------------------- #


def test_replay_never_re_records_the_foreign_surface_reason(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _stub_spawn(app, monkeypatch)
    create_owned = {
        "version": "v0.9.1",
        "createSurface": {
            "surfaceId": "owned-surface",
            "catalogId": WORKSPACE_CATALOG_ID,
            "sendDataModel": True,
        },
    }
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [create_owned]}
    )
    action = _event("agent.submit", "owned-surface", {"text": "go"}, "2026-09-17T00:00:00Z")
    body = {
        "message": action,
        "metadata": {
            "a2uiClientDataModel": {
                "version": "v0.9.1",
                "surfaces": {"owned-surface": {"x": 1}, "foreign-surface": {"y": 2}},
            }
        },
    }

    first = client.post(f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json=body)
    second = client.post(f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json=body)

    assert first.status_code == 200 and second.status_code == 200
    reasons = app.state.a2ui_catalogs.session_reasons(sid)
    foreign = [r for r in reasons if r["reason"] == "a2ui_data_model_foreign_surface"]
    assert len(foreign) == 1, "the replay must not re-record the reason"


# --------------------------------------------------------------------------- #
# #7: the real deps thread through, no shim                                  #
# --------------------------------------------------------------------------- #


def test_no_cancel_deps_shim_module_attribute() -> None:
    import clio_agent.gact.a2ui_actions.dispatcher as dispatcher_module

    assert not hasattr(dispatcher_module, "_CancelDepsShim")


def test_run_cancel_still_cancels_with_the_real_deps(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event("run.cancel", "surface_1", {}, "2026-09-17T00:00:00Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    assert app.state.sessions.get(sid).status == "cancelled"


# --------------------------------------------------------------------------- #
# #9: error record payload carries the error code as action_name + kind      #
# --------------------------------------------------------------------------- #


def test_error_record_lifecycle_event_carries_code_as_action_name_and_kind(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from clio_agent.gact.events import Event

    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    published: list[Event] = []
    original = app.state.bus.publish

    def _capture(event: Event) -> None:
        published.append(event)
        original(event)

    monkeypatch.setattr(app.state.bus, "publish", _capture)
    envelope = _error_envelope("surface_1", "SOME_OTHER_ERROR", message="oops")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )

    assert response.status_code == 200, response.text
    failed_events = [e for e in published if e.type == "a2ui.action.failed"]
    assert failed_events, "expected an a2ui.action.failed event"
    payload = failed_events[-1].payload
    assert payload["action_name"] == "SOME_OTHER_ERROR"
    assert payload["kind"] == "error"


# --------------------------------------------------------------------------- #
# #12: sidecar-declared context_schema is enforced server-side               #
# --------------------------------------------------------------------------- #


def test_context_schema_rejects_empty_required_array(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _install_context_schema_event(app, monkeypatch)
    _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event(
        "stations.search", "surface_1", {"searchId": "x", "stationIds": []}, "2026-09-17T00:00:00Z"
    )

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["error"] == "a2ui_event_context_invalid"
    assert "/stationIds" in response.json()["error"]["message"]
    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    [record] = surface.actions
    assert record["state"] == "failed"
    assert record["delivery"] == "rejected"
    assert record["reason"] == "a2ui_event_context_invalid"


def test_context_schema_rejects_a_missing_required_field(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _install_context_schema_event(app, monkeypatch)
    _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event("stations.search", "surface_1", {"stationIds": ["P1"]}, "2026-09-17T00:00:01Z")

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["error"] == "a2ui_event_context_invalid"


def test_context_schema_delivers_a_conforming_context(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _install_context_schema_event(app, monkeypatch)
    _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event(
        "stations.search",
        "surface_1",
        {"searchId": "x", "stationIds": ["P1"]},
        "2026-09-17T00:00:02Z",
    )

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "delivered"


def test_events_without_context_schema_are_unchanged(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    _install_context_schema_event(app, monkeypatch)
    _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event(
        "earthscope.stations.selected", "surface_1", {"anything": "goes"}, "2026-09-17T00:00:03Z"
    )

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "delivered"


__all__: list[str] = []
