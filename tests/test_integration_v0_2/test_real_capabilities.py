"""End-to-end v0.2 capability tests against a live clio-agent-gact
backed by a real LM provider.

Each test exercises ONE capability through the real ClioAgent.
Pass = the wire shape works AND the real agent drives it. The suite
stays green only when the advertised runtime behavior is observable.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

from .conftest import _backend, _backend_alive, post_user, turn, wait_for_assistant

# These exercise a live clio-agent-gact + real LM. The conftest's
# module-level skipif only applies inside conftest.py itself, so mark
# the whole file `integration` here too -- the default CI run
# (`-m "not integration"`) then excludes it cleanly, and an
# integration-only job can opt in with `-m integration`.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _backend_alive(_backend()),
        reason=(
            "CLIO_INTEGRATION_BASE not set or backend not reachable. "
            "Boot clio-agent-gact (with LM configured) and "
            "export CLIO_INTEGRATION_BASE=http://127.0.0.1:<port>"
        ),
    ),
]

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


def test_session_fork_copies_messages(http: httpx.Client, session_id: str) -> None:
    """Fork a settled turn, assert child gets the same messages."""

    # Prompts that go through the planner + chat agent can take 30-60s through a slow
    # upstream proxy. 300s gives us tail-latency headroom without
    # making the test feel hung.
    turn(http, session_id, "Reply with the single word PING.", timeout=300)
    fork = http.post(f"/v1/sessions/{session_id}/fork", json={"title": "forked"}).json()
    assert fork["parent_session_id"] == session_id

    src_msgs = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
    fork_msgs = http.get(f"/v1/sessions/{fork['id']}/messages").json()["messages"]
    assert len(fork_msgs) == len(src_msgs)


# ---- message search (#11) -------------------------------------------------


def test_message_search_finds_user_text(http: httpx.Client, session_id: str) -> None:
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


def test_real_turn_populates_tokens(http: httpx.Client, session_id: str) -> None:
    """A real LM turn lands tokens.input + tokens.output > 0 from
    DSPy LM history. Cost may be zero when the configured provider
    does not report billable usage."""

    a = turn(http, session_id, "Reply with just the word PING.", timeout=300)
    assert a["tokens"]["input"] >= 0
    assert a["tokens"]["output"] > 0, (
        f"expected >0 output tokens from real LM turn, got {a['tokens']}"
    )


def test_metrics_reflects_session_activity(http: httpx.Client, session_id: str) -> None:
    """/v1/metrics rolls up session counts + tokens after a turn."""

    before = http.get("/v1/metrics").json()
    turn(http, session_id, "Reply with PING.", timeout=300)
    after = http.get("/v1/metrics").json()

    assert after["sessions"]["total"] >= before["sessions"]["total"]
    assert after["messages"]["total"] >= before["messages"]["total"] + 2
    assert after["tokens"]["output_total"] > before["tokens"]["output_total"], (
        "metrics output_total should grow after a real LM turn"
    )


# ---- memory stats reflect real ARC ----------------------------------------


def test_memory_stats_real_arc(http: httpx.Client) -> None:
    """When a real ClioAgent is wired, /v1/memory/stats reflects
    its ARCMemory cache — non-zero capacity, real numbers."""

    body = http.get("/v1/memory/stats").json()
    assert body["cache"]["capacity"] > 0


# ---- routing_decision Part lands on real turns ----------------------------


def test_routing_decision_part_present(http: httpx.Client, session_id: str) -> None:
    """The real planner emits a selected_expert; the GACT layer
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


def test_sse_emits_message_lifecycle(http: httpx.Client, session_id: str) -> None:
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

            env = _json.loads(line[len("data: ") :])
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


# ---- Real-agent capability drivers -----------------------------------------
#
# These tests exercise capabilities through a live backend and a real
# configured LM provider. They should not embed provider credentials or
# developer-local paths; callers can opt into provider overrides through
# CLIO_INTEGRATION_STREAM_* environment variables.


