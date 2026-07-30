"""MCP elicitation bridged into the HITL/questions pipeline (#1113, P1.3).

Server-initiated ``elicitation/create`` (form + url) is a handshake-era MCP
capability (the 2026-07-28 era removed the back-channel — SEP-2577). CLIO wires
an elicitation handler that mints a :class:`~clio_agent.gact.types.UserQuestion`
on the SAME ``app.state.user_questions`` store + ``pending_user_question_id``
anchor + answer route as native asks (RULE 4: one surface, no parallel store),
parks the in-flight tool call on an async-safe future, and returns the SDK
``ElicitResult`` when the user answers.

Correlation is by protocol identity: the handler is bound, per tool call, to the
:class:`~clio_agent.tools.mcp_handlers.MCPInvocationContext` captured where the
call is issued (one client per call), never a client-keyed registry.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from fastmcp import Context, FastMCP

from clio_agent.gact.app import build_app
from clio_agent.tools.mcp_handlers import MCPInvocationContext


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def _create_session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "elicit"}).json()["id"]


def _fake_backend() -> Any:
    """An in-memory FastMCP server whose tool elicits mid-call."""

    backend = FastMCP("elicit-backend")

    @backend.tool
    async def pick_color(ctx: Context) -> str:
        result = await ctx.elicit(
            "Pick a color", response_type=Literal["red", "green", "blue"]
        )
        action = getattr(result, "action", type(result).__name__)
        return f"action={action} value={getattr(result, 'data', None)}"

    return backend


def _run_tool_call_in_thread(client_ctx: Any, tool: str, holder: dict[str, Any]) -> threading.Thread:
    """Dispatch ``tool`` on its own event loop in a worker thread.

    Faithful to production: the external MCP tool call runs on a worker-thread
    loop (``_run_external_mcp_tool_sync`` -> ``asyncio.run``) while the FastAPI
    answer route runs on the serving loop — so the park/resolve MUST be
    cross-loop-safe, never a ``threading.Event`` block on the async boundary.
    """

    def _worker() -> None:
        async def _call() -> str:
            async with client_ctx as c:
                out = await c.call_tool(tool, {})
            return str(getattr(out, "data", out))

        try:
            holder["result"] = asyncio.run(_call())
        except Exception as exc:  # noqa: BLE001 - surfaced to the test assertions
            holder["error"] = repr(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def _wait_for_pending_question(client: TestClient, sid: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = client.get(f"/v1/sessions/{sid}/questions", params={"status": "pending"}).json()
        questions = rows.get("questions", [])
        if questions:
            return questions[0]
        time.sleep(0.02)
    raise TimeoutError("elicitation question never appeared on the questions surface")


def test_elicitation_mid_tool_call_blocks_and_answer_route_unblocks(client: TestClient) -> None:
    """A mid-tool-call elicitation blocks; answering via the answer route unblocks it.

    Acceptance (#1113): a fake server issues an elicitation mid-tool-call — the
    call blocks, a UserQuestion appears with options translated from the JSON
    schema, answering via the EXISTING answer route unblocks the call and the
    tool receives the response.
    """

    from clio_agent.gact.elicitation_bridge import make_elicitation_client

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    backend = _fake_backend()
    invocation = MCPInvocationContext(
        invocation_id="inv-color",
        session_id=sid,
        namespace="ext",
        tool_name="pick_color",
    )

    client_ctx = make_elicitation_client(app, backend, invocation=invocation)
    holder: dict[str, Any] = {}
    thread = _run_tool_call_in_thread(client_ctx, "pick_color", holder)

    # The call blocks until answered: a pending UserQuestion appears, translated
    # from the elicitation form schema (enum -> options).
    question = _wait_for_pending_question(client, sid)
    assert holder == {}, "the tool call must block until the elicitation is answered"
    assert question["source"] == "mcp_elicitation"
    assert question["kind"] == "choice"
    assert [o["value"] for o in question["options"]] == ["red", "green", "blue"]

    # Answering through the existing answer route unblocks the call.
    resp = client.post(
        f"/v1/sessions/{sid}/questions/{question['id']}/answer",
        json={"selected_options": ["blue"]},
    )
    assert resp.status_code == 200, resp.text

    thread.join(timeout=10.0)
    assert not thread.is_alive(), "the tool call did not unblock after the answer"
    assert "error" not in holder, holder.get("error")
    assert holder.get("result") == "action=accept value=blue"


def test_declining_via_answer_route_returns_sdk_decline(client: TestClient) -> None:
    """A user decline delivers the SDK decline result to the tool, not a hang/crash."""

    from clio_agent.gact.elicitation_bridge import make_elicitation_client

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="ext", tool_name="pick_color"
    )
    client_ctx = make_elicitation_client(app, _fake_backend(), invocation=invocation)
    holder: dict[str, Any] = {}
    thread = _run_tool_call_in_thread(client_ctx, "pick_color", holder)

    question = _wait_for_pending_question(client, sid)
    resp = client.post(
        f"/v1/sessions/{sid}/questions/{question['id']}/answer",
        json={"metadata": {"elicitation_action": "decline"}},
    )
    assert resp.status_code == 200, resp.text

    thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert holder.get("result") == "action=decline value=None"


def test_cancelling_via_cancel_route_returns_sdk_cancel(client: TestClient) -> None:
    """Cancelling the question delivers the SDK cancel result to the tool."""

    from clio_agent.gact.elicitation_bridge import make_elicitation_client

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="ext", tool_name="pick_color"
    )
    client_ctx = make_elicitation_client(app, _fake_backend(), invocation=invocation)
    holder: dict[str, Any] = {}
    thread = _run_tool_call_in_thread(client_ctx, "pick_color", holder)

    question = _wait_for_pending_question(client, sid)
    resp = client.post(f"/v1/sessions/{sid}/questions/{question['id']}/cancel")
    assert resp.status_code == 200, resp.text

    thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert holder.get("result") == "action=cancel value=None"


def test_child_session_elicitation_forwards_to_parent_and_resolves(client: TestClient) -> None:
    """An unattended child's elicitation is surfaced on the PARENT and resolves it.

    Acceptance (#1113): child-session elicitation forwards to the parent's HITL
    surface; answering there wakes the child's parked tool call. The parked future
    stays keyed by the question id, so no client-keyed registry is involved.
    """

    from clio_agent.gact.elicitation_bridge import make_elicitation_client

    app = client.app  # type: ignore[attr-defined]
    parent = app.state.sessions.create(workspace_id="ws_default", title="parent")  # type: ignore[attr-defined]
    child = app.state.sessions.create(  # type: ignore[attr-defined]
        workspace_id="ws_default", title="child", parent_session_id=parent.id
    )
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=child.id, namespace="ext", tool_name="pick_color"
    )
    client_ctx = make_elicitation_client(app, _fake_backend(), invocation=invocation)
    holder: dict[str, Any] = {}
    thread = _run_tool_call_in_thread(client_ctx, "pick_color", holder)

    # The question surfaces on the PARENT (attended) session, not the child.
    question = _wait_for_pending_question(client, parent.id)
    assert question["metadata"]["elicitation"]["forwarded_from_session"] == child.id
    assert not client.get(f"/v1/sessions/{child.id}/questions", params={"status": "pending"}).json()[
        "questions"
    ]

    resp = client.post(
        f"/v1/sessions/{parent.id}/questions/{question['id']}/answer",
        json={"selected_options": ["green"]},
    )
    assert resp.status_code == 200, resp.text
    thread.join(timeout=10.0)
    assert holder.get("result") == "action=accept value=green"


# --------------------------------------------------------------------------- #
# Schema translation (form mode) — unit
# --------------------------------------------------------------------------- #


def test_translate_string_field_is_freeform() -> None:
    from clio_agent.gact.elicitation_bridge import translate_form_schema

    t = translate_form_schema(
        {"type": "object", "properties": {"name": {"type": "string"}}}
    )
    assert t.degrade is None
    assert t.kind == "freeform"
    assert t.fields[0]["name"] == "name"


def test_translate_boolean_field_is_confirmation() -> None:
    from clio_agent.gact.elicitation_bridge import translate_form_schema

    t = translate_form_schema(
        {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    )
    assert t.kind == "confirmation"


def test_translate_enum_field_is_choice_with_options() -> None:
    from clio_agent.gact.elicitation_bridge import translate_form_schema

    t = translate_form_schema(
        {
            "type": "object",
            "properties": {"size": {"type": "string", "enum": ["s", "m", "l"]}},
        }
    )
    assert t.kind == "choice"
    assert [o.value for o in t.options] == ["s", "m", "l"]


def test_translate_number_and_defaults_are_carried() -> None:
    from clio_agent.gact.elicitation_bridge import translate_form_schema

    t = translate_form_schema(
        {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "default": 3},
                "ratio": {"type": "number"},
            },
        }
    )
    assert t.kind == "freeform"  # multi-field renders from fields descriptor
    by_name = {f["name"]: f for f in t.fields}
    assert by_name["count"]["default"] == 3
    assert by_name["ratio"]["type"] == "number"


def test_translate_non_object_schema_degrades() -> None:
    from clio_agent.gact.elicitation_bridge import translate_form_schema

    t = translate_form_schema({"type": "string"})
    assert t.degrade == "elicitation_schema_not_object"


def test_translate_nested_schema_degrades_not_flat() -> None:
    from clio_agent.gact.elicitation_bridge import translate_form_schema

    t = translate_form_schema(
        {
            "type": "object",
            "properties": {"nested": {"type": "object", "properties": {}}},
        }
    )
    assert t.degrade == "elicitation_schema_not_flat"


def test_translate_unsupported_field_type_degrades() -> None:
    from clio_agent.gact.elicitation_bridge import translate_form_schema

    t = translate_form_schema(
        {"type": "object", "properties": {"x": {"type": "null"}}}
    )
    assert t.degrade == "elicitation_unsupported_field_type"


# --------------------------------------------------------------------------- #
# URL-mode trust
# --------------------------------------------------------------------------- #


def test_url_trust_rejects_untrusted_origin() -> None:
    from clio_agent.gact.elicitation_bridge import check_url_trust

    reason = check_url_trust("https://evil.example/oauth", ["https://trusted.example"])
    assert reason == "elicitation_url_untrusted_origin"


def test_url_trust_accepts_listed_origin() -> None:
    from clio_agent.gact.elicitation_bridge import check_url_trust

    assert check_url_trust("https://trusted.example/oauth", ["https://trusted.example"]) is None


def test_url_trust_rejects_insecure_scheme() -> None:
    from clio_agent.gact.elicitation_bridge import check_url_trust

    reason = check_url_trust("http://trusted.example/x", ["https://trusted.example"])
    assert reason == "elicitation_url_insecure_scheme"


def test_url_not_declared_when_no_trust_list() -> None:
    from clio_agent.gact.elicitation_bridge import check_url_trust

    assert check_url_trust("https://anything.example/x", []) == "elicitation_url_not_declared"


def test_url_elicitation_untrusted_origin_declines_without_minting(client: TestClient) -> None:
    """An untrusted url-mode elicitation is declined (typed reason) and mints nothing.

    URL-mode trust is decided from the origin alone; the bridge NEVER fetches the
    URL (``check_url_trust`` only parses it), so a rejected origin cannot leak the
    server anything and no question reaches the user.
    """

    import mcp_types

    from clio_agent.gact.elicitation_bridge import handle_elicitation

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="ext", tool_name="oauth"
    )
    params = mcp_types.ElicitRequestURLParams(message="Authorize", url="https://evil.example/oauth")
    result = asyncio.run(
        handle_elicitation(
            app,
            invocation,
            "Authorize",
            params,
            url_trusted_origins=["https://trusted.example"],
        )
    )
    assert result.action == "decline"
    assert not [q for q in app.state.user_questions.values() if q.session_id == sid]


def test_url_elicitation_trusted_origin_mints_confirmation(client: TestClient) -> None:
    """A trusted url-mode elicitation shows the FULL url in an isolated-container question."""

    import mcp_types

    # Drive the handler directly: server-side url-elicitation helpers vary by era,
    # and the bridge translation + trust + park is what P1.3 owns.
    from clio_agent.gact.elicitation_bridge import handle_elicitation

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="ext", tool_name="authorize"
    )
    params = mcp_types.ElicitRequestURLParams(
        message="Authorize CLIO", url="https://trusted.example/oauth"
    )
    holder: dict[str, Any] = {}

    def _worker() -> None:
        holder["result"] = asyncio.run(
            handle_elicitation(
                app,
                invocation,
                "Authorize CLIO",
                params,
                url_trusted_origins=["https://trusted.example"],
            )
        )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    question = _wait_for_pending_question(client, sid)
    assert question["kind"] == "confirmation"
    assert "https://trusted.example/oauth" in question["prompt"]
    assert question["metadata"]["elicitation"]["container"] == "isolated"
    assert question["metadata"]["elicitation"]["url"] == "https://trusted.example/oauth"

    resp = client.post(f"/v1/sessions/{sid}/questions/{question['id']}/answer", json={})
    assert resp.status_code == 200, resp.text
    thread.join(timeout=10.0)
    assert holder["result"].action == "accept"
