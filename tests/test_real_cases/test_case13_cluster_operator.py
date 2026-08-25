"""HPC Cluster Operator (case13) - real case acceptance test.

Encodes the contract from ``benchmark/case13-hpc-cluster-operator/GOAL.md``:
can the agent operate the LIVE ares cluster (through clio-relay's MCP v2 task
surface) from a human-level scientific intent, driving async jobs to a real
terminal state and answering only from evidence it can point at?

This is the L3 acceptance of the clio-relay ladder rebuilt as a durable grind
(GOAL.md "Objective"). Structured evidence only, per the matcher plan in
GOAL.md's "Testing harness (agent-test)" section -- every matcher below reads
typed/structured data (tool call args/output, the artifact registry, the
relay door's own re-query, the serve log text), never the model's prose.

Four independent evidence-gathering seams feed the matchers below, populated
by the test body (never by ``clio_sut.ClioAgent``, which stays a
case-agnostic driver shared with earthscope/wildfire -- see its module
docstring):

* ``run.extra["run_spec"]``       -- the exact dict handed to ``agent.run()``.
* ``run.extra["door_job_status"]``-- per job_id, an INDEPENDENT relay-door
  re-query (subprocesses the ``clio-relay`` CLI; imports nothing from relay
  source, per GOAL's non-negotiable #4 on the domain surface).
* ``run.extra["session_metadata"]`` -- the live ``GET /v1/sessions/{id}``
  response's ``metadata`` (carries ``mcp_tasks``, the durable v2 task-record
  home; see ``clio_agent.gact.mcp_task_store``).
* ``run.extra["artifact_records"]`` -- the full artifact-registry rows
  (path/kind/sha256/custody), re-fetched from
  ``GET /v1/sessions/{id}/artifacts`` because ``clio_sut.ClioAgent`` only
  keeps bare paths in ``run.extra["artifacts"]`` (S7 #973); case13 needs the
  lineage (sha256) column the shared SUT does not carry.
* ``run.extra["serve_log_text"]`` -- the isolated gact server's own stdout log
  text (``gact_server.server_log``), grepped for the v2 suppression reason.

Run live (isolated instance, port 17970 -- see ENV.md + bring_up_isolated_serve.sh;
never touches the :17900 ares-mission serve)::

    CLIO_GACT_FIXTURE_PORT=17970 CLIO_RUN_LIVE=1 uv run pytest \\
        tests/test_real_cases/test_case13_cluster_operator.py \\
        -k s1_capability --provider claude_code --model sonnet \\
        -o addopts="" -p no:cacheprovider -q

Tamper-proofs for every matcher below (offline, no live anything) live in
``test_case13_matchers_tamper.py``.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_test import matcher

CASE_DIR = "benchmark/case13-hpc-cluster-operator"
BLUEPRINT_ID = "cluster-operator"

# Guardrail cell (GOAL.md "Case-specific deviations"): claude_code / sonnet only.
# The live ares cluster is shared, real infra -- no matrix fan-out for this case.
GUARDRAIL_PROVIDER = "claude_code"
GUARDRAIL_MODEL = "sonnet"

# ``timeout_s`` is generous: cluster builds (spack install, jarvis pipelines) can
# run long. >=1200s per GOAL's harness instructions; default well above that.
TIMEOUT_S = float(os.environ.get("CLIO_CASE13_TIMEOUT_S", "3600"))

# The p5local door's `clio-relay` CLI binary and the cluster it is bound to
# (see ENV.md / bring_up_isolated_serve.sh). Overridable for a different box.
RELAY_EXE = os.environ.get("CLIO_RELAY_EXE") or str(Path("D:/relay-p5local/bin/clio-relay.exe"))
RELAY_CLUSTER = os.environ.get("CLIO_RELAY_CLUSTER", "ares-p5run2")

# clio_relay.models.JobState (read for reference only -- NEVER imported; the
# CLI subprocess owns all relay-side logic per GOAL's non-negotiable #4).
TERMINAL_JOB_STATES = {"succeeded", "failed", "canceled"}
SUCCESS_JOB_STATES = {"succeeded"}

# The harness's own request to the SUT may carry only the documented minimal
# agent-facing contract (`task` + `blueprint_id`) plus recognized harness
# plumbing that configures the DRIVER, never the agent's reasoning input
# (workdir/case_dir/run_label/trace_path/timeout_s -- see clio_sut.invoke).
ALLOWED_RUN_SPEC_KEYS = {
    "task",
    "blueprint_id",
    "case_dir",
    "run_label",
    "workdir",
    "trace_path",
    "timeout_s",
}

_FRAME_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
_TEMPLATE_LITERAL_RE = re.compile(r"\{\{[^{}]+\}\}")
# Reported "measurements": decimals (temperature/energy/pressure/IO-rate style
# values) or bare integers with >=4 digits (byte counts, timestamps). Deliberately
# excludes small integers (frame indices, retry counts, atom counts in prose)
# which legitimately do not appear verbatim in an output artifact. Calibrate
# further once real S1/S2 traces exist (GOAL: "every trace-read failure is
# encoded" as a tighter matcher).
_MEASUREMENT_RE = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?|-?\d{4,}")
_NOTHING_FOUND_TERMS = (
    "nothing found",
    "no prior",
    "no earlier",
    "no previous",
    "no results",
    "none found",
    "did not find",
    "didn't find",
    "no jobs",
    "no runs",
    "no results were found",
    "no artifacts",
)
_FAILURE_ACK_TERMS = (
    "failed",
    "failure",
    "error",
    "unavailable",
    "timeout",
    "timed out",
    "could not",
    "unable to",
    "refused",
    "cancelled",
    "canceled",
)
_LISTING_TOOL_NAME_HINTS = ("list", "history", "search")
_LISTING_TOOL_DOMAIN_HINTS = ("job", "task", "artifact", "run", "pipeline")
_LISTING_PAYLOAD_KEYS = ("jobs", "items", "results", "records", "artifacts", "rows", "runs")


@dataclass(frozen=True)
class Scenario:
    """One case13 grind scenario (see ``scenarios.md``)."""

    label: str
    prompt_file: str
    kind: str  # capability | instrumentation | visualization | honest_negative


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("s1_capability", "prompt.txt", "capability"),
    Scenario("s2_instrumentation", "prompt_s2.txt", "instrumentation"),
    Scenario("s3_visualization", "prompt_s3.txt", "visualization"),
    Scenario("s4_honest_negative", "prompt_s4.txt", "honest_negative"),
)


def _load_prompt(filename: str) -> str:
    return Path(CASE_DIR, filename).read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# Structural scanners (used by both evidence-gathering and matchers)
# --------------------------------------------------------------------------- #
def _walk(value: Any):
    """Yield every dict found anywhere inside a JSON-like structure."""
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


def _extract_job_ids(run) -> list[str]:
    """Every distinct ``job_id`` surfaced anywhere in the trace's tool calls.

    Reads the JarvisJobs task/job-handle envelope
    (``clio_agent.tools.jarvis_jobs``: ``task_id`` + ``job_id`` + ``kind`` +
    ``state`` + ``terminal``) out of tool call args AND output -- never prose.
    Order-preserved, deduplicated.
    """
    seen: list[str] = []
    for call in run.tool_calls:
        for blob in (call.args, call.output):
            for node in _walk(blob):
                job_id = node.get("job_id")
                if isinstance(job_id, str) and job_id and job_id not in seen:
                    seen.append(job_id)
    return seen


def _has_task_envelope(value: Any) -> bool:
    """Whether a JSON-like blob contains a real SEP-2663 task/job-handle envelope."""
    for node in _walk(value):
        if "task_id" not in node or "job_id" not in node:
            continue
        if not node.get("task_id") or not node.get("job_id"):
            continue
        if "state" in node or "terminal" in node:
            return True
    return False


def _iter_strings(value: Any):
    """Yield every string scalar found anywhere inside a JSON-like structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _contains_template_literal(value: Any) -> bool:
    """Whether a leaked ``{{...}}`` prompt-template placeholder appears anywhere
    (dict values, list items, or a bare string), at any nesting depth."""
    return any(_TEMPLATE_LITERAL_RE.search(s) for s in _iter_strings(value))


