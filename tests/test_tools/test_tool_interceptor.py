"""P2.3 — the tool boundary consumes the ``tool_interceptor`` + ``post_tool`` seams.

Exercises the REAL ``SyncMCPToolExecutor.call_tool`` path against a FastMCP echo
server, driving the already-wired ``ToolRuntimeHooks.tool_interceptor`` /
``post_tool`` slots directly (the gate→stash chain is proven separately in
``test_gact/test_hook_dispatcher.py``):

* ``synthesize`` => the REAL tool is NOT called, the fabricated result is used, it is
  flagged synthetic to PostToolUse, and PostToolUse still fires on it;
* ``modify`` => the real tool runs with the MUTATED input;
* PostToolUse rewrite changes only the model-visible observation;
* a PostToolUse deny feeds its reason back WITHOUT un-running the effect.

One test (``test_end_to_end_real_gate_stash_interceptor_skips_real_tool``) proves
the two links compose: the REAL stashing gate (``_make_permission_gate``) and the
REAL ``pre_tool_interceptor`` consumer driven through this same executor, rather
than each half proven only in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import Client, FastMCP

from clio_agent.gact.app import _make_permission_gate, build_app
from clio_agent.gact.hooks import PRE_TOOL_USE, install_global_dispatcher, pre_tool_interceptor
from clio_agent.tools.execution import (
    SyncMCPToolExecutor,
    ToolRuntimeHooks,
    set_tool_runtime_fallback,
    set_tool_runtime_resolver,
)
from clio_agent.tools.tool_hooks import InterceptDecision
from tests.test_gact._hook_fixtures import make_command_dispatcher

# Records every REAL tool invocation so a test can prove synthesize skipped it.
_REAL_CALLS: list[dict[str, Any]] = []


def _echo_server() -> FastMCP:
    server = FastMCP("interceptor-demo")

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text back, recording the REAL invocation."""
        _REAL_CALLS.append({"text": text})
        return f"REAL:{text}"

    return server


def _run(hooks: ToolRuntimeHooks, name: str, args: dict[str, Any]) -> Any:
    """Drive one call_tool through the fallback-installed hook bundle."""

    saved_resolver = None
    _REAL_CALLS.clear()
    # Force the app-less path so our fallback bundle is authoritative + deterministic
    # regardless of any resolver a sibling test's build_app installed process-wide.
    set_tool_runtime_resolver(None)
    set_tool_runtime_fallback(hooks)
    executor = SyncMCPToolExecutor(_echo_server(), timeout=5.0, client_factory=Client)
    try:
        return executor.call_tool(name, args)
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        set_tool_runtime_resolver(saved_resolver)
        executor.close()


def test_synthesize_skips_real_tool_and_fires_post_tool(tmp_path: Path) -> None:
    """synthesize: the real tool is NOT called, the fabricated result is returned, and
    PostToolUse fires flagged synthetic."""

    post_seen: list[dict[str, Any]] = []

    def post_tool(name, args, observation, is_error, synthetic):  # noqa: ANN001
        post_seen.append(
            {"name": name, "observation": observation, "synthetic": synthetic, "is_error": is_error}
        )
        return observation

    hooks = ToolRuntimeHooks(
        tool_interceptor=lambda n, a: InterceptDecision(kind="synthesize", result="CACHED:hi"),
        post_tool=post_tool,
    )
    result = _run(hooks, "echo", {"text": "hi"})

    assert result == "CACHED:hi"
    assert _REAL_CALLS == [], "synthesize must SKIP the real tool call (no double-run)"
    assert len(post_seen) == 1
    assert post_seen[0]["synthetic"] is True
    assert post_seen[0]["observation"] == "CACHED:hi"


def test_modify_runs_real_tool_with_mutated_input(tmp_path: Path) -> None:
    """modify: the real tool DOES run, but with the mutated input."""

    hooks = ToolRuntimeHooks(
        tool_interceptor=lambda n, a: InterceptDecision(
            kind="modify", modified_args={"text": "MUTATED"}
        ),
    )
    result = _run(hooks, "echo", {"text": "original"})

    assert _REAL_CALLS == [{"text": "MUTATED"}], "modify must run the real tool with new input"
    assert result == "REAL:MUTATED"


def test_no_decision_runs_real_tool_unchanged(tmp_path: Path) -> None:
    """A ``None`` interceptor decision leaves the call untouched."""

    hooks = ToolRuntimeHooks(tool_interceptor=lambda n, a: None)
    result = _run(hooks, "echo", {"text": "plain"})
    assert _REAL_CALLS == [{"text": "plain"}]
    assert result == "REAL:plain"


def test_post_tool_rewrite_changes_only_model_observation(tmp_path: Path) -> None:
    """PostToolUse rewrite replaces the model-visible observation; the real effect
    (the recorded call) is unchanged."""

    hooks = ToolRuntimeHooks(
        post_tool=lambda n, a, obs, err, syn: "REWRITTEN OBSERVATION",
    )
    result = _run(hooks, "echo", {"text": "hi"})

    assert _REAL_CALLS == [{"text": "hi"}], "the real effect still ran"
    assert result == "REWRITTEN OBSERVATION", "the model sees the rewrite"


