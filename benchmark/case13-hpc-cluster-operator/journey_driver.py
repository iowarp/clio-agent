"""Journey driver for the v1.7.0 FINAL science deliverables (owner acceptance).

Two journeys, each an end-to-end story the owner accepts on evidence:

* ``darshan_journey``  -- LAMMPS runs on ares WITH Darshan bound to it; the
  darshan log (and a darshan-parser text summary) are tracked as cluster-side
  artifacts; BOTH are downloaded to this local machine; the parsed text is
  then analyzed locally (bytes read/written, top files, module counts).
* ``paraview_images``  -- LAMMPS produces trajectory dumps; pvbatch renders
  per-frame PNGs; the PNGs are cluster-side artifacts and are bulk-downloaded
  locally (real PNG files with plausible sizes).

Built on the bare_driver.py pattern (clio-agent#1258: NO pytest on this box).
The AGENT decides and drives from the plain science prompt; this driver only:
 1. pre-creates the run workspace + installs the cluster-operator blueprint
    WORKSPACE-SCOPED from the marketplace checkout (harness setup, not agent
    input -- the run_spec stays the documented minimal contract),
 2. drives one turn through the real ``clio_sut.ClioAgent`` SUT,
 3. runs the REAL case13 matcher functions (advisory verdicts),
 4. downloads the run's cluster-side artifacts locally through the relay
    door CLI (``clio-relay job list-artifacts`` / ``job read-artifact``,
    base64 decoded + sha256-verified) and ALSO persists any base64 payloads
    the agent itself fetched via relay_read_artifact out of the trace.

Nothing here feeds the agent tool args or fabricates results: downloads are
evidence persistence of data that really exists on the cluster / in the trace.

USAGE (env recipe already sourced -- see run_journey.sh):

    uv run --no-sync python benchmark/case13-hpc-cluster-operator/journey_driver.py darshan_journey
    uv run --no-sync python benchmark/case13-hpc-cluster-operator/journey_driver.py paraview_images

RESULTS: ``<workspace_root>/<journey>_barepy/journey_result.json`` plus the
downloaded files under ``<workspace_root>/<journey>_barepy/<darshan|frames>/``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKTREE = Path(__file__).resolve().parents[2]
CASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKTREE / "tests" / "test_real_cases"))
sys.path.insert(0, str(WORKTREE))

PORT = os.environ.get("CLIO_BARE_DRIVER_PORT", "17986")
GACT_URL = f"http://127.0.0.1:{PORT}"
os.environ["CLIO_GACT_URL"] = GACT_URL

import httpx  # noqa: E402

import clio_sut  # noqa: E402

clio_sut.DEFAULT_BASE_URL = GACT_URL

import test_case13_cluster_operator as case13  # noqa: E402

MARKETPLACE_ROOT = os.environ.get(
    "CLIO_JOURNEY_MARKETPLACE",
    r"D:\Libraries\Documents\projects\clio-agent-marketplace-hdf5",
)

JOURNEYS: dict[str, dict[str, str]] = {
    "darshan_journey": {
        "prompt_file": "prompt_darshan_journey.txt",
        "kind": "instrumentation",
        "download_dir": "darshan",
    },
    "paraview_images": {
        "prompt_file": "prompt_paraview_images.txt",
        "kind": "visualization",
        "download_dir": "frames",
    },
    # Fresh finishing session for the paraview journey after the CTE per-blob
    # defect bricked sess_60c838ad7974 mid-journey (2026-08-28): a truthful
    # shift-handoff prompt carrying the prior session's own recorded lessons;
    # the pipeline and trajectory are durable cluster-side.
    "paraview_finish": {
        "prompt_file": "prompt_paraview_fresh.txt",
        "kind": "visualization",
        "download_dir": "frames",
    },
    # Same shape for the darshan journey's fresh finishing session.
    "darshan_finish": {
        "prompt_file": "prompt_darshan_fresh.txt",
        "kind": "instrumentation",
        "download_dir": "darshan",
    },
    # Second fresh session for the paraview journey (the CTE per-blob defect
    # bricked sess_c83fc6ba3ef5 after its huge diagnosis turn). Reuses the
    # SAME workspace dir so the new session can fs_read_file the v8 renderer
    # the previous session authored there.
    "paraview_final": {
        "prompt_file": "prompt_paraview_final.txt",
        "kind": "visualization",
        "download_dir": "frames",
        "workdir_name": "paraview_finish_barepy",
    },
}


@dataclass
class _GactServerStub:
    url: str
    server_log: Path


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return cleaned or "artifact"


def _relay_cli(args: list[str], *, timeout_s: float = 180.0) -> dict[str, Any]:
    """Shell the deployment ``clio-relay`` CLI; JSON stdout or a typed error."""
    exe = os.environ.get("CLIO_RELAY_EXE") or "clio-relay"
    cluster = os.environ.get("CLIO_RELAY_CLUSTER", "ares-p5run2")
    try:
        proc = subprocess.run(
            [exe, *args, "--cluster", cluster],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=dict(os.environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"subprocess failed: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "").strip()[-3000:]}
    try:
        return {"ok": True, "payload": json.loads(proc.stdout)}
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"non-JSON relay output: {exc}; head={proc.stdout[:400]}"}


def _list_job_artifacts(job_id: str) -> dict[str, Any]:
    """Every artifact ref for a job (paged through to exhaustion)."""
    rows: list[dict[str, Any]] = []
    cursor = 1
    for _ in range(50):
        res = _relay_cli(
            ["job", "list-artifacts", job_id, "--cursor", str(cursor), "--limit", "100"]
        )
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error"), "artifacts": rows}
        payload = res["payload"]
        page = (
            payload.get("artifacts")
            if isinstance(payload, dict)
            else payload
            if isinstance(payload, list)
            else None
        ) or []
        rows.extend(r for r in page if isinstance(r, dict))
        next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
        if not next_cursor:
            break
        cursor = int(next_cursor)
    return {"ok": True, "artifacts": rows}


def _download_artifact(row: dict[str, Any], dest_dir: Path) -> dict[str, Any]:
    """``job read-artifact`` one ref, decode base64, sha256-verify, write local."""
    artifact_id = str(row.get("artifact_id") or "")
    if not artifact_id:
        return {"ok": False, "error": "row missing artifact_id", "row": row}
    res = _relay_cli(["job", "read-artifact", artifact_id], timeout_s=300.0)
    if not res.get("ok"):
        return {"ok": False, "artifact_id": artifact_id, "error": res.get("error")}
    payload = res["payload"]
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return {
            "ok": False,
            "artifact_id": artifact_id,
            "error": f"unexpected read-artifact shape: {str(payload)[:300]}",
        }
    try:
        data = base64.b64decode(str(payload.get("data") or ""))
    except (ValueError, TypeError) as exc:
        return {"ok": False, "artifact_id": artifact_id, "error": f"base64 decode failed: {exc}"}
    meta = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    declared_sha = str(meta.get("sha256") or row.get("sha256") or "")
    actual_sha = hashlib.sha256(data).hexdigest()
    sha_match = (not declared_sha) or (declared_sha == actual_sha)
    name = str(meta.get("name") or row.get("name") or "")
    if not name:
        uri = str(meta.get("uri") or row.get("uri") or "")
        name = Path(uri.split("://", 1)[-1]).name or artifact_id
    local = dest_dir / _sanitize_filename(name)
    if local.exists():
        local = dest_dir / f"{artifact_id[:8]}_{_sanitize_filename(name)}"
    local.write_bytes(data)
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "name": name,
        "role": row.get("role") or meta.get("role"),
        "kind": row.get("kind") or meta.get("kind"),
        "job_id": meta.get("job_id") or row.get("job_id"),
        "declared_sha256": declared_sha or None,
        "actual_sha256": actual_sha,
        "sha256_match": sha_match,
        "local_path": str(local),
        "size_bytes": len(data),
    }


def _persist_trace_reads(run: Any, dest_dir: Path) -> list[dict[str, Any]]:
    """Persist base64 artifact payloads the AGENT itself fetched (relay_read_artifact).

    Pure evidence persistence: the bytes already sit in the trace's tool
    outputs; this just decodes and writes them so the report can point at
    real local files the agent's own tool call produced.
    """
    saved: list[dict[str, Any]] = []
    for call in run.tool_calls:
        if "read_artifact" not in call.name:
            continue
        for node in case13._walk(call.output):  # noqa: SLF001
            if node.get("encoding") != "base64" or not node.get("data"):
                continue
            meta = node.get("artifact") if isinstance(node.get("artifact"), dict) else {}
            artifact_id = str(meta.get("artifact_id") or "")
            try:
                data = base64.b64decode(str(node["data"]))
            except (ValueError, TypeError):
                continue
            name = str(meta.get("name") or "")
            if not name:
                uri = str(meta.get("uri") or "")
                name = Path(uri.split("://", 1)[-1]).name or artifact_id or "payload"
            local = dest_dir / f"trace_{_sanitize_filename(name)}"
            if local.exists():
                local = dest_dir / f"trace_{artifact_id[:8]}_{_sanitize_filename(name)}"
            local.write_bytes(data)
            saved.append(
                {
                    "tool": call.name,
                    "artifact_id": artifact_id or None,
                    "name": name,
                    "local_path": str(local),
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return saved


def _ensure_workspace_and_blueprint(workdir: Path) -> dict[str, Any]:
    """Pre-create the run workspace and install cluster-operator WORKSPACE-scoped.

    Harness setup (not agent input): the SUT's own ``_ensure_workspace`` will
    find and reuse this exact workspace (identity = resolved root path), so
    the session created for the run sees the workspace-scoped blueprint.
    """
    target = str(workdir.expanduser().resolve())
    with httpx.Client(base_url=GACT_URL, timeout=200.0) as http:
        workspace_id = ""
        for row in http.get("/v1/workspaces").json().get("workspaces", []):
            if str(row.get("root_path") or "") == target:
                workspace_id = str(row.get("id") or "")
                break
        if not workspace_id:
            created = http.post(
                "/v1/workspaces",
                json={
                    "name": "agent-test",
                    "root_path": target,
                    "storage_root": str(Path(target) / ".clio"),
                },
            )
            created.raise_for_status()
            workspace_id = str(created.json().get("id") or "")
        pack_source = str(Path(MARKETPLACE_ROOT) / "cluster-operator")
        resp = http.post(
            "/v1/agent-blueprints/install",
            json={"source": pack_source, "scope": "workspace", "workspace_id": workspace_id},
            timeout=180.0,
        )
        resp.raise_for_status()
        install_info = resp.json()
    return {"workspace_id": workspace_id, "install": install_info}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in JOURNEYS:
        print(f"usage: journey_driver.py <{('|'.join(JOURNEYS))}>")
        return 2
    label = sys.argv[1]
    journey = JOURNEYS[label]
    prompt = (CASE_DIR / journey["prompt_file"]).read_text(encoding="utf-8").strip()

    workspace_root = Path(
        os.environ.get(
            "CLIO_CASE13_WORKSPACE_ROOT",
            str(Path(case13.CASE_DIR).resolve() / "runs" / "workspace"),
        )
    )
    workdir = workspace_root / journey.get("workdir_name", f"{label}_barepy")
    workdir.mkdir(parents=True, exist_ok=True)
    download_dir = workdir / journey["download_dir"]
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{time.strftime('%H:%M:%S')}] ensuring workspace + workspace-scoped blueprint...")
    setup = _ensure_workspace_and_blueprint(workdir)
    print(
        "workspace_id:", setup["workspace_id"],
        "| install:", json.dumps(setup["install"], default=str)[:300],
    )

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
    except Exception as exc:  # noqa: BLE001 - want the response body
        resp = getattr(exc, "response", None)
        if resp is not None:
            print(f"BIND FAILED status={resp.status_code} body={resp.text}")
        else:
            print(f"BIND FAILED: {type(exc).__name__}: {exc}")
        raise
    print(f"[{time.strftime('%H:%M:%S')}] provider bound, invoking run ({label})...")

    run = agent.run(run_spec)
    print(f"[{time.strftime('%H:%M:%S')}] run completed.")

    gact_server = _GactServerStub(
        url=GACT_URL,
        server_log=Path(r"C:\Users\jaime\AppData\Local\Temp\bare_driver_server.log"),
    )
    case13._augment_with_case13_evidence(run, gact_server, run_spec)  # noqa: SLF001

    print("session_id:", run.extra.get("session_id"))
    print("blueprint_activated:", run.extra.get("blueprint_activated"))
    print("error:", run.error)
    print("stop_reason:", run.extra.get("stop_reason"))
    print("tool_names:", [c.name for c in run.tool_calls])
    job_ids = case13._extract_job_ids(run)  # noqa: SLF001
    print("job_ids:", job_ids)
    print("door_job_status:", run.extra.get("door_job_status"))
    print()
    print("output (first 4000 chars):")
    print((run.output or "")[:4000])
    print()

    # Advisory verdicts from the REAL case13 matcher functions.
    checks = {
        "run_spec_is_not_force_fed": case13.run_spec_is_not_force_fed,
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
    print(f"MATCHER VERDICTS ({label}):")
    all_pass = True
    verdicts: dict[str, object] = {}
    for name, fn in checks.items():
        try:
            ok = bool(fn(run))
            verdicts[name] = ok
        except Exception as exc:  # noqa: BLE001 - a matcher exception is a verdict
            ok = False
            verdicts[name] = f"EXCEPTION {type(exc).__name__}: {exc}"
            print(f"  {name}: EXCEPTION {type(exc).__name__}: {exc}")
            all_pass = False
            continue
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
        all_pass = all_pass and ok
    print("=" * 70)
    print("MATCHERS OVERALL:", "PASS" if all_pass else "FAIL")

    # ---- the journey's own deliverable: LOCAL downloads -------------------
    print()
    print(f"[{time.strftime('%H:%M:%S')}] downloading cluster-side artifacts locally...")
    cluster_listings: dict[str, Any] = {}
    downloads: list[dict[str, Any]] = []
    for job_id in job_ids:
        listing = _list_job_artifacts(job_id)
        cluster_listings[job_id] = listing
        if not listing.get("ok"):
            print(f"  list-artifacts {job_id}: ERROR {listing.get('error')}")
            continue
        rows = listing["artifacts"]
        print(f"  job {job_id}: {len(rows)} artifact(s)")
        for row in rows:
            result = _download_artifact(row, download_dir)
            downloads.append(result)
            if result.get("ok"):
                print(
                    f"    downloaded {result['name']} -> {result['local_path']} "
                    f"({result['size_bytes']} bytes, sha256_match={result['sha256_match']})"
                )
            else:
                print(f"    FAILED {result.get('artifact_id')}: {result.get('error')}")

    trace_saved = _persist_trace_reads(run, download_dir)
    for item in trace_saved:
        print(
            f"  trace-fetched payload persisted: {item['name']} -> {item['local_path']} "
            f"({item['size_bytes']} bytes)"
        )

    out_path = workdir / "journey_result.json"
    out_path.write_text(
        json.dumps(
            {
                "journey": label,
                "run": run.to_dict(),
                "matchers": verdicts,
                "matchers_overall_pass": all_pass,
                "workspace_setup": setup,
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