def _is_none_shaped_workflow_state(args: Any) -> bool:
    """Whether a tool call's args force-reset ``workflow_state`` to bare ``None``."""
    if not isinstance(args, dict):
        return False
    for key, value in args.items():
        if "workflow_state" in str(key) and value is None:
            return True
    return False


def _measurements_in_text(text: str) -> list[float]:
    out: list[float] = []
    for token in _MEASUREMENT_RE.findall(text or ""):
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def _grounded(
    value: float, corpus: list[float], *, rel_tol: float = 1e-4, abs_tol: float = 1e-6
) -> bool:
    return any(math.isclose(value, c, rel_tol=rel_tol, abs_tol=abs_tol) for c in corpus)


def _tool_call_failed(call) -> bool:
    if call.error:
        return True
    out = call.output
    if isinstance(out, dict):
        if out.get("ok") is False:
            return True
        if out.get("is_error") is True:
            return True
        if str(out.get("status") or "").lower() in {"failed", "error"}:
            return True
    return False


def _looks_like_listing_call(call) -> bool:
    """Heuristic: a tool call whose NAME suggests it enumerates prior work.

    Test-harness heuristic (not core code -- CLAUDE.md's no-keyword-matching
    rule binds ``src/``, not the matcher layer, which precedent (wildfire's
    ``call.name == "geo_render_feature_map"``) already does by exact name).
    The cluster-operator pack is not installed yet (GOAL Status: "prerequisites
    in flight"), so the exact discovery tool name is unknown -- tighten this to
    an exact name once the pack lands and r1 shows what it actually calls.
    """
    name = call.name.lower()
    return any(h in name for h in _LISTING_TOOL_NAME_HINTS) and any(
        h in name for h in _LISTING_TOOL_DOMAIN_HINTS
    )


