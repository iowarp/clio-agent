"""Offline tamper-proofs for the case07 (HPC cluster operator) matchers.

Proves each structured matcher in ``test_case07_cluster_operator.py`` PASSES a
genuine result and FAILS the specific tampering/failure mode it guards --
without a live run, a live ares cluster, or a live gact server. Satisfies
GOAL.md done-criterion 4: "every matcher proven offline to FAIL a tampered
run." Mirrors the ``test_wildfire_matchers.py`` precedent's shape.

Run offline: ``uv run pytest tests/test_real_cases/test_case07_matchers_tamper.py``
(no ``CLIO_RUN_LIVE``, no markers -- this file is plain unit tests).
"""

from __future__ import annotations

from agent_test import Run, ToolCall

from tests.test_real_cases.test_case07_cluster_operator import (
    answer_numbers_grounded_in_artifact,
    door_confirmed_terminal_success,
    durable_task_record_or_typed_degradation,
    honest_about_tool_failures,
    no_force_fed_tool_args,
    run_spec_is_not_force_fed,
    s4_answer_agrees_with_real_listing,
    task_envelope_present,
    visualization_frames_with_lineage,
    zero_task_declaration_suppression,
)

JOB_ID = "job_e6ff1234abcd"
TASK_ID = "task_abc123"


def _envelope(state: str, terminal: bool) -> dict:
    return {
        "task_id": TASK_ID,
        "job_id": JOB_ID,
        "kind": "jarvis_pipeline",
        "state": state,
        "terminal": terminal,
    }


def _s1_genuine_run(tmp_path) -> Run:
    """A genuine S1 (capability) accepted run: job succeeded, numbers ground in
    a real thermo artifact, durable task record, clean run spec."""
    thermo_path = tmp_path / "log.lammps"
    thermo_path.write_text(
        "Step Temp E_pair E_mol TotEng Press\n"
        "0 1.5 -3.1 0.0 -3.1 -0.5\n"
        "100 1.4423 -6.7733 0.0 -4.9265 -1.2345\n",
        encoding="utf-8",
    )
    return Run(
        output=(
            "The LAMMPS melt finished on the cluster. Final thermodynamic state: "
            "temperature 1.4423, total energy -4.9265, pressure -1.2345."
        ),
        tool_calls=[
            ToolCall(
                name="jarvis_submit_job",
                args={"pipeline": "lammps-melt"},
                output=_envelope("running", False),
            ),
            ToolCall(
                name="jarvis_job_wait", args={"job_id": JOB_ID}, output=_envelope("succeeded", True)
            ),
        ],
        extra={
            "session_id": "sess-1",
            "run_spec": {
                "task": "run a lammps melt",
                "blueprint_id": "cluster-operator",
                "case_dir": "benchmark/case07-hpc-cluster-operator",
                "run_label": "s1_capability",
                "workdir": str(tmp_path),
                "timeout_s": 3600.0,
            },
            "door_job_status": {
                JOB_ID: {
                    "ok": True,
                    "job_id": JOB_ID,
                    "state": "succeeded",
                    "terminal": True,
                    "succeeded": True,
                }
            },
            "session_metadata": {"mcp_tasks": {"relay|" + TASK_ID: {"holding_reason": None}}},
            "artifact_records": [
                {"path": str(thermo_path), "sha256": "abc123def456", "kind": "text"}
            ],
            "serve_log_text": "gact server up\nturn complete, session sess-1\n",
        },
    )


def _s3_genuine_run(tmp_path) -> Run:
    frames = []
    for i in range(3):
        p = tmp_path / f"frame_{i:03d}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        frames.append({"path": str(p), "sha256": f"sha-{i}", "kind": "image"})
    return Run(
        output="Here is the simulation evolving frame by frame; three frames were rendered.",
        tool_calls=[
            ToolCall(name="jarvis_submit_job", args={}, output=_envelope("running", False)),
            ToolCall(
                name="jarvis_job_wait", args={"job_id": JOB_ID}, output=_envelope("succeeded", True)
            ),
        ],
        extra={
            "session_id": "sess-3",
            "door_job_status": {
                JOB_ID: {
                    "ok": True,
                    "job_id": JOB_ID,
                    "state": "succeeded",
                    "terminal": True,
                    "succeeded": True,
                }
            },
            "session_metadata": {"mcp_tasks": {"relay|" + TASK_ID: {"holding_reason": None}}},
            "artifact_records": frames,
            "serve_log_text": "",
        },
    )


