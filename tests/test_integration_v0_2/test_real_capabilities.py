"""End-to-end v0.2 capability tests against a live clio-agent-gact
backed by a real LM (Meridian + Claude Haiku by default).

Each test exercises ONE capability through the real ClioAgent.
Pass = the wire shape works AND the real agent drives it. Where
the real agent is known to NOT drive a feature (per REAL_GAPS.md
+ the open issues), the test asserts "endpoint accepts the input
shape" and is marked xfail with the issue link so the suite stays
green while honestly tracking what's missing.
"""

from __future__ import annotations

import time

import httpx
import pytest

from .conftest import post_user, turn, wait_for_assistant

# ---- /v1/health + /v1/capabilities -----------------------------------------


def test_health_reports_lm_configured(http: httpx.Client) -> None:
    """Integration server must have an LM configured — otherwise
    every other test would fail at POST /messages with 503."""

    body = http.get("/v1/health").json()
    rows = {r["name"]: r for r in body["integrations"]}
    assert rows["lm"]["status"] == "ready", (
        f"LM not configured for integration run; got {rows.get('lm')}. "
        "PUT /v1/providers/lm before launching the suite."
    )
    assert rows["agent"]["status"] in {"ready", "degraded"}


def test_capabilities_advertises_v0_2(http: httpx.Client) -> None:
    body = http.get("/v1/capabilities").json()
    assert body["contract_version"] == "0.2"
    caps = body["capabilities"]
    # Capabilities verified end-to-end by this suite — must be true.
    for flag in (
        "sessions",
        "workspaces",
        "agent_routing",
        "memory",
        "session_branching",
        "search_messages",
        "files",
        "metrics",
        "structured_errors",
        "integration_health",
    ):
        assert caps[flag] is True, f"{flag} should be true"


# ---- workspaces (#12) ------------------------------------------------------


def test_workspaces_default_exists(http: httpx.Client) -> None:
    body = http.get("/v1/workspaces").json()
    ids = {w["id"] for w in body["workspaces"]}
    assert "ws_default" in ids


def test_workspace_create_then_use(http: httpx.Client) -> None:
    new = http.post("/v1/workspaces", json={"name": "integration-ws"}).json()
    sess = http.post(
        "/v1/sessions",
        json={"workspace_id": new["id"], "title": "scoped"},
    ).json()
    assert sess["workspace_id"] == new["id"]
    # Cleanup so repeated runs don't accumulate clutter.
    http.delete(f"/v1/workspaces/{new['id']}")


# ---- session forks (#10) --------------------------------------------------


def test_session_fork_copies_messages(
    http: httpx.Client, session_id: str
) -> None:
    """Fork a settled turn, assert child gets the same messages."""

    # Prompts that don't hit the heuristic router fall back to the
    # router LM + chat agent, which can take 30-60s through
    # Meridian. 300s gives us tail-latency headroom without making
    # the test feel hung.
    turn(http, session_id, "Reply with the single word PING.", timeout=300)
    fork = http.post(
        f"/v1/sessions/{session_id}/fork", json={"title": "forked"}
    ).json()
    assert fork["parent_session_id"] == session_id

    src_msgs = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
    fork_msgs = http.get(f"/v1/sessions/{fork['id']}/messages").json()["messages"]
    assert len(fork_msgs) == len(src_msgs)


# ---- message search (#11) -------------------------------------------------


def test_message_search_finds_user_text(
    http: httpx.Client, session_id: str
) -> None:
    """Substring search finds the user message we just posted."""

    needle = "magic-needle-12345"
    turn(http, session_id, f"Acknowledge with PING. Marker: {needle}.", timeout=300)
    body = http.get(
        f"/v1/sessions/{session_id}/messages/search",
        params={"q": needle},
    ).json()
    assert body["matches"], f"no search hits for {needle!r}"
    assert any(needle in m["snippet"] for m in body["matches"])


# ---- /v1/messages roundtrip + tokens (#8 partial) -------------------------


