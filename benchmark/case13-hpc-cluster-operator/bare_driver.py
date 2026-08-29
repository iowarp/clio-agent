"""Bare (non-pytest) driver for case13 live cells (clio-agent#1258 workaround).

WHY THIS EXISTS: on this Windows box, launching the gact server through
pytest's own process (`tests/test_real_cases/conftest.py`'s `gact_server`
fixture -- bare `uv run clio-agent-gact ...`) crashes clio-core's CTE
runtime daemon 6-for-6 times (`clio-core runtime daemon crashed:
exit=3221226505 / 0xC0000409`, preceded by `WSAEventSelect ADD failed: 203`
in stderr), which corrupts session state and 404s the very next HTTP call
(`POST /v1/sessions/{id}/agent-blueprint`) -- before any relay call is ever
made. The SAME server + SAME SUT flow launched OUTSIDE pytest (this script)
has shown ZERO crashes across 7+ live attempts. Root cause not found (see
GRIND-HANDOFF.md trap list); this script is the accepted workaround until
someone fixes the pytest-presence trigger. Track: clio-agent#1258.

WHAT IT DOES: starts its own gact server (mirroring conftest.py's env
recipe exactly), drives ONE case13 scenario through
`tests/test_real_cases/clio_sut.py::ClioAgent` (the SAME SUT class pytest
uses), then imports and runs the REAL matcher functions from
`tests/test_real_cases/test_case13_cluster_operator.py` against the result
-- so a pass/fail here is the SAME verdict pytest would produce, no
re-implementation, no drift.

USAGE (from a shell that has already sourced deployment-env.sh -- see
GRIND-HANDOFF.md section 1 for the full env recipe):

    cd D:/Libraries/Documents/projects/clio_develop_workspace/case13-gate
    uv run --no-sync python benchmark/case13-hpc-cluster-operator/bare_driver.py s1_capability
    uv run --no-sync python benchmark/case13-hpc-cluster-operator/bare_driver.py s2_instrumentation
    uv run --no-sync python benchmark/case13-hpc-cluster-operator/bare_driver.py s3_visualization
    uv run --no-sync python benchmark/case13-hpc-cluster-operator/bare_driver.py s4_honest_negative

Or let the wrapper shell script manage the gact server subprocess for you
(recommended -- see run_bare_driver.sh in this same directory).

RESULTS LAND: `D:\\Libraries\\Documents\\projects\\clio-runs\\case13-v170\\<scenario>_barepy\\bare_run_result.json`
(the full agent_test Run, matcher verdicts, and evidence -- durable, never
auto-cleaned).
"""
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
CASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKTREE / "tests" / "test_real_cases"))
sys.path.insert(0, str(WORKTREE))

PORT = os.environ.get("CLIO_BARE_DRIVER_PORT", "17986")
GACT_URL = f"http://127.0.0.1:{PORT}"
os.environ["CLIO_GACT_URL"] = GACT_URL

import clio_sut  # noqa: E402

clio_sut.DEFAULT_BASE_URL = GACT_URL

# Import the REAL test module directly (no pytest collection/execution --
# just importing the file; its @pytest.mark decorators are inert metadata
# when the module is imported this way, not run).
import test_case13_cluster_operator as case13  # noqa: E402