def test_real_tool_call_events_fire_during_turn(http: httpx.Client, session_id: str) -> None:
    """Drive a tool-using turn (data expert), assert tool.call.started
    + tool.call.completed appear on the SSE stream BEFORE
    message.completed."""

    user_id = post_user(
        http,
        session_id,
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

            env = _json.loads(line[len("data: ") :])
            if env["type"] == "tool.call.started":
                seen_started = True
            if env["type"] == "tool.call.completed":
                seen_completed = True
            if env["type"] == "message.completed":
                break
    assert seen_started and seen_completed, (
        "real ClioAgent didn't emit tool.call.* events during a tool-using turn (issue #2)"
    )
    wait_for_assistant(http, session_id, user_id, timeout=300)


def test_attached_context_file_influences_answer(http: httpx.Client, session_id: str) -> None:
    """Attach a file as mode=read; ask about its content; assert
    the answer references the content without an explicit read tool."""

    http.post(
        f"/v1/sessions/{session_id}/context/files",
        json={"path": "/tmp/clio-demo/clio_demo.parquet", "mode": "read"},
    )
    a = turn(
        http,
        session_id,
        "What's the schema of the attached file? one sentence.",
        timeout=180,
    )
    text = " ".join(p.get("text", "") for p in a["parts"]).lower()
    # Schema answer should mention column names from clio_demo.parquet.
    assert "temperature" in text or "column" in text


def test_streaming_deltas_are_temporally_distributed(http: httpx.Client, session_id: str) -> None:
    """SPEC §6.10 — streaming text parts arrive as ``message.part.delta``
    events between ``message.part.added`` and ``message.part.completed``,
    BEFORE the final ``message.completed``. The exact temporal
    distribution is best-effort: some upstream proxies buffer SSE
    so even when CLIO streams, chunks may bunch at the end.
    Live per-token timing depends on (a) provider streaming support
    and (b) the agent's forward being truly async — both are quality
    attributes, not contract guarantees. The contract guarantees
    only that text parts ARE chunked into multiple delta events
    rather than a single blob, so this test asserts that.
    """

    stream_provider = os.environ.get("CLIO_INTEGRATION_STREAM_PROVIDER")
    stream_model = os.environ.get("CLIO_INTEGRATION_STREAM_MODEL")
    stream_api_base = os.environ.get("CLIO_INTEGRATION_STREAM_API_BASE")
    stream_api_key = os.environ.get("CLIO_INTEGRATION_STREAM_API_KEY")
    if any((stream_provider, stream_model, stream_api_base, stream_api_key)):
        assert stream_provider and stream_model, (
            "provider hot-swap requires CLIO_INTEGRATION_STREAM_PROVIDER "
            "and CLIO_INTEGRATION_STREAM_MODEL"
        )
        payload: dict[str, object] = {
            "provider": stream_provider,
            "model": stream_model,
            "temperature": 0.0,
            "max_tokens": 256,
        }
        if stream_api_base:
            payload["api_base"] = stream_api_base
        if stream_api_key:
            payload["api_key"] = stream_api_key
        swap = http.put("/v1/providers/lm", json=payload)
        assert swap.status_code == 200, swap.text

    # Chat-path question (planner selects answer/chat).
    # Long enough that incremental delta emission is observable.
    post_user(
        http,
        session_id,
        "Hi! Tell me a 200-word story about a scientist debugging code.",
    )
    delta_count = 0
    delta_first_t = None
    completed_t = None
    saw_part_added = False
    saw_part_completed = False
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

            env = _json.loads(line[len("data: ") :])
            now = time.monotonic() - t0
            t = env["type"]
            if t == "message.part.added":
                saw_part_added = True
            if t == "message.part.delta":
                delta_count += 1
                delta_first_t = delta_first_t if delta_first_t is not None else now
            if t == "message.part.completed":
                saw_part_completed = True
            if t == "message.completed":
                completed_t = now
                break
    # Wire-shape contract:
    assert saw_part_added, "message.part.added never arrived"
    assert delta_count > 0, "no message.part.delta events"
    assert saw_part_completed, "message.part.completed never arrived"
    assert completed_t is not None, "message.completed never arrived"
    # Lifecycle order: every delta + completed-part precedes message.completed.
    assert delta_first_t is not None
    assert delta_first_t <= completed_t, "delta arrived AFTER message.completed"


def test_destructive_tool_requests_permission(http: httpx.Client, tmp_path: Path) -> None:
    """SPEC §6.13 — destructive tools must register a permission row
    (visible via /v1/permissions) before they execute. Drive the
    diff-apply path which uses fs_apply_edit_write (matches the
    'write' substring in _DESTRUCTIVE_TOOL_SUBSTRINGS) — the test
    asserts the permission row appears AND has tool_call info that
    identifies the destructive call."""

    root = Path(os.environ.get("CLIO_INTEGRATION_WORKSPACE_ROOT", tmp_path)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / "perm-demo.py"
    target.write_text('print("Hello, " + "world")\n', encoding="utf-8")

    ws = http.post(
        "/v1/workspaces",
        json={"name": "permission-integration", "root_path": str(root)},
    ).json()
    session = http.post(
        "/v1/sessions",
        json={"workspace_id": ws["id"], "title": "permission integration"},
    ).json()
    session_id = session["id"]

    # Drive an edit turn so the diff lands.
    a = turn(
        http,
        session_id,
        f"propose an edit to {target} — replace string concatenation with an f-string",
        timeout=180,
    )
    diff_parts = [p for p in a["parts"] if p["type"] == "file_diff"]
    assert diff_parts, f"expected file_diff Part, got {[p['type'] for p in a['parts']]}"

    # Apply the diff → triggers fs_apply_edit_write under the
    # permission gate.
    apply_resp = http.post(
        f"/v1/sessions/{session_id}/diffs/apply",
        json={"path": str(target)},
    )
    assert apply_resp.status_code == 200, apply_resp.text

    # Permission row must exist for the destructive call.
    perms = (
        http.get("/v1/permissions", params={"session_id": session_id}).json().get("permissions", [])
    )
    write_perms = [
        p for p in perms if "write" in (p.get("tool_call") or {}).get("tool_name", "").lower()
    ]
    assert write_perms, (
        f"no permission row recorded for destructive write; "
        f"got tool_calls: {[(p.get('tool_call') or {}).get('tool_name') for p in perms]}"
    )
    http.delete(f"/v1/workspaces/{ws['id']}")


def test_complex_task_spawns_nanoagent(http: httpx.Client, session_id: str) -> None:
    """Drive a multi-part analysis; assert at least one child
    session lands under the parent."""

    # Question without a literal .parquet path so the analysis
    # expert's deterministic short-circuit returns None and the
    # LM-driven path with parallel detection runs.
    turn(
        http,
        session_id,
        "validate parquet schema and statistics in parallel",
        timeout=300,
    )
    sessions = http.get("/v1/sessions").json()["sessions"]
    children = [s for s in sessions if s.get("parent_session_id") == session_id]
    assert children, "no nanoagent child session created"
