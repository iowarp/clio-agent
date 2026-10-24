"""Deep Researcher (marketplace ``clio/deep-researcher`` agent) — real case
acceptance test.

This is **acceptance leg (iii)** of the MCP-client-unification campaign
(``docs/design/mcp-client-unification-2026-08.md``, issue
https://github.com/iowarp/clio-agent/issues/1286, umbrella #1274): does the
marketplace ``deep-researcher`` agent — authored upstream in
``external/clio-agent-marketplace/agents/deep_researcher.py`` and its
``deep-research`` plugin, not by this test — run end-to-end on clio-agent
through the unified MCP v2 client? Since deep-researcher 0.3.0 the coordinator
calls ``run_flow("deep-research")`` (the plugin's
``ai.iowarp.clio/flows/deep_research.py``): the flow plans directions, runs the
plugin's ``researcher`` subagents in parallel (real ``web_search``/``web_fetch``
calls against the clio-kit ``web`` MCP, a ``task=required`` v2 server), has an
independent ``critic`` subagent verify every claim, closes gaps in bounded
rounds, writes the report, and returns ``workflow_state.deep_research``
(``report_path``, ``verified``, ``rejected``, ``gaps_unresolved``, ``rounds``,
``researchers``, ``critics``). The coordinator then delivers the report with
``create_artifact`` (its ``deep-research.report`` fragment).

Encodes the contract from ``benchmark/case14-deep-researcher-web/GOAL.md``.
Per #1286: "RED today by construction — it IS the honest #1274 repro." This
file is SCAFFOLDING for the campaign's own C1-S6 gate: it must collect and
skip cleanly with the live gate off, but the actual live run happens only at
that gate, once C1-S2..S5 land on ``feat/mcp-client-unification``.

Harness wrinkle (see ``clio_sut.py`` module docstring — it stays the shared,
case-agnostic driver for earthscope/this case): the coordinator never calls
``web_search``/``web_fetch`` itself. The researcher and critic sessions are
children of the FLOW-run session, which is itself the coordinator's child, so
they sit two or more levels below the coordinator. ``clio_sut`` folds every
descendant's tool calls into ``run.tool_calls`` and lists the whole tree's ids
in ``run.extra["descendant_session_ids"]``; this file reads each descendant's
``Session`` row (``GET /v1/sessions/{sid}``) and attributes its tool calls to
the subagent id on its own ``agent.id`` field (``prepare_child_session`` stamps
``agent={"id": <subagent id>}`` on the minted child session), keeping this
case-specific evidence seam in the TEST file rather than touching the shared
SUT.

Run live (once the campaign's C1-S2..S5 land and the C1-S6 gate opens)::

    CLIO_RUN_LIVE=1 uv run pytest \\
        tests/test_real_cases/test_deep_researcher_case.py \\
        --provider claude_code --model sonnet -o addopts="" -p no:cacheprovider -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

# The real-cases tier depends on Jaime's `~/agent-test` pytest plugin, an
# unpublished local checkout not installed in every environment (see
# conftest.py's module docstring). conftest.py handles its own absence via
# ``collect_ignore_glob`` during directory-walk collection, but that mechanism
# does not protect an EXPLICIT single-file pytest invocation (it bypasses
# conftest-level ignore globs) — so this module guards its own import the same
# way, converting a missing dependency into a clean skip instead of a
# collection error under either invocation style.
agent_test = pytest.importorskip(
    "agent_test", reason="agent-test harness (local ~/agent-test checkout) not installed"
)
matcher = agent_test.matcher
ToolCall = agent_test.ToolCall

CASE_DIR = "benchmark/case14-deep-researcher-web"
# The default ``clio`` marketplace (the pinned submodule) provides this agent, so
# the run needs no marketplace install.
AGENT_REF = "clio/deep-researcher"

# Guardrail cell (GOAL.md "Case-specific deviations"): subscription provider,
# not the NDP-case default (argonne_metis) — per issue #1286's own leg (iii)
# text and the live-tests-use-claude/codex convention. Documented here for the
# run recipe; actual cell selection happens via `pytest --provider/--model` or
# `CLIO_AGENTTEST_CELLS`, exactly like every sibling real-case test.
GUARDRAIL_PROVIDER = "claude_code"
GUARDRAIL_MODEL = "sonnet"

# Deep research with dynamic fan-out + an independent critic pass can run long,
# so the ceiling is generous. 0 would disable the hard
# cap entirely (progress-watchdog governs, per clio_sut.ClioAgent._post_turn);
# a real ceiling is kept here because a runaway coordinator spawning unbounded
# rounds should eventually be caught by the harness, not just by max_iters.
TIMEOUT_S = float(os.environ.get("CLIO_DEEP_RESEARCHER_TIMEOUT_S", "1800"))

_TOOL_FAILURE_STATUSES = {"failed", "error"}


def _load_prompt() -> str:
    return Path(CASE_DIR, "prompt.txt").read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# Child-session evidence gathering (kept in the TEST file — clio_sut.py stays
# the shared, case-agnostic driver).
# --------------------------------------------------------------------------- #
def _session_tool_calls(http: httpx.Client, session_id: str) -> list[Any]:
    """One session's own ``tools_called`` — the exact extraction
    ``clio_sut.ClioAgent._to_run`` uses (``clio_sut.py`` around the
    ``tools_called`` loop), duplicated here against a CHILD session's own
    messages (confirmed identical wire shape to the parent's:
    ``src/clio_agent/gact/routes/messages.py``'s ``list_messages`` applies no
    parent/child-specific projection)."""
    messages = http.get(f"/v1/sessions/{session_id}/messages").json().get("messages") or []
    calls: list[Any] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        meta = message.get("metadata") or {}
        for tool in meta.get("tools_called") or []:
            if not isinstance(tool, dict):
                continue
            result = tool.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    pass
            calls.append(
                ToolCall(
                    name=str(tool.get("name") or ""),
                    args=tool.get("args") if isinstance(tool.get("args"), dict) else {},
                    output=result,
                )
            )
    return calls


def _coordinator_calls(run: Any, gact_server: Any) -> list[Any]:
    """The coordinator session's OWN tool calls (``run.tool_calls`` also holds
    every descendant's, folded in by ``clio_sut``)."""
    session_id = str(run.extra.get("session_id") or "")
    if not session_id:
        return []
    with httpx.Client(base_url=gact_server.url, timeout=60.0) as http:
        return _session_tool_calls(http, session_id)