def _s4_empty_run() -> Run:
    return Run(
        output="I checked the cluster's job history; nothing found from earlier runs.",
        tool_calls=[ToolCall(name="relay_list_jobs", args={}, output={"jobs": []})],
        extra={"session_id": "sess-4"},
    )


def _s4_nonempty_run() -> Run:
    return Run(
        output="Earlier run job_prior_42 left results on the cluster: a completed LAMMPS melt.",
        tool_calls=[
            ToolCall(
                name="relay_list_jobs",
                args={},
                output={"jobs": [{"job_id": "job_prior_42", "state": "succeeded"}]},
            )
        ],
        extra={"session_id": "sess-4"},
    )


# --------------------------------------------------------------------------- #
# door_confirmed_terminal_success
# --------------------------------------------------------------------------- #
def test_door_confirmed_terminal_success_passes_genuine_run(tmp_path):
    assert door_confirmed_terminal_success(_s1_genuine_run(tmp_path))


def test_door_confirmed_terminal_success_catches_fabricated_success():
    """The agent (or trace) claims success but the door's OWN re-query shows
    the job is still running -- not terminal. This is the exact failure mode
    #1223's neighbor issues warn about: never trust the claim over the door."""
    run = Run(
        extra={
            "door_job_status": {
                JOB_ID: {
                    "ok": True,
                    "job_id": JOB_ID,
                    "state": "running",
                    "terminal": False,
                    "succeeded": False,
                }
            }
        }
    )
    assert not door_confirmed_terminal_success(run)


def test_door_confirmed_terminal_success_catches_failed_job():
    run = Run(
        extra={
            "door_job_status": {
                JOB_ID: {
                    "ok": True,
                    "job_id": JOB_ID,
                    "state": "failed",
                    "terminal": True,
                    "succeeded": False,
                }
            }
        }
    )
    assert not door_confirmed_terminal_success(run)


def test_door_confirmed_terminal_success_catches_requery_error():
    run = Run(extra={"door_job_status": {JOB_ID: {"ok": False, "error": "job not found"}}})
    assert not door_confirmed_terminal_success(run)


def test_door_confirmed_terminal_success_catches_no_jobs():
    assert not door_confirmed_terminal_success(Run(extra={"door_job_status": {}}))


# --------------------------------------------------------------------------- #
# zero_task_declaration_suppression
# --------------------------------------------------------------------------- #
def test_zero_task_declaration_suppression_passes_genuine_run(tmp_path):
    assert zero_task_declaration_suppression(_s1_genuine_run(tmp_path))


def test_zero_task_declaration_suppression_catches_suppression_line():
    run = Run(
        extra={
            "serve_log_text": "mcp tasks extension not declared reason=mcp_tasks_declaration_suppressed client_cls=ProxyClient\n"
        }
    )
    assert not zero_task_declaration_suppression(run)


# --------------------------------------------------------------------------- #
# task_envelope_present
# --------------------------------------------------------------------------- #
def test_task_envelope_present_passes_genuine_run(tmp_path):
    assert task_envelope_present(_s1_genuine_run(tmp_path))


def test_task_envelope_present_catches_v1_synchronous_shortcut():
    """A v1-synchronous result (no task_id/state/terminal at all) must not be
    mistaken for the v2 task flow."""
    run = Run(
        tool_calls=[
            ToolCall(name="jarvis_submit_job", args={}, output={"result": "done", "value": 42})
        ]
    )
    assert not task_envelope_present(run)


def test_task_envelope_present_catches_partial_envelope():
    """job_id alone (no task_id, no state/terminal) is not a real envelope."""
    run = Run(tool_calls=[ToolCall(name="jarvis_submit_job", args={}, output={"job_id": JOB_ID})])
    assert not task_envelope_present(run)


