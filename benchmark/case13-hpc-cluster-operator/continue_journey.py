"""Continue an existing journey session with a follow-up human-level message.

Why this exists (diagnosed live, darshan_journey 2026-08-28): the react loop ends
a turn irreversibly when one LM response omits its ``[[ ## tool_calls ## ]]``
block (``empty_tool_calls`` break -> forced submit), even when the model's own
next responses keep trying to call tools. The session itself is durable and
conversational, so the honest journey-level recovery is a plain follow-up
message on the SAME session ("go ahead and run it") — exactly what a human
scientist would send after the operator's honest "discovery done, run not yet
submitted" status report. No tool args are fed; the agent still decides.

USAGE (server already up — see continue_journey.sh):

    uv run --no-sync python continue_journey.py <journey> <session_id> <prompt_file>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(CASE_DIR))

import journey_driver as jd  # noqa: E402  (sets CLIO_GACT_URL, imports clio_sut/case13)

clio_sut = jd.clio_sut
case13 = jd.case13
httpx = jd.httpx


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[1] not in jd.JOURNEYS:
        print("usage: continue_journey.py <journey> <session_id> <prompt_file>")
        return 2
    label, session_id, prompt_file = sys.argv[1], sys.argv[2], sys.argv[3]
    journey = jd.JOURNEYS[label]
    prompt = Path(prompt_file).read_text(encoding="utf-8").strip()

    workspace_root = Path(
        os.environ.get(
            "CLIO_CASE13_WORKSPACE_ROOT",
            str(Path(case13.CASE_DIR).resolve() / "runs" / "workspace"),
        )
    )
    workdir = workspace_root / journey.get("workdir_name", f"{label}_barepy")
    download_dir = workdir / journey["download_dir"]
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{time.strftime('%H:%M:%S')}] binding provider claude_code/sonnet...")
    agent = clio_sut.ClioAgent()
    agent.bind("claude_code", "sonnet")

    with httpx.Client(base_url=jd.GACT_URL, timeout=200.0) as http:
        info = http.get(f"/v1/sessions/{session_id}")
        info.raise_for_status()
        active = http.get(f"/v1/sessions/{session_id}/agent-blueprint").json()
        print(
            f"session {session_id} found; active blueprint:",
            active.get("active_agent_blueprint_id"),
        )
        print(f"[{time.strftime('%H:%M:%S')}] posting continuation turn ({label})...")
        assistant = agent._post_turn(http, session_id, prompt, case13.TIMEOUT_S)  # noqa: SLF001
        print(f"[{time.strftime('%H:%M:%S')}] turn completed.")
        messages = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
        children = [
            r
            for r in http.get("/v1/sessions").json()["sessions"]
            if r.get("parent_session_id") == session_id
        ]
        active = http.get(f"/v1/sessions/{session_id}/agent-blueprint").json()
        run_artifacts = agent._existing_paths(  # noqa: SLF001
            agent._registry_artifacts(http, session_id)  # noqa: SLF001
        )

    run = agent._to_run(  # noqa: SLF001
        assistant,
        messages,
        children,
        active,
        session_id,
        case13.BLUEPRINT_ID,
        None,
        artifacts=run_artifacts,
    )

    run_spec = {
        "task": prompt,
        "blueprint_id": case13.BLUEPRINT_ID,
        "case_dir": str(CASE_DIR),
        "run_label": f"{label}_continue",
        "workdir": str(workdir),
        "timeout_s": case13.TIMEOUT_S,
    }
    gact_server = jd._GactServerStub(
        url=jd.GACT_URL,
        server_log=Path(r"C:\Users\jaime\AppData\Local\Temp\bare_driver_server.log"),
    )
    case13._augment_with_case13_evidence(run, gact_server, run_spec)  # noqa: SLF001

    print("error:", run.error)
    print("stop_reason:", run.extra.get("stop_reason"))
    print("tool_names:", [c.name for c in run.tool_calls])
    job_ids = case13._extract_job_ids(run)  # noqa: SLF001
    print("job_ids (this turn):", job_ids)
    print("door_job_status:", {k: {kk: v.get(kk) for kk in ("ok", "state", "succeeded")} for k, v in (run.extra.get("door_job_status") or {}).items()})
    print()
    print("output (first 4000 chars):")
    print((run.output or "")[:4000])
    print()

    checks = {
        "no_force_fed_tool_args": case13.no_force_fed_tool_args,
        "zero_task_declaration_suppression": case13.zero_task_declaration_suppression,
        "honest_about_tool_failures": case13.honest_about_tool_failures,
        "task_envelope_present": case13.task_envelope_present,
        "durable_task_record_or_typed_degradation": case13.durable_task_record_or_typed_degradation,
        "door_confirmed_terminal_success": case13.door_confirmed_terminal_success,
    }
    if journey["kind"] == "instrumentation":
        checks["answer_numbers_grounded_in_artifact"] = case13.answer_numbers_grounded_in_artifact
    if journey["kind"] == "visualization":
        checks["visualization_frames_with_lineage"] = case13.visualization_frames_with_lineage

    print("=" * 70)
    print(f"MATCHER VERDICTS ({label} continuation):")
    all_pass = True
    verdicts: dict[str, object] = {}
    for name, fn in checks.items():
        try:
            ok = bool(fn(run))
            verdicts[name] = ok
        except Exception as exc:  # noqa: BLE001
            ok = False
            verdicts[name] = f"EXCEPTION {type(exc).__name__}: {exc}"
            print(f"  {name}: EXCEPTION {type(exc).__name__}: {exc}")
            all_pass = False
            continue
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
        all_pass = all_pass and ok
    print("=" * 70)
    print("MATCHERS OVERALL:", "PASS" if all_pass else "FAIL")

    print()
    print(f"[{time.strftime('%H:%M:%S')}] downloading cluster-side artifacts locally...")
    cluster_listings: dict[str, object] = {}
    downloads: list[dict[str, object]] = []
    for job_id in job_ids:
        listing = jd._list_job_artifacts(job_id)  # noqa: SLF001
        cluster_listings[job_id] = listing
        if not listing.get("ok"):
            print(f"  list-artifacts {job_id}: ERROR {listing.get('error')}")
            continue
        rows = listing["artifacts"]
        print(f"  job {job_id}: {len(rows)} artifact(s)")
        for row in rows:
            result = jd._download_artifact(row, download_dir)  # noqa: SLF001
            downloads.append(result)
            if result.get("ok"):
                print(
                    f"    downloaded {result['name']} -> {result['local_path']} "
                    f"({result['size_bytes']} bytes, sha256_match={result['sha256_match']})"
                )
            else:
                print(f"    FAILED {result.get('artifact_id')}: {result.get('error')}")

    trace_saved = jd._persist_trace_reads(run, download_dir)  # noqa: SLF001
    for item in trace_saved:
        print(
            f"  trace-fetched payload persisted: {item['name']} -> {item['local_path']} "
            f"({item['size_bytes']} bytes)"
        )

    out_path = workdir / f"journey_result.continue.{time.strftime('%H%M%S')}.json"
    out_path.write_text(
        json.dumps(
            {
                "journey": label,
                "continuation": True,
                "session_id": session_id,
                "run": run.to_dict(),
                "matchers": verdicts,
                "matchers_overall_pass": all_pass,
                "cluster_listings": cluster_listings,
                "downloads": downloads,
                "trace_persisted_reads": trace_saved,
            },
            default=str,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"full result written to {out_path}")
    ok_downloads = [d for d in downloads if d.get("ok")]
    print(f"local downloads: {len(ok_downloads)} ok / {len(downloads)} attempted")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