def _find_deep_research(value: Any) -> dict[str, Any] | None:
    """The first ``deep_research`` mapping nested anywhere in ``value``."""
    if isinstance(value, dict):
        found = value.get("deep_research")
        if isinstance(found, dict):
            return found
        for item in value.values():
            nested = _find_deep_research(item)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _find_deep_research(item)
            if nested is not None:
                return nested
    return None


def _deep_research_result(run: Any) -> dict[str, Any] | None:
    """The flow's ``workflow_state.deep_research`` as the coordinator received
    it: from the coordinator's merged workflow state, else from its own tool
    results (the ``wait_tasks`` result that carries the flow run's output)."""
    found = _find_deep_research(run.extra.get("workflow_state") or {})
    if found is not None:
        return found
    for call in run.extra.get("coordinator_tool_calls") or []:
        found = _find_deep_research(call.output)
        if found is not None:
            return found
    return None


def _tool_call_ok(call: Any) -> bool:
    """Whether a recorded tool call actually SUCCEEDED — a task-backed
    ``web_fetch``/``web_search`` that errored proves nothing was fetched or
    searched."""
    if getattr(call, "error", None):
        return False
    out = call.output
    if isinstance(out, dict):
        if out.get("ok") is False:
            return False
        if out.get("is_error") is True:
            return False
        if str(out.get("status") or "").lower() in _TOOL_FAILURE_STATUSES:
            return False
    return True


def _subtree_sessions(http: httpx.Client, run: Any) -> list[dict[str, Any]]:
    """Every session below the coordinator, as ``Session`` rows.

    The ids come from the harness's own workspace-scoped tree walk
    (``run.extra["descendant_session_ids"]``) — never re-list here: a bare
    ``GET /v1/sessions`` defaults to ws_default and hides the per-run-workspace
    children (this module's original bare re-fetch saw ZERO children on a live
    run that spawned five researchers + a critic, 2026-09-03). Each row is read
    by id, which needs no workspace filter. Falls back to the direct children
    when the harness reported no tree."""
    ids = [str(i) for i in run.extra.get("descendant_session_ids") or [] if i]
    if not ids:
        return [dict(c) for c in run.extra.get("child_sessions") or []]
    rows: list[dict[str, Any]] = []
    for sid in ids:
        response = http.get(f"/v1/sessions/{sid}")
        rows.append(response.json() if response.status_code == 200 else {"id": sid})
    return rows