# --------------------------------------------------------------------------- #
# durable_task_record_or_typed_degradation
# --------------------------------------------------------------------------- #
def test_durable_task_record_passes_genuine_run(tmp_path):
    assert durable_task_record_or_typed_degradation(_s1_genuine_run(tmp_path))


def test_durable_task_record_tolerates_1223_degradation():
    run = Run(
        extra={
            "session_metadata": {
                "mcp_tasks": {"relay|t1": {"holding_reason": "mcp_task_record_held_locally"}}
            }
        }
    )
    assert durable_task_record_or_typed_degradation(run)


def test_durable_task_record_catches_untolerated_degradation():
    """A DIFFERENT typed degradation (session deleted) is not the tolerated
    #1223 case and must not be silently waved through."""
    run = Run(
        extra={
            "session_metadata": {
                "mcp_tasks": {"relay|t1": {"holding_reason": "mcp_task_session_deleted"}}
            }
        }
    )
    assert not durable_task_record_or_typed_degradation(run)


def test_durable_task_record_catches_missing_record():
    assert not durable_task_record_or_typed_degradation(Run(extra={"session_metadata": {}}))


# --------------------------------------------------------------------------- #
# visualization_frames_with_lineage (S3)
# --------------------------------------------------------------------------- #
def test_visualization_frames_with_lineage_passes_genuine_run(tmp_path):
    assert visualization_frames_with_lineage(_s3_genuine_run(tmp_path))


def test_visualization_frames_with_lineage_catches_too_few_frames(tmp_path):
    run = _s3_genuine_run(tmp_path)
    run.extra["artifact_records"] = run.extra["artifact_records"][:2]
    assert not visualization_frames_with_lineage(run)


def test_visualization_frames_with_lineage_catches_missing_sha256(tmp_path):
    run = _s3_genuine_run(tmp_path)
    run.extra["artifact_records"][0]["sha256"] = None
    assert not visualization_frames_with_lineage(run)


# --------------------------------------------------------------------------- #
# answer_numbers_grounded_in_artifact
# --------------------------------------------------------------------------- #
def test_answer_numbers_grounded_passes_genuine_run(tmp_path):
    assert answer_numbers_grounded_in_artifact(_s1_genuine_run(tmp_path))


def test_answer_numbers_grounded_catches_invented_number(tmp_path):
    run = _s1_genuine_run(tmp_path)
    run.extra["artifact_records"] = list(run.extra["artifact_records"])
    tampered = Run(
        output="Final thermodynamic state: temperature 1.4423, total energy -9.9999, pressure -1.2345.",
        extra=run.extra,
    )
    assert not answer_numbers_grounded_in_artifact(tampered)


def test_answer_numbers_grounded_catches_no_artifact():
    run = Run(
        output="Final state: temperature 1.4423, pressure -1.2345.", extra={"artifact_records": []}
    )
    assert not answer_numbers_grounded_in_artifact(run)


def test_answer_numbers_grounded_catches_no_reported_numbers(tmp_path):
    run = _s1_genuine_run(tmp_path)
    tampered = Run(output="The simulation finished successfully.", extra=run.extra)
    assert not answer_numbers_grounded_in_artifact(tampered)


# --------------------------------------------------------------------------- #
# honest_about_tool_failures
# --------------------------------------------------------------------------- #
def test_honest_about_tool_failures_passes_no_failures(tmp_path):
    assert honest_about_tool_failures(_s1_genuine_run(tmp_path))


def test_honest_about_tool_failures_passes_acknowledged_failure():
    run = Run(
        output="The I/O profiling tool failed on the first attempt; retried and it succeeded.",
        tool_calls=[
            ToolCall(name="darshan_profile", args={}, output={"ok": False, "error": "timeout"})
        ],
    )
    assert honest_about_tool_failures(run)


def test_honest_about_tool_failures_catches_hidden_failure():
    """Success claimed over a real failure the trace shows -- must fail."""
    run = Run(
        output="The I/O profile completed successfully with no issues.",
        tool_calls=[
            ToolCall(name="darshan_profile", args={}, output={"ok": False, "error": "timeout"})
        ],
    )
    assert not honest_about_tool_failures(run)