@dataclass
class _GactServerStub:
    """Minimal stand-in for conftest.py's GactServer dataclass -- only the
    two attributes `_augment_with_case13_evidence` actually reads."""

    url: str
    server_log: Path


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {s.label for s in case13.SCENARIOS}:
        labels = ", ".join(s.label for s in case13.SCENARIOS)
        print(f"usage: bare_driver.py <scenario>  (one of: {labels})")
        return 2
    label = sys.argv[1]
    scenario = next(s for s in case13.SCENARIOS if s.label == label)
    prompt = case13._load_prompt(scenario.prompt_file)  # noqa: SLF001

    workspace_root = Path(
        os.environ.get(
            "CLIO_CASE13_WORKSPACE_ROOT",
            str(Path(case13.CASE_DIR).resolve() / "runs" / "workspace"),
        )
    )
    workdir = workspace_root / f"{label}_barepy"
    workdir.mkdir(parents=True, exist_ok=True)

    run_spec = {
        "task": prompt,
        "blueprint_id": case13.BLUEPRINT_ID,
        "case_dir": str(CASE_DIR),
        "run_label": f"{label}_barepy",
        "workdir": str(workdir),
        "timeout_s": case13.TIMEOUT_S,
    }

    print(f"[{time.strftime('%H:%M:%S')}] binding provider claude_code/sonnet...")
    agent = clio_sut.ClioAgent()
    try:
        agent.bind("claude_code", "sonnet")
    except Exception as exc:  # noqa: BLE001 - want the response body, not just the exception
        resp = getattr(exc, "response", None)
        if resp is not None:
            print(f"BIND FAILED status={resp.status_code} body={resp.text}")
        else:
            print(f"BIND FAILED: {type(exc).__name__}: {exc}")
        raise
    print(f"[{time.strftime('%H:%M:%S')}] provider bound, invoking run ({label})...")

    run = agent.run(run_spec)
    print(f"[{time.strftime('%H:%M:%S')}] run completed.")

    # Same evidence-gathering the pytest test body does: door-side job
    # re-query, session metadata (mcp_tasks), artifact registry, serve log.
    gact_server = _GactServerStub(url=GACT_URL, server_log=Path(r"C:\Users\jaime\AppData\Local\Temp\bare_driver_server.log"))
    case13._augment_with_case13_evidence(run, gact_server, run_spec)  # noqa: SLF001

    print("session_id:", run.extra.get("session_id"))
    print("blueprint_activated:", run.extra.get("blueprint_activated"))
    print("error:", run.error)
    print("stop_reason:", run.extra.get("stop_reason"))
    print("tool_names:", [c.name for c in run.tool_calls])
    print("job_ids:", case13._extract_job_ids(run))  # noqa: SLF001
    print("door_job_status:", run.extra.get("door_job_status"))
    print()
    print("output (first 3000 chars):")
    print((run.output or "")[:3000])
    print()

    # Run the REAL matchers -- same functions pytest asserts on, so this
    # verdict is not a re-implementation.
    always_on = {
        "run_spec_is_not_force_fed": case13.run_spec_is_not_force_fed,
        "no_force_fed_tool_args": case13.no_force_fed_tool_args,
        "zero_task_declaration_suppression": case13.zero_task_declaration_suppression,
        "honest_about_tool_failures": case13.honest_about_tool_failures,
    }
    if scenario.kind == "honest_negative":
        checks = {**always_on, "s4_answer_agrees_with_real_listing": case13.s4_answer_agrees_with_real_listing}
    else:
        checks = {
            **always_on,
            "task_envelope_present": case13.task_envelope_present,
            "durable_task_record_or_typed_degradation": case13.durable_task_record_or_typed_degradation,
            "door_confirmed_terminal_success": case13.door_confirmed_terminal_success,
        }
        if scenario.kind in ("capability", "instrumentation"):
            checks["answer_numbers_grounded_in_artifact"] = case13.answer_numbers_grounded_in_artifact
        if scenario.kind == "visualization":
            checks["visualization_frames_with_lineage"] = case13.visualization_frames_with_lineage

    print("=" * 70)
    print(f"MATCHER VERDICTS ({label}):")
    all_pass = True
    verdicts: dict[str, object] = {}
    for name, fn in checks.items():
        try:
            ok = bool(fn(run))
            verdicts[name] = ok
        except Exception as exc:  # noqa: BLE001 - a matcher exception is itself a verdict
            ok = False
            verdicts[name] = f"EXCEPTION {type(exc).__name__}: {exc}"
            print(f"  {name}: EXCEPTION {type(exc).__name__}: {exc}")
            all_pass = False
            continue
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
        all_pass = all_pass and ok
    print("=" * 70)
    print("OVERALL:", "PASS" if all_pass else "FAIL")

    out_path = workdir / "bare_run_result.json"
    out_path.write_text(
        json.dumps(
            {"run": run.to_dict(), "matchers": verdicts, "overall_pass": all_pass},
            default=str,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"full result written to {out_path}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
