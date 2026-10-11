"""Run a real elapsed 48-hour CLIO qualification under an enclosing systemd unit.

The model writes fictional checkpoints through production tools. This independent
observer verifies bytes, pacing and resource ceilings; it never supplies later turns.
Run with uv run --no-project <pinned-python> <this-file> --root <owned-run-root>.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import requests

SOURCE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SOURCE), str(SOURCE / "src")]

from _common import terminate_server  # noqa: E402
from long_horizon_evidence import audit_record  # noqa: E402

from tests._cte_isolation import isolate_cte_env, reap_private_daemon  # noqa: E402

MARKER = "CLIO-LONG-HORIZON-20261011"
COUNT = 49
MIN_SECONDS = 48 * 3600
MAX_SECONDS = 54 * 3600
MAX_DISK = 4 * 1024**3
MIN_FREE = 10 * 1024**3
MAX_RSS = 3 * 1024**3
MAX_CHILDREN = 80
MAX_TOKENS = 4_000_000


def publish(path: Path, value: Any) -> None:
    """Atomically replace one bounded status or evidence record."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def verify_reports(workspace: Path, prior: dict[str, str]) -> dict[str, str]:
    """Reject changed, skipped or malformed independently inspected checkpoints."""
    reports = sorted(workspace.glob("batch-*.json"))
    hashes: dict[str, str] = {}
    if len(reports) > COUNT:
        raise AssertionError("Extra checkpoint files")
    for number, path in enumerate(reports):
        if path.name != f"batch-{number:03d}.json":
            raise AssertionError("Checkpoint sequence has a gap")
        raw = path.read_bytes()
        expected = {"batch": number, "next": number + 1, "previous": number - 1, "marker": MARKER}
        if json.loads(raw) != expected:
            raise AssertionError(f"Invalid checkpoint contents: {path.name}")
        digest = hashlib.sha256(raw).hexdigest()
        if path.name in prior and prior[path.name] != digest:
            raise AssertionError(f"Checkpoint was overwritten: {path.name}")
        hashes[path.name] = digest
    if set(prior) - hashes.keys():
        raise AssertionError("A checkpoint was removed")
    return hashes


def verify_manifest(workspace: Path) -> None:
    """Check the model's actual final manifest against all expected filenames."""
    manifest = json.loads((workspace / "final-manifest.json").read_bytes())
    if manifest != {"marker": MARKER, "files": [f"batch-{n:03d}.json" for n in range(COUNT)]}:
        raise AssertionError("Final manifest does not name every checkpoint")


def resources(root: Path) -> dict[str, int | float]:
    """Measure the owned tree and refuse growth beyond its explicit ceilings."""
    children = psutil.Process().children(recursive=True)
    rss = psutil.Process().memory_info().rss
    for child in children:
        try:
            rss += child.memory_info().rss
        except psutil.NoSuchProcess:
            continue
    allocated = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                allocated += path.stat().st_blocks * 512
        except FileNotFoundError:
            # Concurrent atomic writes may retire an observed temporary file.
            continue
    free = shutil.disk_usage(root).free
    result = {
        "at": time.time(),
        "children": len(children),
        "rss": rss,
        "disk": allocated,
        "free": free,
    }
    if allocated > MAX_DISK or free < MIN_FREE or rss > MAX_RSS or len(children) > MAX_CHILDREN:
        raise RuntimeError(f"Owned resource ceiling exceeded: {result}")
    return result