def _listing_rows(output: Any) -> list[dict[str, Any]]:
    if isinstance(output, list):
        return [row for row in output if isinstance(row, dict)]
    if isinstance(output, dict):
        for key in _LISTING_PAYLOAD_KEYS:
            value = output.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


# --------------------------------------------------------------------------- #
# Door-side re-query (subprocess the clio-relay CLI; imports nothing from
# relay source -- GOAL's non-negotiable: "the domain tools are NOT a new MCP").
# --------------------------------------------------------------------------- #
def relay_job_status(job_id: str, *, timeout_s: float = 60.0) -> dict[str, Any]:
    """Re-query the relay door for one job's REAL terminal status.

    Independent of anything the agent's own trace/answer claimed: shells out to
    the ``clio-relay job status`` CLI, which reads the job's durable record over
    SSH from the cluster's own queue (the door-side truth; see
    ``clio_relay.relay_ops.job_status`` / ``clio_relay.models.JobState`` --
    read for reference only, never imported here). Never raises: a missing job,
    an RPC error, or a timeout all come back as ``{"ok": False, ...}`` so one
    bad job id cannot crash the evidence-gathering pass.
    """
    if not job_id:
        return {"ok": False, "error": "empty job_id"}
    try:
        proc = subprocess.run(
            [RELAY_EXE, "job", "status", job_id, "--cluster", RELAY_CLUSTER],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=dict(os.environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"subprocess failed: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "").strip()[-2000:]}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"non-JSON relay output: {exc}"}
    job = payload.get("job") if isinstance(payload, dict) else None
    if not isinstance(job, dict):
        return {"ok": False, "error": "relay response missing 'job'"}
    state = str(job.get("state") or "")
    return {
        "ok": True,
        "job_id": str(job.get("job_id") or job_id),
        "state": state,
        "terminal": state in TERMINAL_JOB_STATES,
        "succeeded": state in SUCCESS_JOB_STATES,
        "raw": job,
    }


# --------------------------------------------------------------------------- #
# GACT-side re-fetch (artifact lineage + session metadata) -- HTTP the owned
# gact server directly; ``clio_sut.ClioAgent`` does not carry these columns.
# --------------------------------------------------------------------------- #
def _fetch_session_metadata(base_url: str, session_id: str) -> dict[str, Any]:
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as http:
            resp = http.get(f"/v1/sessions/{session_id}")
        if resp.status_code != 200:
            return {}
        return dict(resp.json().get("metadata") or {})
    except httpx.HTTPError:
        return {}


def _fetch_artifact_records(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Full artifact-registry rows (path/kind/sha256/custody), bounded pagination.

    Mirrors ``clio_sut.ClioAgent._registry_artifacts`` (S7 #973's designation
    truth) but keeps the ``sha256``/``custody`` lineage columns the shared SUT
    discards -- case13's S3 matcher needs lineage, not just a bare path list.
    """
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as http:
            for _ in range(50):
                params: dict[str, Any] = {"include_children": True, "limit": 200}
                if cursor:
                    params["before"] = cursor
                resp = http.get(f"/v1/sessions/{session_id}/artifacts", params=params)
                if resp.status_code != 200:
                    break
                body = resp.json()
                for record in body.get("artifacts") or []:
                    for version in record.get("versions") or []:
                        items.append(
                            {
                                "artifact_id": str(version.get("artifact_id") or ""),
                                "name": str(record.get("name") or ""),
                                "path": str(version.get("path") or ""),
                                "kind": str(version.get("kind") or ""),
                                "sha256": version.get("sha256"),
                                "custody": str(version.get("custody") or ""),
                            }
                        )
                cursor = body.get("next_cursor")
                if not cursor:
                    break
    except httpx.HTTPError:
        return items
    return items


def _augment_with_case13_evidence(run, gact_server, run_spec: dict[str, Any]) -> None:
    """Populate the case13-specific evidence seams onto ``run.extra`` in place.

    ``Run`` is a frozen dataclass, but ``extra`` is a plain mutable dict (the
    documented escape hatch -- ``agent_test.run.Run`` docstring), so mutating
    its contents here is legal and is how every matcher below receives evidence
    the shared ``clio_sut.ClioAgent`` does not gather (door-side re-query,
    artifact lineage, session task-record metadata, serve log text). Kept
    entirely in the TEST file so ``clio_sut.py`` -- shared with
    earthscope/wildfire -- is never touched (additive, no regression risk).
    """
    run.extra["run_spec"] = dict(run_spec)
    session_id = str(run.extra.get("session_id") or "")
    run.extra["session_metadata"] = (
        _fetch_session_metadata(gact_server.url, session_id) if session_id else {}
    )
    run.extra["artifact_records"] = (
        _fetch_artifact_records(gact_server.url, session_id) if session_id else []
    )
    job_ids = _extract_job_ids(run)
    run.extra["door_job_status"] = {jid: relay_job_status(jid) for jid in job_ids}
    log_path = gact_server.server_log
    run.extra["serve_log_text"] = (
        log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    )


def degradation_verdict(run) -> list[str]:
    """The typed task-record degradation reasons observed this run (not a
    pass/fail predicate -- the run-verdict inventory GOAL asks be kept "for the
    security-phase re-run": every degradation must be typed and RECORDED, even
    the tolerated #1223 one)."""
    tasks = (run.extra.get("session_metadata") or {}).get("mcp_tasks") or {}
    return sorted(
        {
            str(record.get("holding_reason"))
            for record in tasks.values()
            if isinstance(record, dict) and record.get("holding_reason")
        }
    )


# --------------------------------------------------------------------------- #
# Matchers (structured evidence only -- GOAL.md's matcher plan, letters a-f)
# --------------------------------------------------------------------------- #
@matcher
def door_confirmed_terminal_success(run) -> bool:
    """(a) door-side: every job_id the trace surfaced reached a REAL terminal
    `succeeded` state per an INDEPENDENT relay-door re-query -- never the
    agent's own claim. Fails on: no jobs found, a re-query error, a
    non-succeeded terminal state, or a non-terminal state (the agent must not
    report success before the door confirms terminal)."""
    statuses = run.extra.get("door_job_status") or {}
    if not statuses:
        return False
    return all(
        isinstance(row, dict) and row.get("ok") and row.get("succeeded") is True
        for row in statuses.values()
    )


@matcher
def zero_task_declaration_suppression(run) -> bool:
    """(b.i) v2-or-fail (GOAL non-negotiable): zero occurrences of
    ``mcp_tasks_declaration_suppressed`` in the serve log for this session."""
    return "mcp_tasks_declaration_suppressed" not in (run.extra.get("serve_log_text") or "")


@matcher
def task_envelope_present(run) -> bool:
    """(b.ii) v2 discipline: a real SEP-2663 task/job-handle envelope
    (``task_id`` + ``job_id`` + ``state``/``terminal`` -- the JarvisJobs
    contract) appeared in the trace's tool calls, proving the answer rode the
    v2 async task flow rather than a v1-synchronous shortcut."""
    for call in run.tool_calls:
        if _has_task_envelope(call.args) or _has_task_envelope(call.output):
            return True
    return False


@matcher
def durable_task_record_or_typed_degradation(run) -> bool:
    """(b.iii) the v2 task record persisted durably in session metadata
    (``mcp_tasks``), OR degraded to the SINGLE typed, tolerated reason
    ``mcp_task_record_held_locally`` (#1223: relay-federation tasks are not yet
    session-bound; loud+typed is correct per doctrine). Any OTHER typed
    degradation (session deleted, lease held, not durable) fails -- those are
    not the tolerated #1223 case and must not be silently waved through."""
    tasks = (run.extra.get("session_metadata") or {}).get("mcp_tasks") or {}
    if not tasks:
        return False
    for record in tasks.values():
        if not isinstance(record, dict):
            return False
        reason = record.get("holding_reason")
        if reason not in (None, "mcp_task_record_held_locally"):
            return False
    return True


@matcher
def visualization_frames_with_lineage(run) -> bool:
    """(c) S3: at least 3 distinct per-frame images arrived as workspace
    artifacts, each carrying registry lineage (a non-empty ``sha256`` --
    the registry-sourced designation, never a bare path scrape)."""
    frames = [
        row
        for row in run.extra.get("artifact_records") or []
        if Path(str(row.get("path") or "")).suffix.lower() in _FRAME_IMAGE_SUFFIXES
    ]
    if len(frames) < 3:
        return False
    return all(row.get("sha256") for row in frames)


@matcher
def answer_numbers_grounded_in_artifact(run) -> bool:
    """(d) answer grounding: every numeric measurement the final message
    reports (S1: temperature/energy/pressure; S2: I/O byte counts) actually
    appears -- re-extracted by THIS matcher, never trusted from prose -- in the
    bytes of a real output artifact. A run reporting no measurements, or any
    measurement absent from every artifact, fails (invented/unfounded numbers)."""
    reported = _measurements_in_text(run.output)
    if not reported:
        return False
    paths = [
        row.get("path")
        for row in run.extra.get("artifact_records") or []
        if row.get("path") and Path(str(row.get("path"))).is_file()
    ]
    if not paths:
        return False
    corpus: list[float] = []
    for path in paths:
        try:
            corpus.extend(
                _measurements_in_text(Path(str(path)).read_text(encoding="utf-8", errors="ignore"))
            )
        except OSError:
            continue
    if not corpus:
        return False
    return all(_grounded(value, corpus) for value in reported)


@matcher
def honest_about_tool_failures(run) -> bool:
    """(e.i) any terminal tool failure the trace shows (a ``ToolCall`` with a
    truthy ``.error``, or an output carrying ``ok: false`` / ``is_error: true``
    / a failed ``status``) must be acknowledged in the final visible answer --
    no success claim over a failure the trace records. A run with no failures
    trivially passes (nothing to acknowledge)."""
    failures = [c for c in run.tool_calls if _tool_call_failed(c)]
    if not failures:
        return True
    lowered = (run.output or "").lower()
    return any(term in lowered for term in _FAILURE_ACK_TERMS)


@matcher
def s4_answer_agrees_with_real_listing(run) -> bool:
    """(e.ii) S4 (honest negative): the final answer's claim must agree with
    the REAL discovery-tool evidence the trace itself produced (re-extracted
    here, never prose-matched): empty listing -> the answer must read as a
    genuine "nothing found"; non-empty -> the answer must name at least one
    real id from that evidence. A run with NO discovery tool call at all fails
    -- S4 cannot be answered honestly without checking the cluster."""
    listing_calls = [c for c in run.tool_calls if _looks_like_listing_call(c)]
    if not listing_calls:
        return False
    ids: set[str] = set()
    any_nonempty = False
    for call in listing_calls:
        rows = _listing_rows(call.output)
        if rows:
            any_nonempty = True
        for row in rows:
            for key in ("job_id", "id", "task_id", "artifact_id", "name"):
                value = row.get(key)
                if value:
                    ids.add(str(value))
    lowered = (run.output or "").lower()
    if not any_nonempty:
        return any(term in lowered for term in _NOTHING_FOUND_TERMS)
    return any(i.lower() in lowered for i in ids if i)


@matcher
def run_spec_is_not_force_fed(run) -> bool:
    """(f) anti-force-feeding: the harness's own request to the SUT
    (``run.extra['run_spec']``, recorded verbatim by the test body before
    dispatch) carries only the documented minimal contract (``task`` +
    ``blueprint_id``) plus recognized harness plumbing -- never a pre-filled
    ``workflow_state``, an un-rendered ``{{...}}`` prompt template, or any
    other synthetic agent-facing field the GOAL forbids the harness from
    injecting."""
    spec = run.extra.get("run_spec") or {}
    if not spec:
        return False
    if set(spec) - ALLOWED_RUN_SPEC_KEYS:
        return False
    task = spec.get("task")
    if not isinstance(task, str) or not task.strip():
        return False
    if spec.get("blueprint_id") != BLUEPRINT_ID:
        return False
    if _contains_template_literal(task):
        return False
    return True


@matcher
def no_force_fed_tool_args(run) -> bool:
    """(f) anti-force-feeding (trace side): no tool call arg/output contains an
    un-interpolated ``{{...}}`` template literal (a leaked prompt-template
    placeholder), and no tool call force-resets ``workflow_state`` to bare
    ``None`` mid-run -- both are harness/blueprint bugs that inject synthetic
    state instead of letting the agent's own reasoning produce it."""
    for call in run.tool_calls:
        if _contains_template_literal(call.args) or _contains_template_literal(call.output):
            return False
        if _is_none_shaped_workflow_state(call.args):
            return False
    return True


# --------------------------------------------------------------------------- #
# The test
# --------------------------------------------------------------------------- #
@pytest.mark.real_case
@pytest.mark.live
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.label for s in SCENARIOS])
def test_case13_cluster_operator(agent, gact_server, scenario: Scenario, tmp_path):
    prompt = _load_prompt(scenario.prompt_file)

    # Durable, worktree-local workspace (GOAL isolation: "artifacts root under
    # the worktree", "work in the session workspace" -- never a stray/repo path,
    # and never auto-cleaned pytest tmp_path so a grinder can inspect it after).
    workspace_root = Path(
        os.environ.get(
            "CLIO_CASE13_WORKSPACE_ROOT",
            str(Path(CASE_DIR).resolve() / "runs" / "workspace"),
        )
    )
    workdir = workspace_root / scenario.label
    workdir.mkdir(parents=True, exist_ok=True)

    run_spec = {
        "task": prompt,
        "blueprint_id": BLUEPRINT_ID,
        "case_dir": CASE_DIR,
        "run_label": scenario.label,
        "workdir": str(workdir),
        "timeout_s": TIMEOUT_S,
    }
    run = agent.run(run_spec)
    _augment_with_case13_evidence(run, gact_server, run_spec)

    # Runtime/harness invariants (same shape as earthscope/wildfire).
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    # (f) anti-force-feeding -- always required, every scenario.
    assert run_spec_is_not_force_fed(run), run.extra.get("run_spec")
    assert no_force_fed_tool_args(run), run.tool_names

    # (b.i) v2-or-fail -- hard gate, every scenario, no exceptions.
    assert zero_task_declaration_suppression(run), "mcp_tasks_declaration_suppressed observed"

    # (e.i) honesty -- always required: no success claimed over a real failure.
    assert honest_about_tool_failures(run), [c.name for c in run.tool_calls if _tool_call_failed(c)]

    if scenario.kind == "honest_negative":
        # S4: no new job is necessarily submitted -- the v2/door-side job
        # matchers do not apply. What DOES apply: the answer must agree with
        # the trace's own real discovery evidence.
        assert s4_answer_agrees_with_real_listing(run), run.output
        return

    # S1/S2/S3 all submit and drive a real cluster job through the v2 task flow.
    assert task_envelope_present(run), run.tool_names
    assert durable_task_record_or_typed_degradation(run), run.extra.get("session_metadata")
    assert door_confirmed_terminal_success(run), run.extra.get("door_job_status")
    # Inventory kept for the security-phase re-run (GOAL done-criterion 7):
    # any typed degradation this run hit is on the record even though it did
    # not fail the run (the tolerated #1223 case).
    run.extra["degradation_verdict"] = degradation_verdict(run)

    if scenario.kind in ("capability", "instrumentation"):
        # S1: temperature/energy/pressure. S2: I/O byte counts.
        assert answer_numbers_grounded_in_artifact(run), (
            run.output,
            run.extra.get("artifact_records"),
        )

    if scenario.kind == "visualization":
        assert visualization_frames_with_lineage(run), run.extra.get("artifact_records")
