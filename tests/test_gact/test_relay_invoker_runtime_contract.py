"""Contract-pinning tests for the relay submit tool/wire (issues #1221, #1222).

Every existing relay-transport test mocks the transport's tool-name layer, so a
hardcoded wrong tool name in :data:`RELAY_REMOTE_AGENT_TOOL` was invisible to the
whole suite: the mock and the code drifted together and stayed "green" against
each other while being dead against the real relay door (``relay_submit_remote_agent``
was submitted; the door only ever advertised ``relay_submit_agent``, #1221).

The first test below does NOT mock the transport. Instead it pins the constant
against a fixture carrying the door's REAL ``tools/list`` catalog -- the exact
12-tool set captured live from ``127.0.0.1:18796/mcp`` during the #1221
investigation. If the constant ever again names a tool the door does not actually
advertise, this test fails red without needing a live relay connection.

The remaining tests pin two more contract facts discovered live while proving the
#1221 fix end to end (folded in as #1222): the door's real ``relay_submit_agent``
``inputSchema`` has no inline ``context`` argument (``remote_agent_task_spec`` must
not send one), and its real completion envelope is a raw JARVIS-CD job/artifact
record, not the ``TaskResult`` boundary shape the invoker used to assume
(``relay_job_failure_reason`` must recognize it). Both fixtures below are captured
verbatim from the same live investigation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from clio_agent.gact.agent_tasks import ERROR_REASONS, STATUS_COMPLETED, AgentTask
from clio_agent.gact.agents.invoker import TaskHandle, TaskSpec
from clio_agent.gact.agents.relay_expert_invoker import RelayExpertInvoker
from clio_agent.gact.agents.relay_invoker_runtime import (
    RELAY_REMOTE_AGENT_TOOL,
    relay_job_failure_reason,
)

# Captured live via a direct MCP `tools/list` call against the relay door
# (127.0.0.1:18796/mcp, desktop-local cluster, #1221 investigation, 2026-08-18).
# This is the door's real advertised catalog, independent of any local mock.
RELAY_DOOR_TOOLS_LIST_FIXTURE: frozenset[str] = frozenset(
    {
        "relay_artifact_lineage",
        "relay_bind_jarvis_runtime",
        "relay_cancel",
        "relay_observe",
        "relay_queue_diagnose",
        "relay_queue_list",
        "relay_queue_stale",
        "relay_remote_mcp_context",
        "relay_status",
        "relay_storage_status",
        "relay_submit_agent",
        "relay_wait",
    }
)


def test_relay_remote_agent_tool_constant_is_in_the_doors_real_catalog() -> None:
    """``RELAY_REMOTE_AGENT_TOOL`` must name a tool the real door advertises.

    Red before #1221's fix (the constant was ``"relay_submit_remote_agent"``,
    which is NOT a member of the door's real catalog); green after (the constant
    is ``"relay_submit_agent"``, which IS).
    """

    assert RELAY_REMOTE_AGENT_TOOL in RELAY_DOOR_TOOLS_LIST_FIXTURE, (
        f"RELAY_REMOTE_AGENT_TOOL={RELAY_REMOTE_AGENT_TOOL!r} is not a member of "
        "the relay door's real tools/list catalog -- this constant has drifted "
        "from the live contract (see #1221)."
    )


def test_relay_door_fixture_catalog_size_matches_the_live_probe() -> None:
    """Guard the fixture itself: the live door advertised exactly 12 tools.

    A shrinking or growing fixture without an accompanying re-probe of the live
    door would silently weaken the contract pin above.
    """

    assert len(RELAY_DOOR_TOOLS_LIST_FIXTURE) == 12


# The door's real relay_submit_agent inputSchema (captured live from
# 127.0.0.1:18796/mcp, #1222 investigation, 2026-08-18): additionalProperties is
# false and "context" is not a declared property.
RELAY_SUBMIT_AGENT_SCHEMA_PROPERTIES_FIXTURE: frozenset[str] = frozenset(
    {
        "cluster",
        "prompt_path",
        "mcp_config_path",
        "model",
        "workdir",
        "timeout_seconds",
        "request_followup_message",
        "idempotency_key",
        "used_artifact_refs",
        "wait_for_terminal",
        "wait_timeout_seconds",
        "poll_seconds",
    }
)


def test_remote_agent_task_spec_only_sends_door_recognized_arguments() -> None:
    """``remote_agent_task_spec`` must not send a key the door's real schema forbids.

    Red before #1222's fix (the wire carried ``"context"``, which the door's
    ``additionalProperties: false`` schema rejects at submission -- confirmed live:
    ``relay arguments for 'relay_submit_agent' do not match its discovered
    inputSchema: unknown ['context']``); green after.
    """

    invoker = RelayExpertInvoker(
        app=cast(Any, None),  # unused by remote_agent_task_spec()
        client_factory=lambda _sid: None,
        cluster="desktop-local",
        prompt_path="ladder-l3-nonexistent-prompt.md",
    )
    spec = TaskSpec(
        child_expert_id="compute",
        task_text="probe",
        parent_session_id="session-probe",
    )
    wire = invoker.remote_agent_task_spec(spec)

    unknown = set(wire) - RELAY_SUBMIT_AGENT_SCHEMA_PROPERTIES_FIXTURE
    assert not unknown, (
        f"remote_agent_task_spec sent {sorted(unknown)}, which the door's real "
        "relay_submit_agent inputSchema does not declare (see #1222)."
    )


# The door's real relay_submit_agent completion envelope for a job whose remote
# worker failed before running (captured live, #1222 investigation, 2026-08-18):
# a raw JARVIS-CD job/artifact record, not clio's TaskResult boundary shape.
RELAY_JOB_FAILURE_RESULT_FIXTURE: dict[str, Any] = {
    "content": [{"type": "text", "text": "{...omitted, see structuredContent...}"}],
    "structuredContent": {
        "job": {
            "job_id": "job_de8f8bc13ff44efd945d604e6e38c022",
            "cluster": "desktop-local",
            "kind": "remote_agent",
            "state": "failed",
            "last_error": "ConfigurationError: JARVIS-CD executable not found: jarvis",
        },
    },
}


def test_relay_job_failure_reason_recognizes_the_doors_real_envelope() -> None:
    """``relay_job_failure_reason`` must extract the door's own failure signal.

    Red before #1222's fix (this function did not exist; the invoker's
    ``_terminal_result`` only understood the phantom ``TaskResult`` boundary shape
    and raised ``InvokerError: relay completion omitted its TaskResult boundary
    record`` against this exact live envelope); green after -- the typed
    ``ConfigurationError: JARVIS-CD executable not found: jarvis`` is the sanctioned
    terminal reason.
    """

    reason = relay_job_failure_reason(RELAY_JOB_FAILURE_RESULT_FIXTURE)
    assert reason == "ConfigurationError: JARVIS-CD executable not found: jarvis"


def test_relay_job_failure_reason_ignores_unrecognized_shapes() -> None:
    """A shape with no ``structuredContent.job.state`` must not be misread as a
    failure -- ``_terminal_result`` needs ``None`` here so it keeps raising loudly
    for a genuinely unrecognized envelope instead of fabricating a reason."""

    assert relay_job_failure_reason({"content": []}) is None
    assert relay_job_failure_reason({"structuredContent": {"job": {"state": "running"}}}) is None
    assert relay_job_failure_reason("not a mapping") is None


def test_relay_terminal_result_uses_a_typed_error_reason_for_a_real_job_failure() -> None:
    """``_terminal_result`` must never pass the raw relay failure text straight
    through as ``error_reason`` -- ``AgentTaskRegistry.transition`` (agent_tasks.py)
    rejects any ``error_reason`` outside its closed ``ERROR_REASONS`` vocabulary
    ("Typed reason catalogs (no free-form strings on the wire)").

    Red on #1222's first cut (``error_reason=relay_job_failure_reason(...)`` sent
    the raw ``"ConfigurationError: JARVIS-CD executable not found: jarvis"`` text
    straight through; the live redrive failed one layer downstream with
    ``AgentTaskError: unknown error_reason 'ConfigurationError: ...'``); green
    after -- ``error_reason`` stays the generic typed ``"agent_error"`` and the
    raw detail travels in ``result.answer_excerpt`` instead, same as any other
    tool-fail completion's message body.
    """

    invoker = RelayExpertInvoker(
        app=cast(Any, None),
        client_factory=lambda _sid: None,
        cluster="desktop-local",
        prompt_path="ladder-l3-nonexistent-prompt.md",
    )
    local = AgentTask(
        task_id="task-1222",
        parent_session_id="session-parent",
        child_session_id="",
    )
    handle = TaskHandle.from_task(local)
    current = SimpleNamespace(
        status="completed",
        result=RELAY_JOB_FAILURE_RESULT_FIXTURE,
        error=None,
    )

    result = invoker._terminal_result(handle, local, current, STATUS_COMPLETED)  # noqa: SLF001

    assert result.error_reason in ERROR_REASONS, (
        f"error_reason={result.error_reason!r} is not in the closed ERROR_REASONS "
        "vocabulary -- AgentTaskRegistry.transition would reject it (see #1222)."
    )
    assert result.result is not None
    assert result.result["answer_excerpt"] == (
        "ConfigurationError: JARVIS-CD executable not found: jarvis"
    )