class Soak:
    """Observe a real service with finite requests and durable evidence."""

    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.endpoint = f"http://127.0.0.1:{port}"
        self.sid = ""
        self.started = 0.0
        self.started_mono = 0.0
        self.hashes: dict[str, str] = {}
        self.observed: dict[str, float] = {}
        self.workspace = root / "runtime/workspace"

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """Call a production endpoint and retain all writes with their responses."""
        response = requests.request(method, self.endpoint + path, json=body, timeout=45)
        value = response.json() if response.content else None
        if method != "GET":
            with (self.root / "requests.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": time.time(),
                            "method": method,
                            "path": path,
                            "body": body,
                            "status": response.status_code,
                            "response": value,
                        }
                    )
                    + "\n"
                )
        response.raise_for_status()
        return value

    def command(self, name: str, text: str) -> Any:
        """Use the same goal and loop commands as the user composer."""
        return self.call(
            "POST", f"/v1/sessions/{self.sid}/commands/{name}", {"input": text, "args": {}}
        )

    def start(self, process: subprocess.Popen[bytes]) -> None:
        """Arm public controls and send the single initial instruction."""
        deadline = time.monotonic() + 240
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Service exited during startup: {process.returncode}")
            try:
                health = self.call("GET", "/v1/health")
                if health.get("overall_status") == "ready":
                    break
            except (requests.ConnectionError, requests.HTTPError):
                if time.monotonic() > deadline:
                    raise
            if time.monotonic() > deadline:
                raise TimeoutError("Real Codex provider did not become ready")
            time.sleep(3)
        publish(self.root / "health.json", health)
        publish(self.root / "provider.json", self.call("GET", "/v1/providers/lm"))
        wid = self.call(
            "POST",
            "/v1/workspaces",
            {"name": "48-hour fictional qualification", "root_path": str(self.workspace)},
        )["id"]
        self.sid = self.call(
            "POST",
            "/v1/sessions",
            {
                "title": "48-hour hourly checkpoints",
                "workspace_id": wid,
                "approval_mode": "bypass",
                "metadata": {"effort": "low"},
            },
        )["id"]
        self.call(
            "POST", f"/v1/sessions/{self.sid}/agent-blueprint", {"blueprint_id": "base-agent"}
        )
        instruction = (
            f"This is a fictional 48-hour software soak, marker {MARKER}. No science, subagents, questions, web, "
            "shell, packages or extra tools. In EACH turn write exactly ONE new numbered JSON checkpoint, "
            "then end. Numbers run 0 through 48 inclusive. Determine the next number from the latest successful "
            "create_artifact result in this conversation (start with 0). BEFORE every checkpoint after 0, "
            "use fs_read_file on the previous checkpoint's absolute workspace path and verify its marker and next number. "
            f"Workspace: {self.workspace}. Use create_artifact(name='batch-NNN.json', kind='other', pdf_preview=false, "
            f"content=JSON with exactly batch:N, next:N+1, previous:N-1, marker:'{MARKER}'). "
            "NNN is zero-padded to three digits. Preserve every previous file. After each checkpoint below 48, "
            "call loop_wakeup(delay_seconds=3600, prompt='') to preserve this complete original procedure. "
            "Never reduce the one-hour delay. At checkpoint 48 ALSO create final-manifest.json, kind other, "
            f"pdf_preview=false, containing exactly marker:'{MARKER}' and files:[all 49 batch-NNN.json filenames "
            "in ascending order]. Then finish without another wakeup. Do not claim science or broader reliability. "
            "Reply only with the completed checkpoint number and next wake status."
        )
        publish(self.root / "instruction.json", {"text": instruction})
        self.command(
            "goal",
            f"--max-iters=64 --max-hours=54 --max-tokens={MAX_TOKENS} "
            "All 49 checkpoints batch-000.json through batch-048.json and final-manifest.json exist, "
            "with successful create_artifact results, preserved marker and ordered file list.",
        )
        self.command(
            "loop", f"1h --max-iters=64 --max-hours=54 --max-tokens={MAX_TOKENS} " + instruction
        )
        self.started = time.time()
        self.started_mono = time.monotonic()
        publish(
            self.root / "identity.json",
            {
                "session_id": self.sid,
                "workspace_id": wid,
                "started": self.started,
                "started_monotonic": self.started_mono,
                "earliest_pass": self.started + MIN_SECONDS,
                "hard_deadline": self.started + MAX_SECONDS,
                "marker": MARKER,
                "checkpoints": COUNT,
                "minimum_elapsed_seconds": MIN_SECONDS,
                "max_tokens": MAX_TOKENS,
            },
        )
        admission_deadline = time.monotonic() + 240
        while True:
            try:
                self.call("POST", f"/v1/sessions/{self.sid}/messages", {"text": instruction})
                break
            except requests.HTTPError as exc:
                response = exc.response
                error = response.json().get("error", {}) if response is not None else {}
                if (
                    response is None
                    or response.status_code != 503
                    or error.get("details", {}).get("agent_status") != "starting"
                    or time.monotonic() >= admission_deadline
                ):
                    raise
                # This refusal did not accept a turn; retry only startup admission.
                time.sleep(3)

    def observe(self) -> bool:
        """Record current facts, fail on stalled work, and accept only a full soak."""
        session = self.call("GET", f"/v1/sessions/{self.sid}")
        schedules = self.call("GET", f"/v1/sessions/{self.sid}/schedules")["schedules"]
        meta = session.get("metadata") or {}
        now = time.time()
        if session["status"] == "idle":
            found = verify_reports(self.workspace, self.hashes)
            if found != self.hashes:
                for name in found.keys() - self.hashes.keys():
                    self.observed[name] = now
                self.hashes = found
                publish(
                    self.root / "file-evidence.json",
                    {"sha256": found, "observed_at": self.observed},
                )
                publish(
                    self.root / "transcript.json",
                    self.call("GET", f"/v1/sessions/{self.sid}/messages"),
                )
                print(f"Observed {len(found)}/{COUNT} verified checkpoints", flush=True)
        status = {
            "status": "running",
            "session_id": self.sid,
            "session_status": session["status"],
            "elapsed_seconds": now - self.started,
            "elapsed_monotonic_seconds": time.monotonic() - self.started_mono,
            "checkpoints": len(self.hashes),
            "goal": meta.get("goal"),
            "loop": meta.get("loop"),
            "schedules": schedules,
            "resources": resources(self.root),
        }
        publish(self.root / "status.json", status)
        if session["status"] == "idle" and self.hashes:
            publish(self.root / "independent-audit.json", audit_record(self.root))
        with (self.root / "metrics.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "elapsed": status["elapsed_seconds"],
                        "checkpoints": len(self.hashes),
                        **status["resources"],
                    }
                )
                + "\n"
            )
        if len(self.hashes) == COUNT and meta.get("goal", {}).get("clear_reason") == "goal_met":
            verify_manifest(self.workspace)
            if (
                now - self.started < MIN_SECONDS
                or status["elapsed_monotonic_seconds"] < MIN_SECONDS
                or schedules
                or meta.get("loop", {}).get("active")
            ):
                raise AssertionError("Premature completion or pending work")
            for n in range(1, COUNT):
                a = self.workspace / f"batch-{n - 1:03d}.json"
                b = self.workspace / f"batch-{n:03d}.json"
                if b.stat().st_mtime - a.stat().st_mtime < 3590:
                    raise AssertionError("Checkpoint interval was shortened")
            audit = audit_record(self.root, complete=True)
            if audit.get("qualified") is not True:
                raise AssertionError("Independent transcript audit did not qualify the run")
            publish(self.root / "independent-audit.json", audit)
            publish(self.root / "verdict.json", {"passed": True, **status, "status": "passed"})
            return True
        if session["status"] in {"error", "cancelled", "waiting_user", "waiting_permission"}:
            raise RuntimeError(f"Unattended run requires intervention: {session['status']}")
        if meta.get("goal", {}).get("cleared") or meta.get("loop", {}).get("stopped"):
            raise RuntimeError("Controls stopped before independent acceptance")
        last = max(self.observed.values(), default=self.started)
        if now - last > (1200 if not self.hashes else 4800):
            raise TimeoutError("No new checkpoint within the expected batch window")
        return False


