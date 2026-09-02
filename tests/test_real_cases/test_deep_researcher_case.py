"""Deep Researcher (marketplace `deep-researcher` pack) — real case acceptance
test.

This is **acceptance leg (iii)** of the MCP-client-unification campaign
(``docs/design/mcp-client-unification-2026-08.md``, issue
https://github.com/iowarp/clio-agent/issues/1286, umbrella #1274): does the
marketplace ``deep-researcher`` pack — authored upstream in
``external/clio-agent-marketplace/deep-researcher``, not by this test — run
end-to-end on clio-agent through the unified MCP v2 client? The pack's
``main`` coordinator dynamically fans out ``researcher`` leaves (real
``web_search``/``web_fetch`` calls against the clio-kit ``web`` MCP, a
``task=required`` v2 server), sends the assembled evidence through an
independent ``critic`` leaf, and produces a cited Markdown report artifact.

Encodes the contract from ``benchmark/case14-deep-researcher-web/GOAL.md``.
Per #1286: "RED today by construction — it IS the honest #1274 repro." This
file is SCAFFOLDING for the campaign's own C1-S6 gate: it must collect and
skip cleanly with the live gate off, but the actual live run happens only at
that gate, once C1-S2..S5 land on ``feat/mcp-client-unification``.

Harness wrinkle (see ``clio_sut.py`` module docstring — it stays the shared,
case-agnostic driver for earthscope/wildfire/case13/this case): the
``main`` coordinator here never calls ``web_search``/``web_fetch`` itself —
only its ``researcher``/``critic`` children do, each on its OWN session
(agent-driven ``spawn_agent_task``/``spawn_agents_parallel`` per C1-S1's
capability-keyed spawn runtime, not a declared workflow). So
``clio_sut.ClioAgent._to_run`` — which only parses the top session's own
``tools_called`` — never sees those calls; this file re-fetches every direct
child session's own messages and attributes each child's tool calls to its
spawning expert via the child ``Session``'s own ``agent.id`` field
(``src/clio_agent/gact/turn_spawn.py``: ``spawn_child_turn`` stamps
``agent={"id": spec.child_expert_id, ...}`` on the minted child session — the
same field ``tests/test_stress_benchmark/test_local_scientific_workflows.py``
already reads children through, and case13's precedent for keeping
case-specific evidence seams in the TEST file rather than touching the shared
SUT).

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
BLUEPRINT_ID = "deep-researcher"

# Absolute, derived-at-runtime local path to the marketplace pack (submodule) —
# never hardcode a drive path; this box's repo root may not be D:\. A local
# filesystem `marketplace_source` installs with no network fetch (see
# `clio_agent.gact.agent_blueprint_sources.refresh_agent_blueprint_source`:
# a `source` that resolves to an existing path is installed as `source_kind:
# "path"`, never cloned).
REPO_ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE_SOURCE = str(REPO_ROOT / "external" / "clio-agent-marketplace" / "deep-researcher")

# Guardrail cell (GOAL.md "Case-specific deviations"): subscription provider,
# not the NDP-case default (argonne_metis) — per issue #1286's own leg (iii)
# text and the live-tests-use-claude/codex convention. Documented here for the
# run recipe; actual cell selection happens via `pytest --provider/--model` or
# `CLIO_AGENTTEST_CELLS`, exactly like every sibling real-case test.
GUARDRAIL_PROVIDER = "claude_code"
GUARDRAIL_MODEL = "sonnet"

# Deep research with dynamic fan-out + an independent critic pass can run long;
# generous like case13's cluster-operator timeout. 0 would disable the hard
# cap entirely (progress-watchdog governs, per clio_sut.ClioAgent._post_turn);
# a real ceiling is kept here because a runaway coordinator spawning unbounded
# rounds should eventually be caught by the harness, not just by max_iters.
TIMEOUT_S = float(os.environ.get("CLIO_DEEP_RESEARCHER_TIMEOUT_S", "1800"))

_TOOL_FAILURE_STATUSES = {"failed", "error"}


def _load_prompt() -> str:
    return Path(CASE_DIR, "prompt.txt").read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# Child-session evidence gathering (kept in the TEST file, per case13's
# precedent — clio_sut.py stays the shared, case-agnostic driver).
# --------------------------------------------------------------------------- #
def _child_sessions(http: httpx.Client, parent_session_id: str) -> list[dict[str, Any]]:
    """Direct child sessions the coordinator spawned via ``spawn_agent_task``/
    ``spawn_agents_parallel`` (C1-S1 capability-keyed routing, not
    declared-workflow handoffs) — same filter
    ``tests/test_stress_benchmark/test_local_scientific_workflows.py``'s
    ``_children`` helper already uses live."""
    sessions = http.get("/v1/sessions").json().get("sessions") or []
    return [row for row in sessions if str(row.get("parent_session_id") or "") == parent_session_id]


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


def _tool_call_ok(call: Any) -> bool:
    """Whether a recorded tool call actually SUCCEEDED — a task-backed
    ``web_fetch``/``web_search`` that errored proves nothing was fetched or
    searched. Mirrors ``test_case13_cluster_operator.py``'s
    ``_tool_call_failed`` (inverted), duplicated rather than imported so this
    file stays self-contained."""
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


def _augment_with_deep_researcher_evidence(
    run: Any, gact_server: Any, run_spec: dict[str, Any]
) -> None:
    """Populate ``run.extra`` with this case's evidence seam: every direct
    child session's own tool calls, grouped by which expert id spawned it
    (``researcher`` vs ``critic``, read off the child ``Session``'s own
    ``agent.id`` — see module docstring). ``Run`` is frozen but ``extra`` is a
    plain mutable dict (the documented escape hatch — ``agent_test.run.Run``),
    so mutating it in place here is legal, same as case13's
    ``_augment_with_case13_evidence``."""
    run.extra["run_spec"] = dict(run_spec)
    session_id = str(run.extra.get("session_id") or "")
    children: list[dict[str, Any]] = []
    by_expert: dict[str, list[Any]] = {}
    if session_id:
        with httpx.Client(base_url=gact_server.url, timeout=60.0) as http:
            children = _child_sessions(http, session_id)
            for child in children:
                expert_id = str((child.get("agent") or {}).get("id") or "")
                calls = _session_tool_calls(http, str(child.get("id") or ""))
                by_expert.setdefault(expert_id, []).extend(calls)
    run.extra["child_sessions"] = children
    run.extra["child_tool_calls_by_expert"] = by_expert
    run.extra["all_tool_calls"] = list(run.tool_calls) + [
        call for calls in by_expert.values() for call in calls
    ]


# --------------------------------------------------------------------------- #
# Matchers — structured evidence only (tool call name/args/output/error, the
# artifact registry, the child session's own agent.id), never synthesis prose.
# --------------------------------------------------------------------------- #
@matcher
def web_fetch_succeeded(run: Any) -> bool:
    """At least one SUCCESSFUL ``web_fetch`` call landed anywhere across the
    coordinator's own trace and every direct child session — task-backed
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
    """The pack's own completion gate (``main``'s AGENT.md: ``create_artifact``
    with the complete report, ``.md`` name, ``kind="report"``) actually
    produced a real file. ``run.extra["artifacts"]`` is registry-sourced (S7
    #973 — not a tool-output path scrape), and only lists paths that exist on
    disk (see ``clio_sut.ClioAgent._existing_paths``)."""
    return any(
        p.lower().endswith(".md") and Path(p).is_file() and Path(p).stat().st_size > 256
        for p in run.extra.get("artifacts") or []
    )