def test_real_turn_populates_tokens(
    http: httpx.Client, session_id: str
) -> None:
    """A real LM turn lands tokens.input + tokens.output > 0 from
    DSPy LM history (cost may be 0 from Meridian — see #8)."""

    a = turn(http, session_id, "Reply with just the word PING.", timeout=300)
    assert a["tokens"]["input"] >= 0
    assert a["tokens"]["output"] > 0, (
        f"expected >0 output tokens from real Claude turn, got {a['tokens']}"
    )


def test_metrics_reflects_session_activity(
    http: httpx.Client, session_id: str
) -> None:
    """/v1/metrics rolls up session counts + tokens after a turn."""

    before = http.get("/v1/metrics").json()
    turn(http, session_id, "Reply with PING.", timeout=300)
    after = http.get("/v1/metrics").json()

    assert after["sessions"]["total"] >= before["sessions"]["total"]
    assert after["messages"]["total"] >= before["messages"]["total"] + 2
    assert (
        after["tokens"]["output_total"]
        > before["tokens"]["output_total"]
    ), "metrics output_total should grow after a real LM turn"


# ---- memory stats reflect real ARC ----------------------------------------


def test_memory_stats_real_arc(http: httpx.Client) -> None:
    """When a real ClioAgent is wired, /v1/memory/stats reflects
    its ARCMemory cache — non-zero capacity, real numbers."""

    body = http.get("/v1/memory/stats").json()
    assert body["cache"]["capacity"] > 0


# ---- routing_decision Part lands on real turns ----------------------------


def test_routing_decision_part_present(
    http: httpx.Client, session_id: str
) -> None:
    """The real router emits a selected_expert; the GACT layer
    materialises it as a routing_decision Part."""

    # Use a question that maps cleanly to the chat path (no tool
    # calls, no expert ReAct loop) so the test focuses on the
    # routing_decision Part landing — not on tool-loop latency.
    a = turn(
        http,
        session_id,
        "Acknowledge with one word: PING.",
        timeout=300,
    )
    types = [p["type"] for p in a["parts"]]
    assert "routing_decision" in types, f"got parts {types}"
    rd = next(p for p in a["parts"] if p["type"] == "routing_decision")
    assert rd["selected_agent"], "routing_decision must carry selected_agent"


# ---- SSE stream emits per-message events ----------------------------------