def _augment_with_deep_researcher_evidence(
    run: Any, gact_server: Any, run_spec: dict[str, Any]
) -> None:
    """Populate ``run.extra`` with this case's evidence seam: every session in
    the coordinator's subtree (the flow-run session and the researcher and
    critic sessions under it), and each one's own tool calls grouped by which
    subagent id it ran (read off the ``Session``'s own ``agent.id`` — see module
    docstring). ``Run`` is frozen but ``extra`` is a plain mutable dict (the
    documented escape hatch — ``agent_test.run.Run``), so mutating it in place
    here is legal."""
    run.extra["run_spec"] = dict(run_spec)
    session_id = str(run.extra.get("session_id") or "")
    subtree: list[dict[str, Any]] = []
    by_expert: dict[str, list[Any]] = {}
    if session_id:
        with httpx.Client(base_url=gact_server.url, timeout=60.0) as http:
            subtree = _subtree_sessions(http, run)
            for row in subtree:
                expert_id = str((row.get("agent") or {}).get("id") or "")
                calls = _session_tool_calls(http, str(row.get("id") or ""))
                by_expert.setdefault(expert_id, []).extend(calls)
    run.extra["subtree_sessions"] = subtree
    run.extra["child_tool_calls_by_expert"] = by_expert
    # ``clio_sut`` already folded every descendant's calls into run.tool_calls.
    run.extra["all_tool_calls"] = list(run.tool_calls)


# --------------------------------------------------------------------------- #
# Matchers — structured evidence only (tool call name/args/output/error, the
# artifact registry, the child session's own agent.id), never synthesis prose.
# --------------------------------------------------------------------------- #
@matcher
def ran_deep_research_flow(run: Any) -> bool:
    """The coordinator started the plugin's flow itself: a ``run_flow`` call
    naming ``deep-research`` in the coordinator's OWN session (not a
    descendant's)."""
    session_id = str(run.extra.get("session_id") or "")
    own = run.extra.get("coordinator_tool_calls")
    calls = own if own is not None else run.tool_calls
    return bool(session_id) and any(
        call.name == "run_flow" and (call.args or {}).get("flow") == "deep-research"
        for call in calls
    )


@matcher
def web_fetch_succeeded(run: Any) -> bool:
    """At least one SUCCESSFUL ``web_fetch`` call landed anywhere across the
    coordinator's own trace and its whole session subtree — task-backed
    evidence that a source was actually retrieved through the ``task=required``
    v2 web MCP, not merely attempted."""
    calls = run.extra.get("all_tool_calls") or []
    return any(call.name == "web_fetch" and _tool_call_ok(call) for call in calls)


@matcher
def web_search_succeeded(run: Any) -> bool:
    """At least one SUCCESSFUL ``web_search`` call — candidate-discovery
    actually happened, not just an attempted/errored query."""
    calls = run.extra.get("all_tool_calls") or []
    return any(call.name == "web_search" and _tool_call_ok(call) for call in calls)


@matcher
def markdown_report_artifact(run: Any) -> bool:
    """The coordinator delivered the flow's report (the ``deep-research``
    plugin's ``deep-research.report`` fragment: ``create_artifact`` with
    ``path`` set to ``workflow_state.deep_research.report_path``) as a real
    ``.md`` file. ``run.extra["artifacts"]`` is registry-sourced (S7 #973 — not
    a tool-output path scrape), and only lists paths that exist on disk (see
    ``clio_sut.ClioAgent._existing_paths``)."""
    return any(
        p.lower().endswith(".md") and Path(p).is_file() and Path(p).stat().st_size > 256
        for p in run.extra.get("artifacts") or []
    )