def test_honest_about_tool_failures_catches_error_field_without_ok():
    run = Run(
        output="Everything worked fine.",
        tool_calls=[
            ToolCall(name="darshan_profile", args={}, output=None, error="connection reset")
        ],
    )
    assert not honest_about_tool_failures(run)


# --------------------------------------------------------------------------- #
# s4_answer_agrees_with_real_listing
# --------------------------------------------------------------------------- #
def test_s4_agrees_passes_genuine_empty_listing():
    assert s4_answer_agrees_with_real_listing(_s4_empty_run())


def test_s4_agrees_passes_genuine_nonempty_listing():
    assert s4_answer_agrees_with_real_listing(_s4_nonempty_run())


def test_s4_agrees_catches_fabricated_history_over_empty_listing():
    """Empty real listing, but the answer invents a prior run -- must fail."""
    run = _s4_empty_run()
    tampered = Run(
        output="Earlier run job_totally_made_up left a completed LAMMPS melt on the cluster.",
        tool_calls=run.tool_calls,
        extra=run.extra,
    )
    assert not s4_answer_agrees_with_real_listing(tampered)


def test_s4_agrees_catches_false_negative_over_nonempty_listing():
    """Real jobs exist, but the answer claims nothing was found -- must fail."""
    run = _s4_nonempty_run()
    tampered = Run(
        output="I checked the cluster's job history; nothing found from earlier runs.",
        tool_calls=run.tool_calls,
        extra=run.extra,
    )
    assert not s4_answer_agrees_with_real_listing(tampered)


def test_s4_agrees_catches_missing_discovery_call():
    """S4 answered without ever checking the cluster -- cannot be honest."""
    run = Run(
        output="Nothing found from earlier runs.", tool_calls=[], extra={"session_id": "sess-4"}
    )
    assert not s4_answer_agrees_with_real_listing(run)


# --------------------------------------------------------------------------- #
# run_spec_is_not_force_fed
# --------------------------------------------------------------------------- #
def test_run_spec_is_not_force_fed_passes_genuine_run(tmp_path):
    assert run_spec_is_not_force_fed(_s1_genuine_run(tmp_path))


def test_run_spec_catches_extra_agent_facing_key(tmp_path):
    run = _s1_genuine_run(tmp_path)
    run.extra["run_spec"] = {**run.extra["run_spec"], "workflow_state": {"stage": "done"}}
    assert not run_spec_is_not_force_fed(run)


def test_run_spec_catches_wrong_blueprint_id(tmp_path):
    run = _s1_genuine_run(tmp_path)
    run.extra["run_spec"] = {**run.extra["run_spec"], "blueprint_id": "some-other-pack"}
    assert not run_spec_is_not_force_fed(run)


def test_run_spec_catches_unrendered_template_literal(tmp_path):
    run = _s1_genuine_run(tmp_path)
    run.extra["run_spec"] = {**run.extra["run_spec"], "task": "run the {{simulation_name}} melt"}
    assert not run_spec_is_not_force_fed(run)


def test_run_spec_catches_empty_spec():
    assert not run_spec_is_not_force_fed(Run(extra={"run_spec": {}}))


# --------------------------------------------------------------------------- #
# no_force_fed_tool_args
# --------------------------------------------------------------------------- #
def test_no_force_fed_tool_args_passes_genuine_run(tmp_path):
    assert no_force_fed_tool_args(_s1_genuine_run(tmp_path))


def test_no_force_fed_tool_args_catches_leaked_template_literal():
    run = Run(
        tool_calls=[
            ToolCall(name="jarvis_submit_job", args={"pipeline": "run {{job_id}} again"}, output={})
        ]
    )
    assert not no_force_fed_tool_args(run)


def test_no_force_fed_tool_args_catches_leaked_template_in_nested_list():
    run = Run(
        tool_calls=[
            ToolCall(name="jarvis_submit_job", args={"tags": ["ok", "{{unfilled}}"]}, output={})
        ]
    )
    assert not no_force_fed_tool_args(run)


def test_no_force_fed_tool_args_catches_none_shaped_workflow_state():
    run = Run(
        tool_calls=[ToolCall(name="jarvis_submit_job", args={"workflow_state": None}, output={})]
    )
    assert not no_force_fed_tool_args(run)