def main() -> int:
    """Own a private core and service; stop the entire owned process tree on exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", default=17963, type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if (root / "identity.json").exists():
        raise RuntimeError("Refusing to overwrite or resume a qualification silently")
    workspace = root / "runtime/workspace"
    workspace.mkdir(parents=True)
    env = dict(os.environ)
    env.update(
        {
            "CLIO_AGENT_HOME": str(root / "runtime/home"),
            "CLIO_SESSIONS_PATH": str(root / "runtime/home/sessions.json"),
            "CLIO_ALLOWED_ROOTS": str(workspace),
            "CLIO_LM_PROVIDER": "codex",
            "CLIO_LM_MODEL": "gpt-6-luna",
            "CLIO_STREAM_AUDIT_LOG": str(root / "stream-audit.jsonl"),
            "PYTHONPATH": str(SOURCE / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_CACHE_DIR": str(root / "runtime/cache"),
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
    )
    isolation = isolate_cte_env(root / "runtime/private-core", env)
    publish(
        root / "runtime-versions.json",
        {
            "python": sys.version,
            "executable": sys.executable,
            "libraries": {
                n: importlib.metadata.version(n)
                for n in ("iowarp-core", "clio-schemas", "dspy", "fastmcp", "litellm")
            },
            "limits": {
                "disk": MAX_DISK,
                "rss": MAX_RSS,
                "children": MAX_CHILDREN,
                "minimum_free": MIN_FREE,
                "seconds": MAX_SECONDS,
            },
            "baseline": resources(root),
            "core_port": isolation.port,
        },
    )
    signal.signal(
        signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError("Owner stopped unit"))
    )
    runner = Soak(root, args.port)
    child: subprocess.Popen[bytes] | None = None
    try:
        with (root / "server.log").open("ab", buffering=0) as log:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from clio_agent.gact.app import run_server; "
                    f"run_server(host='127.0.0.1', port={args.port})",
                ],
                cwd=workspace,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            publish(
                root / "server.json",
                {"pid": child.pid, "endpoint": runner.endpoint, "core_port": isolation.port},
            )
            runner.start(child)
            while time.time() - runner.started < MAX_SECONDS:
                if child.poll() is not None:
                    raise RuntimeError(f"Owned service exited: {child.returncode}")
                if runner.observe():
                    return 0
                time.sleep(30)
            raise TimeoutError("Maximum soak duration reached")
    except (Exception, KeyboardInterrupt) as exc:
        publish(
            root / "verdict.json",
            {"passed": False, "at": time.time(), "error": repr(exc), "session_id": runner.sid},
        )
        publish(
            root / "status.json", {"status": "failed", "error": repr(exc), "session_id": runner.sid}
        )
        raise
    finally:
        if child is not None:
            terminate_server(child)
        reap_private_daemon(isolation.state_dir)
        publish(
            root / "service-stopped.json",
            {
                "at": time.time(),
                "stopped": True,
                "remaining_children": [
                    p.pid for p in psutil.Process().children(recursive=True) if p.is_running()
                ],
            },
        )


if __name__ == "__main__":
    raise SystemExit(main())
