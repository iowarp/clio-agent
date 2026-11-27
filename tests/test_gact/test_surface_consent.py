"""One consent covers a server's view-only tools when a person acts on its surface."""

from __future__ import annotations

import contextvars
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import surface_consent
from clio_agent.gact.app import _make_permission_gate, _tool_session_context
from clio_agent.gact.permission_gate import _ask_and_wait
from clio_agent.gact.runtime.grant_resolver import resolve
from tests.test_gact.test_permissions import _client

pytestmark = pytest.mark.usefixtures("host_agent_executor")

LISTED = [
    {"name": "open_form", "_meta": {"ui": {"visibility": ["app"]}}},
    {"name": "save_form", "_meta": {"ui": {"visibility": ["app"]}}},
    SimpleNamespace(name="probe", meta={"ui": {"visibility": ["app"]}}),
    {"name": "describe", "_meta": {"ui": {"visibility": ["model", "app"]}}},
    {"name": "plain"},
    {"name": "model_only", "_meta": {"ui": {"visibility": ["model"]}}},
]
EXTERNAL = {"kind": "external_mcp", "annotations": {}}


def test_only_tools_the_model_cannot_see_share_the_consent() -> None:
    assert surface_consent.view_only_tools("srv", LISTED) == (
        "srv_open_form",
        "srv_save_form",
        "srv_probe",
    )
    assert surface_consent.for_surface("srv", "Server", [{"name": "plain"}]) is None


@pytest.mark.parametrize(
    ("place", "title", "summary"),
    [
        ("page", "Compute", "Let Server act on what you do in its Compute page"),
        ("tab", "Runs", "Let Server act on what you do in its Runs tab"),
        ("conversation", "", "Let Server act on what you do in its view"),
    ],
)
def test_the_question_names_the_server_and_where_the_person_acted(
    place: str, title: str, summary: str
) -> None:
    consent = surface_consent.for_surface("srv", "Server", LISTED, place=place, place_title=title)

    assert consent is not None and consent.summary == summary
    assert "·" not in consent.summary


def test_the_consent_covers_only_its_own_tools_inside_its_scope() -> None:
    consent = surface_consent.for_surface("srv", "Server", LISTED, place="page", place_title="P")

    assert surface_consent.covering("srv_open_form") is None
    with surface_consent.scope(consent):
        assert surface_consent.covering("srv_open_form") is consent
        assert surface_consent.covering("srv_describe") is None
    assert surface_consent.covering("srv_open_form") is None


def _ask_in_thread(app: Any, sid: str, name: str, consent: Any) -> tuple[threading.Thread, list]:
    decisions: list[str] = []

    def ask() -> None:
        with surface_consent.scope(consent):
            decisions.append(
                _ask_and_wait(
                    app,
                    sid,
                    name,
                    {"token": "t"},
                    EXTERNAL,
                    "external MCP tool",
                    "default",
                    "",
                    None,
                    {},
                )  # fmt: skip
            )

    context = contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(ask,), daemon=True)
    thread.start()
    return thread, decisions


def _pending(app: Any, name: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for row in app.state.permissions.values():
            if row.get("status") == "pending" and row["tool_call"]["tool_name"] == name:
                return row
        time.sleep(0.01)
    raise AssertionError(f"no pending permission for {name}")


@pytest.mark.parametrize("action", ["allow_session", "allow_workspace"])
def test_one_answer_grants_every_view_only_tool_of_the_server(tmp_path: Path, action: str) -> None:
    consent = surface_consent.for_surface(
        "srv", "Server", LISTED, place="page", place_title="Compute"
    )
    with _client(tmp_path, []) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "page"}).json()["id"]
        workspace = app.state.sessions.get(sid).workspace_id
        first, decided = _ask_in_thread(app, sid, "srv_open_form", consent)
        row = _pending(app, "srv_open_form")
        assert row["summary"] == "Let Server act on what you do in its Compute page"
        assert row["consent"]["tools"] == ["srv_open_form", "srv_save_form", "srv_probe"]
        listed = client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"]
        body = next(i for i in listed if i["id"] == f"permission:{row['id']}")
        assert body["title"] == row["summary"]
        assert body["payload"]["consent"]["place_title"] == "Compute"
        # A second control of the same surface asks while the first question is open.
        second, second_decided = _ask_in_thread(app, sid, "srv_probe", consent)
        waiting = _pending(app, "srv_probe")

        response = client.post(f"/v1/permissions/{row['id']}", json={"action": action})
        assert response.status_code == 204, response.text
        first.join(5)
        second.join(5)

        assert decided == ["allow"] and second_decided == ["allow"]
        assert waiting["reason"] == "permission_policy_updated"
        for tool in ("srv_open_form", "srv_save_form", "srv_probe"):
            assert (
                resolve(
                    "tool",
                    tool,
                    policies=app.state.permission_policies,
                    session_id=sid,
                    workspace_id=workspace,
                )
                == "allow"
            ), tool
        for tool in ("srv_describe", "srv_plain", "other_open_form"):
            assert (
                resolve(
                    "tool",
                    tool,
                    policies=app.state.permission_policies,
                    session_id=sid,
                    workspace_id=workspace,
                )
                != "allow"
            ), tool
        granted = [p["tool_name_pattern"] for p in app.state.permission_policies]
        assert sorted(granted) == ["srv_open_form", "srv_probe", "srv_save_form"]