def test_post_tool_deny_feeds_reason_without_unrunning_effect(tmp_path: Path) -> None:
    """A PostToolUse 'deny' cannot un-run a completed effect — it only appends the
    reason as feedback to the observation the model sees."""

    def post_tool(name, args, observation, is_error, synthetic):  # noqa: ANN001
        return f"{observation}\n\n[PostToolUse blocked] scanner: secret detected"

    hooks = ToolRuntimeHooks(post_tool=post_tool)
    result = _run(hooks, "echo", {"text": "hi"})

    assert _REAL_CALLS == [{"text": "hi"}], "the effect already ran; PostToolUse can't undo it"
    assert "REAL:hi" in result
    assert "[PostToolUse blocked] scanner: secret detected" in result


def test_post_tool_hook_failure_leaves_original_observation(tmp_path: Path) -> None:
    """A raising PostToolUse hook must never break the tool boundary — the original
    observation stands (hook failure != a broken tool call)."""

    def boom(name, args, observation, is_error, synthetic):  # noqa: ANN001
        raise RuntimeError("hook exploded")

    hooks = ToolRuntimeHooks(post_tool=boom)
    result = _run(hooks, "echo", {"text": "hi"})
    assert result == "REAL:hi"


def _run_raw(hooks: ToolRuntimeHooks, name: str, args: dict[str, Any]) -> Any:
    """Drive one ``call_tool_result`` (the MCP Apps raw bridge, ``return_raw=True``)
    through the fallback-installed hook bundle."""

    saved_resolver = None
    _REAL_CALLS.clear()
    set_tool_runtime_resolver(None)
    set_tool_runtime_fallback(hooks)
    executor = SyncMCPToolExecutor(_echo_server(), timeout=5.0, client_factory=Client)
    try:
        return executor.call_tool_result(name, args)
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        set_tool_runtime_resolver(saved_resolver)
        executor.close()


def test_synthesize_on_raw_bridge_returns_raw_result_without_post_tool_use(
    tmp_path: Path,
) -> None:
    """FIX — a synthesize decision on the MCP Apps raw bridge (``call_tool_result``,
    ``return_raw=True``) must return ``intercept.result`` directly and must NOT fire
    PostToolUse, mirroring the normal path's "MCP Apps bridge is not model-facing: no
    PostToolUse" guard. Before the fix this branch ignored ``return_raw`` and returned
    ``apply_post_tool_hook(...)`` (a synthetic string/None) instead of the raw result."""

    post_seen: list[dict[str, Any]] = []

    def post_tool(name, args, observation, is_error, synthetic):  # noqa: ANN001
        post_seen.append({"name": name, "observation": observation, "synthetic": synthetic})
        return observation

    raw_result = {"content": [{"type": "text", "text": "RAW CACHED"}], "isError": False}
    hooks = ToolRuntimeHooks(
        tool_interceptor=lambda n, a: InterceptDecision(kind="synthesize", result=raw_result),
        post_tool=post_tool,
    )
    result = _run_raw(hooks, "echo", {"text": "hi"})

    assert result is raw_result, "the raw bridge must return the raw-shaped intercept result"
    assert _REAL_CALLS == [], "synthesize must SKIP the real tool call on the raw bridge too"
    assert post_seen == [], "the MCP Apps bridge is not model-facing: PostToolUse must not fire"


# --------------------------------------------------------------------------- #
# End-to-end: the REAL gate stashes, the REAL interceptor consumes, driven      #
# through the REAL SyncMCPToolExecutor in one synchronous call. The two links   #
# (gate->stash in test_gact/test_hook_dispatcher.py, interceptor->skip above)   #
# are otherwise only proven separately.                                         #
# --------------------------------------------------------------------------- #


def test_end_to_end_real_gate_stash_interceptor_skips_real_tool(tmp_path: Path) -> None:
    """A PreToolUse hook ``synthesize`` decision, dispatched by the REAL stashing gate
    (``_make_permission_gate``) and consumed by the REAL ``pre_tool_interceptor``,
    skips the real tool through the full ``SyncMCPToolExecutor`` path — proving the
    gate->stash->interceptor chain composes end to end, not just in isolated halves."""

    body = (
        "import json\n"
        'print(json.dumps({"decision": "synthesize", "result": "CACHED:real-gate"}))\n'
    )
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body, hook_id="cache")
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        # approval_mode="bypass" so the gate actually resolves to "allow" (rather than
        # registering a pending permission and blocking) after the hook's non-denied
        # synthesize outcome is stashed — the point under test is the FULL chain,
        # including the gate returning "allow" so the interceptor gets to run.
        app.state.sessions.create(workspace_id="w1", title="t", approval_mode="bypass")
        gate = _make_permission_gate(app)
        hooks = ToolRuntimeHooks(permission_gate=gate, tool_interceptor=pre_tool_interceptor)

        saved_resolver = None
        _REAL_CALLS.clear()
        set_tool_runtime_resolver(None)
        set_tool_runtime_fallback(hooks)
        executor = SyncMCPToolExecutor(_echo_server(), timeout=5.0, client_factory=Client)
        try:
            result = executor.call_tool("echo", {"text": "hi"})
        finally:
            set_tool_runtime_fallback(ToolRuntimeHooks())
            set_tool_runtime_resolver(saved_resolver)
            executor.close()
    finally:
        install_global_dispatcher(None)

    assert result == "CACHED:real-gate"
    assert _REAL_CALLS == [], (
        "the real gate's PreToolUse stash + the real interceptor's consume-once read "
        "must skip the real tool end to end"
    )
