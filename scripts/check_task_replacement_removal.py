"""Reject reintroduction of the task owners removed by the R2 replacement.

Run directly with uv; this guard does not import the deliberately broken product.
Historical data may retain old field names, but may not import an execution owner.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOVED_MODULES = frozenset(
    [
        "clio_agent.gact.agent_tasks",
        "clio_agent.gact.agent_task_admission",
        "clio_agent.gact.agent_task_recovery",
        "clio_agent.gact.agent_task_notifications",
        "clio_agent.gact.assignment_history",
        "clio_agent.gact.agent_mailbox",
        "clio_agent.gact.async_user_question",
        "clio_agent.gact.child_forward",
        "clio_agent.gact.core_task_journal",
        "clio_agent.gact.delegation_return",
        "clio_agent.gact.human_task_intervention",
        "clio_agent.gact.loop_inbox",
        "clio_agent.gact.mcp_task_events",
        "clio_agent.gact.mcp_task_store",
        "clio_agent.gact.question_tasks",
        "clio_agent.gact.session_run_tasks",
        "clio_agent.gact.subagent_lifecycle",
        "clio_agent.gact.subagent_restart",
        "clio_agent.gact.task_backend_identity",
        "clio_agent.gact.task_completion_wake",
        "clio_agent.gact.task_context_frame",
        "clio_agent.gact.task_controls",
        "clio_agent.gact.task_delivery",
        "clio_agent.gact.task_fold",
        "clio_agent.gact.task_input_owner",
        "clio_agent.gact.task_input_questions",
        "clio_agent.gact.task_mcp_owner",
        "clio_agent.gact.task_notifications",
        "clio_agent.gact.task_permissions",
        "clio_agent.gact.task_projection",
        "clio_agent.gact.task_recovery",
        "clio_agent.gact.task_recovery_client",
        "clio_agent.gact.task_relay_owner",
        "clio_agent.gact.task_subagent_owner",
        "clio_agent.gact.task_submission_custody",
        "clio_agent.gact.task_subscriptions",
        "clio_agent.gact.task_supervisor",
        "clio_agent.gact.task_tools",
        "clio_agent.gact.turn_spawn",
        "clio_agent.gact.turn_spawn_executor",
        "clio_agent.gact.workflow_step_watch",
        "clio_agent.gact.run_registry",
        "clio_agent.gact.agent_targets",
        "clio_agent.gact.agent_messaging",
        "clio_agent.gact.agent_session_messaging",
        "clio_agent.gact.agent_message_transport",
        "clio_agent.gact.relay_wiring",
        "clio_agent.gact.relay_status",
        "clio_agent.gact.relay_timeline",
        "clio_agent.gact.agents.relay_expert_invoker",
        "clio_agent.gact.agents.relay_invoker_runtime",
        "clio_agent.gact.agents.agent_task_input_refs",
        "clio_agent.gact.routes.agent_tasks",
        "clio_agent.gact.routes.async_processes",
        "clio_agent.gact.routes.relay",
        "clio_agent.tools.mcp_tasks",
        "clio_agent.tools.task_call_context.require_admission",
        "clio_agent.tools.mcp_task_records",
        "clio_agent.tools.mcp_task_lease",
        "clio_agent.tools.mcp_task_extension",
        "clio_agent.tools.mcp_wait_ladder",
        "clio_agent.tools.task_driver_lease",
        "clio_agent.tools.task_observers",
        "clio_agent.tools.task_submission",
        "clio_agent.tools.native_task_host",
        "clio_agent.tools.native_task_tool",
        "clio_agent.tools.mcp_call_progress",
        "clio_agent.tools.relay_transport",
        "clio_agent.tools.relay_factory",
        "clio_agent.tools.relay_contract",
        "clio_agent.tools.relay_console_stream",
        "clio_agent.tools.relay_console",
        "clio_agent.tools.relay_cli_runner",
        "clio_agent.tools.relay_artifact_fetch",
        "clio_agent.tools.relay_install_jobs",
        "clio_agent.tools.relay_install_surface",
        "clio_agent.gact.agent_lifecycle_tools",
        "clio_agent.gact.ask_user_tool",
        "clio_agent.gact.background_exit",
        "clio_agent.gact.goal_wait",
        "clio_agent.gact.live_handle",
        "clio_agent.gact.message_admission",
        "clio_agent.gact.user_question_ledger",
        "clio_agent.gact.user_question_resume",
        "clio_agent.gact.user_question_pause",
        "clio_agent.gact.agent_elicitation_answer_turn",
        "clio_agent.gact.agents.invoker",
        "clio_agent.gact.agents.observe_runtime",
        "clio_agent.gact.agents.spawn_placement",
        "clio_agent.gact.agents.spawn_runtime",
        "clio_agent.gact.agents.spawn_runtime_declarations",
        "clio_agent.gact.agents.skill_spawn_effect",
        "clio_agent.tools.jarvis_jobs",
        "clio_agent.tools.jarvis_result_contract",
        "clio_agent.tools.remote_mcp",
        "clio_agent.gact.routes.agent_lifecycle",
        "clio_agent.tools.servers.storage_server",
        "clio_agent.gact.elicitation_forwarding",
        "clio_agent.gact.routes.session_questions",
        "clio_agent.gact.routes.message_intents",
        "clio_agent.gact.agent_elicitation_dispatch",
        "clio_agent.gact.agent_elicitation_answer_inline",
        "clio_agent.gact.agent_elicitation_activity",
        "clio_agent.gact.elicitation_bridge",
        "clio_agent.gact.elicitation_correlation",
    ]
)
REMOVED_NAMES = frozenset(
    {
        "_repair_divergent_lane",
        "_schedule_lane_mint",
        "_backfill_or_serve",
        "run_transcript_job",
        "resume_turn_defer",
        "_stage_resume_turn",
        "_resume_user_prompt_defer",
        "_resume_stop_defer",
        "watch_step",
        "PENDING_ASK_USER_META",
        "refresh_session_inputs",
        "release_claim",
        "AgentTaskRegistry",
        "_MAX_DRAIN_PASSES",
        "register_async_process_routes",
        "discard_pending",
        "TaskSupervisor",
        "accept_pending",
        "persist_agent_task",
        "configure_relay_expert_invokers",
        "waiting_presentation",
        "task_record_store_is_durable",
        "_poll_until_terminal",
        "CoreTaskJournal",
        "_build_tasks",
        "CoreTaskRecordStore",
        "agent_task_executor_for_depth",
        "TaskLease",
        "seed_agent_task",
        "install_agent_task_executor",
        "install_mcp_task_event_publisher",
        "mark_input_committed",
        "restore_user_questions",
        "app_task_store",
        "MessageIntentStore",
        "set_assignment_disposition",
        "InMemoryTaskRecordStore",
        "drive_with_lease",
        "build_wait_tool",
        "claim_pending",
        "mark_consumed",
        "relay_capabilities",
        "consume_notification",
        "shutdown_agent_task_executors",
        "register_relay_routes",
        "drive_task_to_terminal",
        "install_session_task_store",
        "restore_pending_ask_user_questions",
        "stage_session_inputs",
        "_wait_agent_tasks_call_metadata",
        "refresh_relay_tool_surfaces_if_stale",
        "construct_agent_with_relay",
        "accept_session_run",
        "NativeTaskHost",
        "_retire_pending_steer_intent",
        "pending_turn_yield",
        "FORWARDED_QUESTION_SOURCE",
        "claim_question_transition",
        "install_agent_task_registry",
    }
)
REMOVED_STATE = frozenset(
    {
        "_pending_task_submissions",
        "_remote_mcp_federation",
        "_accepted_task_handles",
        "_clio_federation_epoch",
        "task_store",
        "_relay_federation_epoch",
        "user_questions",
        "agent_task_registry",
        "task_supervisor",
        "agent_task_executors",
        "core_task_journal",
        "automatic_input_hold",
        "pending_loop_inbox_drain",
        "transfer_settled",
        "_repair_indexes",
    }
)
REMOVED_CONFIG = frozenset(
    {
        "agents.child_forward_deadline_s",
        "workflows.step_inactivity_s",
        "CLIO_WORKFLOW_STEP_INACTIVITY_S",
        "CLIO_CHILD_FORWARD_DEADLINE_S",
        "gact.ledger_retention.user_questions.",
        "CLIO_LEDGER_USER_QUESTIONS_",
        "gact.loop_inbox.max_events",
        "CLIO_GACT_LOOP_INBOX_MAX_EVENTS",
        "gact.message_intents.max_queued_per_session",
        "CLIO_GACT_MAX_QUEUED_MESSAGES_PER_SESSION",
    }
)
# No unfinished branch interface has a compatibility exemption.
COMPATIBILITY_ALIASES: frozenset[str] = frozenset()

# These exact string literals decode old data only. Attributes, imports and
# definitions remain forbidden even in this file.
HISTORICAL_BINDINGS = frozenset({("src/clio_agent/gact/task_migration.py", "user_questions")})


def violations(root: Path = ROOT) -> list[str]:
    """Find retired production authority, import, configuration and catalog bindings."""
    findings: list[str] = []
    for path in sorted((root / "src" / "clio_agent").rglob("*.py")):
        source = path.read_text(encoding="utf-8-sig")
        relative = path.relative_to(root).as_posix()
        module_name = ".".join(path.relative_to(root / "src").with_suffix("").parts)
        if module_name in REMOVED_MODULES:
            findings.append(f"{relative}: retired module path restored")
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            findings.append(f"{relative}:{error.lineno}: cannot audit syntax: {error.msg}")
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "history"
                    or isinstance(node.value, ast.Attribute)
                    and node.value.attr in {"history", "task_history"}
                )
                and relative
                not in {
                    "src/clio_agent/gact/task_history.py",
                    "src/clio_agent/gact/task_history_projection.py",
                }
            ):
                findings.append(
                    f"{relative}:{node.lineno}: owner bypasses the explicit Core history API"
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "TaskSnapshot"
                and node.func.attr == "read_history"
                and relative
                not in {
                    "src/clio_agent/gact/task_history_projection.py",
                    "src/clio_agent/gact/task_migration.py",
                    # The one-time lane pass decodes stored observations as replay does.
                    "src/clio_agent/gact/task_lane_reads.py",
                }
            ):
                findings.append(
                    f"{relative}:{node.lineno}: live owner uses historical vocabulary decoding"
                )
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if node.level:
                    package = module_name.split(".")[:-1]
                    imported = ".".join(
                        package[: len(package) + 1 - node.level] + ([imported] if imported else [])
                    )
                candidates = {imported, *(f"{imported}.{alias.name}" for alias in node.names)}
                for module in candidates & REMOVED_MODULES:
                    findings.append(f"{relative}:{node.lineno}: removed import {module}")
                for alias in node.names:
                    if alias.name in REMOVED_NAMES:
                        findings.append(
                            f"{relative}:{node.lineno}: retired imported symbol {alias.name}"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in REMOVED_MODULES:
                        findings.append(f"{relative}:{node.lineno}: removed import {alias.name}")
            if isinstance(node, ast.Name) and node.id in REMOVED_NAMES:
                findings.append(f"{relative}:{node.lineno}: retired symbol {node.id}")
            elif (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in REMOVED_NAMES
            ):
                findings.append(f"{relative}:{node.lineno}: retired definition {node.name}")
            elif isinstance(node, ast.Attribute) and node.attr in REMOVED_NAMES | REMOVED_STATE:
                findings.append(f"{relative}:{node.lineno}: retired attribute {node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if (
                    node.value in REMOVED_NAMES | REMOVED_STATE | REMOVED_CONFIG | REMOVED_MODULES
                    and (relative, node.value) not in HISTORICAL_BINDINGS
                ):
                    findings.append(f"{relative}:{node.lineno}: retired binding {node.value}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                blocked_call = None
                if node.func.attr == "run_in_executor" and len(node.args) > 1:
                    if isinstance(node.args[0], ast.Constant) and node.args[0].value is None:
                        blocked_call = node.args[1]
                elif node.func.attr == "to_thread" and node.args:
                    blocked_call = node.args[0]
                if relative.startswith("src/clio_agent/providers/") and isinstance(
                    blocked_call, ast.Attribute
                ):
                    if blocked_call.attr in {"get", "acquire"}:
                        findings.append(
                            f"{relative}:{node.lineno}: provider queue/lock wait on default executor"
                        )
                elif relative.startswith("src/clio_agent/providers/") and isinstance(
                    blocked_call, ast.Lambda
                ):
                    if any(
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr in {"get", "acquire"}
                        for child in ast.walk(blocked_call)
                    ):
                        findings.append(
                            f"{relative}:{node.lineno}: provider queue/lock lambda on default executor"
                        )
    defaults = root / "src" / "clio_agent" / "config.defaults.yaml"
    for number, line in enumerate(defaults.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("relay.") or any(key in line for key in REMOVED_CONFIG):
            findings.append(f"{defaults.relative_to(root)}:{number}: removed driver/relay config")
    for relative in ("docs/ENVIRONMENT.md", "scripts/config_key_notes.py"):
        path = root / relative
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "CLIO_RELAY_" in line or any(key in line for key in REMOVED_CONFIG):
                findings.append(
                    f"{relative}:{number}: obsolete driver/relay configuration expectation"
                )
    return sorted(set(findings))


def main() -> None:
    """Exit unsuccessfully while a removed owner or binding remains in production."""
    findings = violations()
    for finding in findings:
        print(finding)
    print(f"Task replacement removal guard: {len(findings)} violations")
    raise SystemExit(bool(findings))


if __name__ == "__main__":
    main()