def test_a_tool_outside_the_consent_keeps_its_own_question(tmp_path: Path) -> None:
    consent = surface_consent.for_surface("srv", "Server", LISTED, place="tab", place_title="T")
    with _client(tmp_path, []) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "tab"}).json()["id"]
        thread, decided = _ask_in_thread(app, sid, "srv_describe", consent)
        row = _pending(app, "srv_describe")
        assert "consent" not in row
        assert row["summary"] != consent.summary  # type: ignore[union-attr]

        response = client.post(f"/v1/permissions/{row['id']}", json={"action": "allow_session"})
        assert response.status_code == 204, response.text
        thread.join(5)

        assert decided == ["allow"]
        granted = [p["tool_name_pattern"] for p in app.state.permission_policies]
        assert granted == ["srv_describe"]


def test_only_a_click_on_the_plugins_own_page_is_its_consent() -> None:
    page = surface_consent.for_surface("srv", "Server", LISTED, place="page", place_title="P")
    tab = surface_consent.for_surface("srv", "Server", LISTED, place="tab", place_title="T")
    view = surface_consent.for_surface("srv", "Server", LISTED)

    with surface_consent.scope(page):
        assert surface_consent.page_click("srv_open_form") is page
        assert surface_consent.page_click("srv_describe") is None  # the model sees it too
    for other in (tab, view):
        with surface_consent.scope(other):
            assert surface_consent.page_click("srv_open_form") is None
    assert surface_consent.page_click("srv_open_form") is None


def test_a_click_on_the_page_runs_its_app_only_tool_without_a_question(tmp_path: Path) -> None:
    consent = surface_consent.for_surface(
        "srv", "Server", LISTED, place="page", place_title="Compute"
    )
    with _client(tmp_path, []) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "page"}).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid), surface_consent.scope(consent):
            assert gate("srv_open_form", {"token": "t"}, EXTERNAL) == "allow"

        rows = list(app.state.permissions.values())
        assert [row["status"] for row in rows] == ["auto_approved"]
        assert rows[0]["reason"] == surface_consent.REASON_PAGE_CLICK
        assert app.state.permission_policies == []  # nothing is granted beyond this call


def test_a_deny_rule_still_wins_over_a_page_click(tmp_path: Path) -> None:
    consent = surface_consent.for_surface(
        "srv", "Server", LISTED, place="page", place_title="Compute"
    )
    with _client(tmp_path, []) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "page"}).json()["id"]
        response = client.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": "srv_open_form",
                        "action": "deny",
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        gate = _make_permission_gate(app)
        with _tool_session_context(sid), surface_consent.scope(consent):
            assert gate("srv_open_form", {"token": "t"}, EXTERNAL) == "deny"


def test_a_tab_click_still_asks(tmp_path: Path) -> None:
    consent = surface_consent.for_surface("srv", "Server", LISTED, place="tab", place_title="T")
    with _client(tmp_path, []) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "tab"}).json()["id"]
        decided: list[str] = []

        def call() -> None:
            gate = _make_permission_gate(app)
            with _tool_session_context(sid), surface_consent.scope(consent):
                decided.append(gate("srv_open_form", {"token": "t"}, EXTERNAL))

        thread = threading.Thread(target=contextvars.copy_context().run, args=(call,))
        thread.start()
        row = _pending(app, "srv_open_form")
        assert row["consent"]["place"] == "tab"

        response = client.post(f"/v1/permissions/{row['id']}", json={"action": "deny"})
        assert response.status_code == 204, response.text
        thread.join(5)
        assert decided == ["deny"]