@matcher
def critic_independent_evidence(run: Any) -> bool:
    """The critic's OWN validity condition (the ``deep-research`` plugin's
    ``prompt/subagents/critic.md``: it verifies every claim on a page it fetched
    itself), checked with GENUINE per-subagent attribution rather than a ">=2
    distinct calls" proxy: each subtree ``Session``'s ``agent.id`` field (set to
    the subagent id by ``prepare_child_session``,
    ``src/clio_agent/gact/spawn_context.py``) lets this test isolate exactly the
    ``critic`` session(s)' own tool calls — wherever they sit under the flow-run
    session — from the ``researcher`` sessions' calls, so this is a real
    attribution match, not a proxy count."""
    by_expert = run.extra.get("child_tool_calls_by_expert") or {}
    critic_calls = by_expert.get("critic") or []
    return any(
        call.name in ("web_search", "web_fetch") and _tool_call_ok(call) for call in critic_calls
    )


# --------------------------------------------------------------------------- #
# The test
# --------------------------------------------------------------------------- #
@pytest.mark.real_case
@pytest.mark.live
def test_deep_researcher_web_synthesis(agent, gact_server, tmp_path):
    prompt = _load_prompt()

    run_spec = {
        "task": prompt,
        "agent_ref": AGENT_REF,
        "case_dir": CASE_DIR,
        "run_label": "acceptance",
        # Isolated, auto-cleaned workspace root: the report artifact is
        # written here, never into the repo (see clio_sut.ClioAgent.invoke).
        "workdir": str(tmp_path),
        # Durable per-cell trace dir (inspectable later, not wiped /tmp) — same
        # convention as the EarthScope case.
        "trace_path": str(gact_server.trace_dir / "acceptance.run.jsonl"),
        "timeout_s": TIMEOUT_S,
    }
    run = agent.run(run_spec)
    # The coordinator's own calls, before anything else reads the folded list.
    run.extra["coordinator_tool_calls"] = _coordinator_calls(run, gact_server)
    _augment_with_deep_researcher_evidence(run, gact_server, run_spec)

    # Runtime/harness invariants.
    assert run.error is None, run.error
    assert run.extra["agent_activated"], run.extra.get("active_agent_ref")

    # The coordinator started the flow, and the flow actually ran subagents: a
    # subtree with no researcher or critic session never dispatched any web
    # research at all (they are children of the flow-run session, not of the
    # coordinator, so the whole subtree is checked).
    assert ran_deep_research_flow(run), [call.name for call in run.extra["coordinator_tool_calls"]]
    by_expert = run.extra.get("child_tool_calls_by_expert") or {}
    subtree_agents = {
        str((row.get("agent") or {}).get("id") or "") for row in run.extra["subtree_sessions"]
    }
    assert {"researcher", "critic"} <= subtree_agents, (
        f"no researcher/critic sessions under the coordinator: {sorted(subtree_agents)}"
    )

    # The flow's typed result reached the coordinator.
    deep_research = _deep_research_result(run)
    assert deep_research is not None, run.extra.get("workflow_state")
    for key in ("verified", "rejected", "gaps_unresolved"):
        assert isinstance(deep_research.get(key), list), (key, deep_research)

    # (leg iii, matcher a) task-backed web_fetch actually succeeded.
    assert web_fetch_succeeded(run), [
        (call.name, call.error) for call in run.extra.get("all_tool_calls") or []
    ]

    # (leg iii, matcher b) web_search actually succeeded.
    assert web_search_succeeded(run), [call.name for call in run.extra.get("all_tool_calls") or []]

    # (leg iii, matcher c) a real cited Markdown report landed in the registry.
    assert markdown_report_artifact(run), run.extra.get("artifacts")

    # (leg iii, matcher d) the critic's own validity condition: an INDEPENDENT
    # web_search/web_fetch call, attributed to the critic child session itself
    # (the plugin's own stated requirement, not a harness-invented one).
    assert critic_independent_evidence(run), by_expert

    # Hygiene: the report lands inside the isolated workdir, never the repo —
    # same observable guarantee every other case's artifact-path check makes.
    for p in run.extra.get("artifacts") or []:
        if p.lower().endswith(".md"):
            assert Path(p).resolve().is_relative_to(tmp_path.resolve()), (
                f"report {p!r} written outside the isolated workdir {tmp_path}"
            )
