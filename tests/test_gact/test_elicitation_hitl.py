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
        result = await ctx.elicit("Pick a color", response_type=Literal["red", "green", "blue"])
        action = getattr(result, "action", type(result).__name__)
        return f"action={action} value={getattr(result, 'data', None)}"

    return backend


def _run_tool_call_in_thread(
    client_ctx: Any, tool: str, holder: dict[str, Any]
) -> threading.Thread:
    """Dispatch ``tool`` on its own event loop in a worker thread.

    Faithful to production: the external MCP tool call runs on a worker-thread
    loop (``_run_external_mcp_tool_sync`` -> ``asyncio.run``) while the FastAPI
    answer route runs on the serving loop — so the park/resolve MUST be
    cross-loop-safe, never a ``threading.Event`` block on the async boundary.
    """

    # Server-initiated elicitation is handshake-era only (SEP-2577); a real legacy
    # server negotiates legacy naturally. Production keeps auto negotiation, so the
    # test pins legacy to stand in for a legacy server and exercise the fired path.
    if hasattr(client_ctx, "mode"):
        client_ctx.mode = "legacy"

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


def _wait_for_pending_question(
    client: TestClient, sid: str, timeout: float = 10.0
) -> dict[str, Any]:
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


def test_elicitation_client_preserves_modern_negotiation(client: TestClient) -> None:
    """RULING 1 / finding 3: wiring the handler must NOT force the legacy era.

    A non-eliciting server reached through the elicitation client negotiates its
    normal (modern) era and its tool call succeeds — the handler rides an
    auto-negotiated connection, never a forced legacy one.
    """

    from clio_agent.gact.elicitation_bridge import make_elicitation_client

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    backend = FastMCP("modern-backend")

    @backend.tool
    def ping() -> str:
        return "pong"

    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="ext", tool_name="ping"
    )
    client_ctx = make_elicitation_client(app, backend, invocation=invocation)

    async def _run() -> tuple[str, str]:
        async with client_ctx as c:
            out = await c.call_tool("ping", {})
            return str(getattr(out, "data", out)), str(c.protocol_version)

    result, protocol = asyncio.run(_run())
    assert result == "pong"
    assert protocol == "2026-07-28", f"handler wiring forced a non-modern era: {protocol}"


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
    assert not client.get(
        f"/v1/sessions/{child.id}/questions", params={"status": "pending"}
    ).json()["questions"]

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

    t = translate_form_schema({"type": "object", "properties": {"name": {"type": "string"}}})
    assert t.degrade is None
    assert t.kind == "freeform"
    assert t.fields[0]["name"] == "name"


def test_translate_boolean_field_is_confirmation() -> None:
    from clio_agent.gact.elicitation_bridge import translate_form_schema

    t = translate_form_schema({"type": "object", "properties": {"ok": {"type": "boolean"}}})
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

    t = translate_form_schema({"type": "object", "properties": {"x": {"type": "null"}}})
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


# ---------------------------------------------------------------------------
# Finding 4 — concurrent first registration converges on ONE waiter registry
# ---------------------------------------------------------------------------