@matcher
def critic_independent_evidence(run: Any) -> bool:
    """The critic's OWN validity condition (its ``experts/critic.md``: "A
    critic pass is invalid unless you independently call ``web_search`` or
    ``web_fetch``"), checked with GENUINE per-expert attribution rather than a
    ">=2 distinct calls" proxy: the child ``Session`` wire model's ``agent.id``
    field (confirmed set to ``TaskSpec.child_expert_id`` by
    ``spawn_child_turn``, ``src/clio_agent/gact/turn_spawn.py``) lets this test
    isolate exactly the ``critic`` child session(s)' own tool calls from the
    ``researcher`` leaves' calls, so this is a real attribution match, not a
    proxy count."""
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
        "blueprint_id": BLUEPRINT_ID,
        "marketplace_source": MARKETPLACE_SOURCE,
        "case_dir": CASE_DIR,
        "run_label": "acceptance",
        # Isolated, auto-cleaned workspace root: the report artifact is
        # written here, never into the repo (see clio_sut.ClioAgent.invoke).
        "workdir": str(tmp_path),
        # Durable per-cell trace dir (inspectable later, not wiped /tmp) — same
        # convention as the EarthScope/wildfire cases.
        "trace_path": str(gact_server.trace_dir / "acceptance.run.jsonl"),
        "timeout_s": TIMEOUT_S,
    }
    run = agent.run(run_spec)
    _augment_with_deep_researcher_evidence(run, gact_server, run_spec)

    # Runtime/harness invariants.
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    # The coordinator actually delegated (agent-driven spawn, no forced count):
    # a run with zero children never dispatched any web research at all.
    assert run.extra.get("child_sessions"), "coordinator spawned no researcher/critic children"

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
    # (the pack's own stated requirement, not a harness-invented one).
    assert critic_independent_evidence(run), run.extra.get("child_tool_calls_by_expert")

    # Hygiene: the report lands inside the isolated workdir, never the repo —
    # same observable guarantee every other case's artifact-path check makes.
    for p in run.extra.get("artifacts") or []:
        if p.lower().endswith(".md"):
            assert Path(p).resolve().is_relative_to(tmp_path.resolve()), (
                f"report {p!r} written outside the isolated workdir {tmp_path}"
            )