def test_sse_emits_message_lifecycle(
    http: httpx.Client, session_id: str
) -> None:
    """Verify the SSE channel publishes message.created +
    message.completed during a real turn (covers tool_telemetry's
    transport too)."""

    user_id = post_user(http, session_id, "Reply with PING.")
    deadline = time.monotonic() + 300
    seen_completed = False
    seen_created = False
    with httpx.stream(
        "GET",
        f"{http.base_url}/v1/sessions/{session_id}/events",
        timeout=300.0,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            import json as _json
            env = _json.loads(line[len("data: "):])
            if env["type"] == "message.created":
                seen_created = True
            if env["type"] == "message.completed":
                seen_completed = True
                break
            if time.monotonic() > deadline:
                break
    assert seen_created and seen_completed, (
        f"SSE stream incomplete: created={seen_created} completed={seen_completed}"
    )
    # Still have the assistant in the log too (catches cases where
    # SSE fires but persistence is broken).
    wait_for_assistant(http, session_id, user_id)


# ---- Wire-shape-only capabilities (issues #2/#4/#5/#6/#7/#9) ---------------
#
# These assert the *endpoint* accepts the request shape correctly
# but mark the agent-driver path as xfail until the corresponding
# CLIO issue closes. Keeps the suite green while the gap is
# tracked honestly.


@pytest.mark.xfail(
    reason="iowarp/clio-agent#2 — real ClioAgent doesn't emit live tool.call.* events",
    strict=False,
)
def test_real_tool_call_events_fire_during_turn(
    http: httpx.Client, session_id: str
) -> None:
    """Drive a tool-using turn (data expert), assert tool.call.started
    + tool.call.completed appear on the SSE stream BEFORE
    message.completed."""

    user_id = post_user(
        http, session_id,
        "Analyze /tmp/clio-demo/clio_demo.h5 and summarise the structure in one sentence.",
    )
    seen_started = False
    seen_completed = False
    with httpx.stream(
        "GET",
        f"{http.base_url}/v1/sessions/{session_id}/events",
        timeout=300.0,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            import json as _json
            env = _json.loads(line[len("data: "):])
            if env["type"] == "tool.call.started":
                seen_started = True
            if env["type"] == "tool.call.completed":
                seen_completed = True
            if env["type"] == "message.completed":
                break
    assert seen_started and seen_completed, (
        "real ClioAgent didn't emit tool.call.* events during a "
        "tool-using turn (issue #2)"
    )
    wait_for_assistant(http, session_id, user_id, timeout=300)


@pytest.mark.xfail(
    reason="iowarp/clio-agent#5 — context_files store works but agent ignores attachments",
    strict=False,
)
def test_attached_context_file_influences_answer(
    http: httpx.Client, session_id: str
) -> None:
    """Attach a file as mode=read; ask about its content; assert
    the answer references the content without an explicit read tool."""

    http.post(
        f"/v1/sessions/{session_id}/context/files",
        json={"path": "/tmp/clio-demo/clio_demo.parquet", "mode": "read"},
    )
    a = turn(
        http, session_id,
        "What's the schema of the attached file? one sentence.",
        timeout=180,
    )
    text = " ".join(p.get("text", "") for p in a["parts"]).lower()
    # Schema answer should mention column names from clio_demo.parquet.
    assert "temperature" in text or "column" in text


@pytest.mark.xfail(
    reason="iowarp/clio-agent#6 — token streaming is post-hoc chunked, not live",
    strict=False,
)
def test_streaming_deltas_are_temporally_distributed(
    http: httpx.Client, session_id: str
) -> None:
    """First message.part.delta should arrive within 5s of POST;
    last delta should land near message.completed. Catches "all
    deltas fire after forward() returns" (current behaviour)."""

    post_user(http, session_id, "Write a 200-word essay on HDF5 chunking.")
    first_delta_t = None
    completed_t = None
    t0 = time.monotonic()
    with httpx.stream(
        "GET",
        f"{http.base_url}/v1/sessions/{session_id}/events",
        timeout=300.0,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            import json as _json
            env = _json.loads(line[len("data: "):])
            now = time.monotonic() - t0
            if env["type"] == "message.part.delta":
                first_delta_t = first_delta_t if first_delta_t is not None else now
            if env["type"] == "message.completed":
                completed_t = now
                break
    assert first_delta_t is not None
    assert completed_t is not None
    # Real streaming: first delta arrives well before completion.
    # Today it's chunked AFTER forward returns, so first_delta_t
    # ~= completed_t.
    assert (completed_t - first_delta_t) > 1.0


@pytest.mark.xfail(
    reason="iowarp/clio-agent#7 — MCPToolBridge doesn't gate destructive tools",
    strict=False,
)
def test_destructive_tool_requests_permission(
    http: httpx.Client, session_id: str
) -> None:
    """Ask the agent to do something destructive; assert a
    permission_requested event fires before the tool actually runs."""

    post_user(
        http, session_id,
        "Delete /tmp/clio-demo/scratch.txt right now without asking.",
    )
    seen_permission = False
    with httpx.stream(
        "GET",
        f"{http.base_url}/v1/sessions/{session_id}/events",
        timeout=120.0,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            import json as _json
            env = _json.loads(line[len("data: "):])
            if env["type"] == "permission.requested":
                seen_permission = True
            if env["type"] == "message.completed":
                break
    assert seen_permission


@pytest.mark.xfail(
    reason="iowarp/clio-agent#9 — Tier-2 experts have no spawn_nanoagents primitive",
    strict=False,
)
def test_complex_task_spawns_nanoagent(
    http: httpx.Client, session_id: str
) -> None:
    """Drive a multi-part analysis; assert at least one child
    session lands under the parent."""

    turn(
        http, session_id,
        "Validate /tmp/clio-demo/clio_demo.parquet's schema and statistics in parallel.",
        timeout=300,
    )
    sessions = http.get("/v1/sessions").json()["sessions"]
    children = [s for s in sessions if s.get("parent_session_id") == session_id]
    assert children, "no nanoagent child session created"