def test_concurrent_first_elicitations_share_one_registry(client: TestClient) -> None:
    """Two worker loops racing the FIRST elicitation must not orphan a parked call.

    Both threads hit the lazily-created waiter registry simultaneously (barrier);
    the double-checked lock (finding 4) makes them converge on ONE dict, so BOTH
    questions are registered and resolvable — never rival dicts with one lost.
    """

    import mcp_types

    from clio_agent.gact import elicitation_bridge as eb

    app = client.app  # type: ignore[attr-defined]
    # No registry yet: force the concurrent-first-creation race.
    if hasattr(app.state, "elicitation_waiters"):
        delattr(app.state, "elicitation_waiters")

    sids = [_create_session(client), _create_session(client)]
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def _fire(idx: int) -> None:
        inv = MCPInvocationContext(
            invocation_id=f"i{idx}", session_id=sids[idx], namespace="ext", tool_name="t"
        )
        params = mcp_types.ElicitRequestFormParams(
            message="pick",
            requested_schema={"type": "object", "properties": {"v": {"type": "string"}}},
        )

        async def _call() -> Any:
            barrier.wait()
            return await eb.handle_elicitation(app, inv, "pick", params, timeout=10.0)

        results[str(idx)] = asyncio.run(_call())

    threads = [threading.Thread(target=_fire, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()

    # Both questions must appear (one shared registry holds both waiters).
    for sid in sids:
        _wait_for_pending_question(client, sid)
    assert len(app.state.elicitation_waiters) == 2, app.state.elicitation_waiters

    # Both resolve independently through the shared answer route.
    for sid in sids:
        q = client.get(f"/v1/sessions/{sid}/questions", params={"status": "pending"}).json()[
            "questions"
        ][0]
        client.post(
            f"/v1/sessions/{sid}/questions/{q['id']}/answer",
            json={"metadata": {"v": "ok"}},
        )
    for t in threads:
        t.join(timeout=10.0)
    assert results["0"].action == "accept" and results["1"].action == "accept"


# ---------------------------------------------------------------------------
# Finding 6 — timeout atomically terminalizes the question + clears the anchor
# ---------------------------------------------------------------------------


def test_timeout_expires_question_clears_anchor_and_blocks_late_answer(client: TestClient) -> None:
    """On timeout the tool gets cancel, the row goes ``expired``, the anchor clears,
    and a late answer is refused (409) rather than leaking into native handling."""

    import mcp_types

    from clio_agent.gact.elicitation_bridge import handle_elicitation

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="ext", tool_name="t"
    )
    params = mcp_types.ElicitRequestFormParams(
        message="pick",
        requested_schema={"type": "object", "properties": {"v": {"type": "string"}}},
    )
    holder: dict[str, Any] = {}

    def _worker() -> None:
        holder["result"] = asyncio.run(
            handle_elicitation(app, invocation, "pick", params, timeout=0.3)
        )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    question = _wait_for_pending_question(client, sid)
    thread.join(timeout=10.0)

    assert holder["result"].action == "cancel"
    row = app.state.user_questions[question["id"]]
    assert row.status == "expired"
    sess = app.state.sessions.get(sid)
    assert (sess.metadata or {}).get("pending_user_question_id", "") == ""
    # A late answer is refused, never leaked into native ask-user resolution.
    late = client.post(f"/v1/sessions/{sid}/questions/{question['id']}/answer", json={})
    assert late.status_code == 409, late.text


# ---------------------------------------------------------------------------
# Finding 7 — invalid form answers are 422 re-prompts, never invalid accepts
# ---------------------------------------------------------------------------


def test_required_integer_invalid_answer_is_422_then_valid_accepts(client: TestClient) -> None:
    """An invalid required-integer answer re-prompts (422, still pending); a valid
    answer then resolves the parked call with coerced content."""

    import mcp_types

    from clio_agent.gact.elicitation_bridge import handle_elicitation

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    invocation = MCPInvocationContext(
        invocation_id="inv", session_id=sid, namespace="ext", tool_name="t"
    )
    params = mcp_types.ElicitRequestFormParams(
        message="how many",
        requested_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )
    holder: dict[str, Any] = {}

    def _worker() -> None:
        holder["result"] = asyncio.run(
            handle_elicitation(app, invocation, "how many", params, timeout=10.0)
        )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    question = _wait_for_pending_question(client, sid)

    # Non-integer answer -> 422, question stays pending, future not resolved.
    bad = client.post(
        f"/v1/sessions/{sid}/questions/{question['id']}/answer",
        json={"metadata": {"count": "not-a-number"}},
    )
    assert bad.status_code == 422, bad.text
    assert app.state.user_questions[question["id"]].status == "pending"
    assert holder == {}

    # A valid integer answer resolves with coerced content.
    good = client.post(
        f"/v1/sessions/{sid}/questions/{question['id']}/answer",
        json={"metadata": {"count": "7"}},
    )
    assert good.status_code == 200, good.text
    thread.join(timeout=10.0)
    assert holder["result"].action == "accept"
    assert holder["result"].content == {"count": 7}


def test_validate_elicitation_answer_unit_rules() -> None:
    """Unit: required / enum / integer / additionalProperties validation rules."""

    from clio_agent.gact.elicitation_schema import validate_elicitation_answer
    from clio_agent.gact.types import UserQuestion

    def _q(fields: list[dict[str, Any]], *, additional: bool = True) -> UserQuestion:
        return UserQuestion(
            id="q",
            session_id="s",
            prompt="p",
            created_at="t",
            updated_at="t",
            source="mcp_elicitation",
            metadata={
                "elicitation": {
                    "mode": "form",
                    "fields": fields,
                    "additional_properties": additional,
                }
            },
        )

    # required missing
    q = _q([{"name": "count", "type": "integer", "required": True}])
    assert (
        validate_elicitation_answer(q, selected_options=[], answer="", answer_metadata={})
        is not None
    )
    # invalid integer
    assert (
        validate_elicitation_answer(
            q, selected_options=[], answer="", answer_metadata={"count": "x"}
        )
        is not None
    )
    # enum violation
    qe = _q([{"name": "c", "type": "string", "enum": ["a", "b"]}])
    assert (
        validate_elicitation_answer(qe, selected_options=["z"], answer="", answer_metadata={})
        is not None
    )
    # additionalProperties=False rejects extras
    qa = _q([{"name": "c", "type": "string"}], additional=False)
    assert (
        validate_elicitation_answer(
            qa, selected_options=[], answer="", answer_metadata={"c": "x", "extra": "1"}
        )
        is not None
    )
    # valid
    assert (
        validate_elicitation_answer(qe, selected_options=["a"], answer="", answer_metadata={})
        is None
    )


# ---------------------------------------------------------------------------
# Findings 1 + 2 — wiring through the real executor + receive-loop correlation
# ---------------------------------------------------------------------------


def test_correlation_registry_single_in_flight_and_ambiguity() -> None:
    """The correlation registry resolves the single in-flight record (binding the
    session), and declines (None) when >1 is open and none matches (RULING 2)."""

    from clio_agent.gact import elicitation_correlation as ec
    from clio_agent.tools.mcp_handlers import MCPInvocationContext

    ec._OPEN.clear()
    rec = ec.open_invocation(object(), session_id="s1", tool_name="ns_tool")
    # single in-flight -> resolves for any session key and binds it
    assert ec._resolve_for_session(111) is rec
    assert rec.session_key == 111
    # a second concurrent record: an unknown session key is ambiguous -> None
    rec2 = ec.open_invocation(object(), session_id="s2", tool_name="ns_tool")
    assert ec._resolve_for_session(999) is None
    # ...but the already-bound session still resolves precisely
    assert ec._resolve_for_session(111) is rec
    ec.close_invocation(rec)
    ec.close_invocation(rec2)
    assert ec._OPEN == []
    _ = MCPInvocationContext  # imported for signature documentation


def test_executor_elicitation_end_to_end_through_correlated_handler(client: TestClient) -> None:
    """End-to-end through the REAL AsyncMCPToolExecutor: the gateway-style correlated
    handler resolves its invocation at the receive loop from the correlation record
    the tool-call boundary opened, mints the question, and the answer route unblocks
    the tool (RULING 2 — not the make_elicitation_client wrapper)."""

    from clio_agent.gact import elicitation_correlation as ec
    from clio_agent.gact.elicitation_correlation import make_correlated_handlers
    from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
    from clio_agent.tools.mcp_handlers import MCPClientCapabilities
    from clio_agent.tools.mcp_runtime import make_mcp_client

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    ec._OPEN.clear()
    backend = _fake_backend()

    def _factory(target: Any) -> Any:
        c = make_mcp_client(
            target,
            handlers=make_correlated_handlers(),
            capabilities=MCPClientCapabilities(elicitation_form=True),
        )
        c.mode = "legacy"  # stand in for a legacy server that can elicit
        return c

    executor = AsyncMCPToolExecutor(backend, client_factory=_factory)
    holder: dict[str, Any] = {}

    def _worker() -> None:
        async def _call() -> str:
            # The tool-call boundary (the observer, in production) opens the per-call
            # correlation record; the handler resolves it at the receive loop.
            record = ec.open_invocation(app, session_id=sid, tool_name="pick_color")
            try:
                async with executor:
                    return await executor.call_tool("pick_color", {})
            finally:
                ec.close_invocation(record)

        try:
            holder["result"] = asyncio.run(_call())
        except Exception as exc:  # noqa: BLE001 - surfaced to the assertions
            holder["error"] = repr(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    question = _wait_for_pending_question(client, sid)
    assert question["source"] == "mcp_elicitation"
    assert [o["value"] for o in question["options"]] == ["red", "green", "blue"]
    resp = client.post(
        f"/v1/sessions/{sid}/questions/{question['id']}/answer",
        json={"selected_options": ["green"]},
    )
    assert resp.status_code == 200, resp.text
    thread.join(timeout=10.0)
    assert "error" not in holder, holder.get("error")
    assert holder.get("result") == "action=accept value=green"


def test_gateway_build_wires_correlated_elicitation_handler() -> None:
    """finding 1: build_gateway carries the elicitation handler onto declared-server
    backends (so a gateway tool call can reach the HITL surface)."""

    from fastmcp import FastMCP

    from clio_agent.gact.elicitation_correlation import (
        correlated_elicitation_handler,
        make_correlated_handlers,
    )
    from clio_agent.tools import gateway
    from clio_agent.tools.gateway import build_gateway, namespace_proxies
    from clio_agent.tools.mcp_config import MCPServerSpec

    backend = FastMCP("backend")

    @backend.tool
    def ping() -> str:
        return "pong"

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(gateway, "transport_for", lambda spec, cwd=None: backend)
    try:
        gw = build_gateway(
            {"ext": MCPServerSpec(name="ext", transport="stdio", command="x")},
            handlers=make_correlated_handlers(),
        )
        upstream = namespace_proxies(gw)["ext"].client_factory()
        cb = upstream._session_kwargs.get("elicitation_callback")
        assert cb is not None  # the handler reaches the real upstream call path
    finally:
        monkeypatch.undo()
    _ = correlated_elicitation_handler


# ---------------------------------------------------------------------------
# Blocker 2 (finding 6 reopened) — ONE atomic question-state transition
# ---------------------------------------------------------------------------


def _seed_pending_question(app: Any, sid: str, qid: str = "q_race") -> str:
    from clio_agent.gact.types import UserQuestion

    app.state.user_questions[qid] = UserQuestion(
        id=qid,
        session_id=sid,
        prompt="p",
        created_at="t",
        updated_at="t",
        source="mcp_elicitation",
        metadata={"elicitation": {"mode": "form", "fields": []}},
    )
    return qid


def test_claim_question_transition_is_atomic_first_wins(client: TestClient) -> None:
    """Answer / timeout / cancel racing the SAME pending question: exactly one wins,
    and the stored status matches the winner — never a double-win (finding 6)."""

    from clio_agent.gact.elicitation_bridge import claim_question_transition

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    qid = _seed_pending_question(app, sid)

    barrier = threading.Barrier(3)
    results: dict[str, Any] = {}

    def _claim(status: str) -> None:
        barrier.wait()
        results[status] = claim_question_transition(app, qid, status)

    threads = [
        threading.Thread(target=_claim, args=(s,), daemon=True)
        for s in ("answered", "expired", "cancelled")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    winners = [s for s, r in results.items() if r is not None]
    assert len(winners) == 1, results
    assert app.state.user_questions[qid].status == winners[0]


def test_terminalize_never_overwrites_a_landed_answer(client: TestClient) -> None:
    """Once answered, a losing timeout/cancel claim is a no-op — no expire-over-answer."""

    from clio_agent.gact.elicitation_bridge import claim_question_transition

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    qid = _seed_pending_question(app, sid)

    assert claim_question_transition(app, qid, "answered", answer="x") is not None
    # the timeout arrives late: it must LOSE and leave the answer intact
    assert claim_question_transition(app, qid, "expired") is None
    assert app.state.user_questions[qid].status == "answered"
    assert app.state.user_questions[qid].answer == "x"


def test_answer_vs_answer_second_is_409(client: TestClient) -> None:
    """Two answers to the same question: one 200, the other 409 (atomic claim)."""

    app = client.app  # type: ignore[attr-defined]
    sid = _create_session(client)
    qid = _seed_pending_question(app, sid)
    app.state.sessions.update(sid, metadata_patch={"pending_user_question_id": qid})

    barrier = threading.Barrier(2)
    codes: list[int] = []
    lock = threading.Lock()

    def _answer() -> None:
        barrier.wait()
        resp = client.post(f"/v1/sessions/{sid}/questions/{qid}/answer", json={"answer": "a"})
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=_answer, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert sorted(codes) == [200, 409], codes


# ---------------------------------------------------------------------------
# Blocker 3 (finding 7 reopened) — exact JSON-Schema scalar validation
# ---------------------------------------------------------------------------


def test_scalar_validation_rejects_invalid_answers() -> None:
    """Post-coercion scalar rules reject the probe's invalid values (finding 7)."""

    from clio_agent.gact.elicitation_schema import validate_elicitation_answer
    from clio_agent.gact.types import UserQuestion

    def _q(fields: list[dict[str, Any]]) -> UserQuestion:
        return UserQuestion(
            id="q",
            session_id="s",
            prompt="p",
            created_at="t",
            updated_at="t",
            source="mcp_elicitation",
            metadata={"elicitation": {"mode": "form", "fields": fields}},
        )

    def _bad(field: dict[str, Any], value: Any) -> bool:
        return (
            validate_elicitation_answer(
                _q([field]), selected_options=[], answer="", answer_metadata={field["name"]: value}
            )
            is not None
        )

    intf = {"name": "n", "type": "integer"}
    numf = {"name": "n", "type": "number"}
    boolf = {"name": "n", "type": "boolean"}
    empty_enum = {"name": "n", "type": "string", "enum": []}

    assert _bad(intf, 1.5)  # non-integral float
    assert _bad(intf, True)  # bool is not an integer
    assert _bad(intf, "1.5")  # non-integral string
    assert _bad(numf, False)  # bool is not a number
    assert _bad(numf, float("inf"))  # non-finite
    assert _bad(boolf, "maybe")  # unrecognised bool string (never coerce-to-false)
    assert _bad(empty_enum, "anything")  # empty enum admits nothing

    # ...and the valid coercions still pass
    def _ok(field: dict[str, Any], value: Any) -> bool:
        return (
            validate_elicitation_answer(
                _q([field]), selected_options=[], answer="", answer_metadata={field["name"]: value}
            )
            is None
        )

    assert _ok(intf, "5") and _ok(intf, 5)
    assert _ok(numf, "1.5") and _ok(numf, 2)
    assert _ok(boolf, "true") and _ok(boolf, False)


# ---------------------------------------------------------------------------
# Blocker 4 (finding 2 partial) — gateway advertises url only when trusted
# ---------------------------------------------------------------------------


def test_correlated_capabilities_declares_url_only_when_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway capability declaration advertises form always but url ONLY when a
    trust allow-list is configured — never url over-advertisement (finding 2)."""

    import clio_agent.conf as conf
    from clio_agent.gact.elicitation_correlation import correlated_capabilities

    conf.reload()
    monkeypatch.delenv("CLIO_MCP_ELICITATION_URL_TRUSTED_ORIGINS", raising=False)
    caps = correlated_capabilities()
    assert caps.elicitation_form is True and caps.elicitation_url is False
    env = caps.elicitation_capability()
    assert env is not None and env.form is not None and env.url is None

    monkeypatch.setenv("CLIO_MCP_ELICITATION_URL_TRUSTED_ORIGINS", "https://trusted.example")
    conf.reload()
    caps2 = correlated_capabilities()
    assert caps2.elicitation_form is True and caps2.elicitation_url is True
    env2 = caps2.elicitation_capability()
    assert env2 is not None and env2.form is not None and env2.url is not None
    conf.reload()
