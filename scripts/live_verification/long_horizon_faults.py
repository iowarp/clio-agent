"""Run bounded, real-model intervention cases beside an uninterrupted soak.

Reuse a frozen source tree and its existing interpreter. No dependency install,
worktree, substitute provider, accelerated clock or change to the soak is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

SOURCE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SOURCE), str(SOURCE / "src")]

from _common import terminate_server  # noqa: E402
from long_horizon_controls import LongHorizonAcceptance  # noqa: E402
from long_horizon_soak import publish, resources  # noqa: E402

from tests._cte_isolation import isolate_cte_env, reap_private_daemon  # noqa: E402


class Owner:
    """Own the separate service and its private core, including real restarts."""

    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True)
        self.env = dict(os.environ)
        self.env.update(
            {
                "CLIO_AGENT_HOME": str(root / "home"),
                "CLIO_SESSIONS_PATH": str(root / "home/sessions.json"),
                "CLIO_ALLOWED_ROOTS": str(self.workspace),
                "CLIO_LM_PROVIDER": "codex",
                "CLIO_LM_MODEL": "gpt-6-luna",
                "PYTHONPATH": str(SOURCE / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "UV_CACHE_DIR": str(root / "cache"),
                "CLIO_TEST_CTE_FILE_TIER_CAPACITY": "256MB",
                "CLIO_STREAM_AUDIT_LOG": str(root / "stream-audit.jsonl"),
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            }
        )
        self.isolation = isolate_cte_env(root / "private-core", self.env)
        self.generation = 0

    def start(self) -> None:
        """Start the real product and wait for a usable model with a finite deadline."""
        with (self.root / "server.log").open("ab", buffering=0) as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from clio_agent.gact.app import run_server; "
                    f"run_server(host='127.0.0.1', port={self.port})",
                ],
                cwd=self.workspace,
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        publish(
            self.root / "server.json",
            {"pid": self.process.pid, "port": self.port, "generation": self.generation},
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            publish(self.root / "resources.json", resources(self.root))
            if self.process.poll() is not None:
                raise RuntimeError("Owned fault service exited during startup")
            try:
                response = requests.get(f"http://127.0.0.1:{self.port}/v1/health", timeout=15)
                if response.status_code == 200:
                    return
            except requests.ConnectionError:
                pass  # Expected while this newly spawned socket is not listening yet.
            time.sleep(2)
        raise TimeoutError("Fault service model readiness")

    def restart(self) -> None:
        """Stop the owned process tree, preserving durable session and schedule state."""
        if self.process is None:
            raise RuntimeError("Cannot restart an unstarted service")
        terminate_server(self.process)
        self.generation += 1
        self.start()

    def close(self) -> None:
        """Reap the owned backend and private core even after a failed assertion."""
        if self.process is not None:
            terminate_server(self.process)
        reap_private_daemon(self.isolation.state_dir)
        publish(self.root / "service-stopped.json", {"stopped": True, "at": time.time()})


class Faults(LongHorizonAcceptance):
    """Verify actual interventions and independently inspect their saved outputs."""

    def __init__(self, owner: Owner, name: str) -> None:
        super().__init__(f"http://127.0.0.1:{owner.port}", owner.root / name)
        self.owner = owner
        self.folder = owner.workspace / name
        self.folder.mkdir()
        self.workspace = self.call(
            "POST", "/v1/workspaces", {"name": name, "root_path": str(self.folder)}, status=201
        )["id"]

    def call(self, method: str, path: str, body: Any = None, *, status: int = 200) -> Any:
        """Check owned resource growth even while waiting for a model or tool."""
        publish(self.owner.root / "resources.json", resources(self.owner.root))
        return super().call(method, path, body, status=status)

    def send(self, sid: str, text: str) -> None:
        """Retry only a typed startup refusal, which has not accepted a model turn."""
        deadline = time.monotonic() + 180
        while True:
            response = requests.post(
                self.endpoint + f"/v1/sessions/{sid}/messages", json={"text": text}, timeout=45
            )
            value = response.json()
            self.requests.append(
                {"method": "POST", "status": response.status_code, "response": value}
            )
            self.save("requests", self.requests)
            if response.status_code == 200:
                return
            if (
                response.status_code != 503
                or value.get("error", {}).get("details", {}).get("agent_status") != "starting"
                or time.monotonic() >= deadline
            ):
                raise AssertionError(f"Message admission failed: {value}")
            time.sleep(2)

    def hold(self, seconds: int) -> None:
        """Wait real elapsed time while checking resource ceilings every five seconds."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            publish(self.owner.root / "resources.json", resources(self.owner.root))
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def record(self, sid: str) -> list[dict[str, Any]]:
        """Retain transcript, controls and independent output byte hashes."""
        rows = self.messages(sid)
        self.save("transcript", {"messages": rows})
        self.save("state", self.call("GET", f"/v1/sessions/{sid}"))
        self.save(
            "files",
            {
                p.name: {
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    "text": p.read_text(),
                }
                for p in self.folder.iterdir()
                if p.is_file()
            },
        )
        return rows

    def replacement(self) -> None:
        """Clearing the goal preserves its loop; replacing the loop cancels the old wake."""
        sid = self.session("Replace loop and clear goal")
        self.command(sid, "goal", "A fictional old output exists")
        self.command(sid, "loop", "60s --max-iters=1 Write old-output.md containing old")
        before = self.metadata(sid)["loop"]["pending_schedule_id"]
        self.command(sid, "goal", "clear")
        meta = self.metadata(sid)
        self.check("goal clear preserves explicit loop", meta["loop"]["active"], meta)
        self.command(
            sid,
            "loop",
            "60s --max-iters=1 Use create_artifact(name='new-output.md', "
            "content='NEW-ONLY', kind='other', pdf_preview=false). No other tools.",
        )
        schedules = self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"]
        self.check(
            "replacement has one fresh wake",
            len(schedules) == 1 and schedules[0]["id"] != before,
            schedules,
        )
        self.until("new-output", lambda: (self.folder / "new-output.md").exists(), bool, 180)
        self.wait(sid)
        self.hold(70)
        rows = self.record(sid)
        self.check(
            "old wake never delivered",
            not (self.folder / "old-output.md").exists()
            and len([m for m in rows if m.get("metadata", {}).get("scheduled")]) == 1,
            rows,
        )
        self.check(
            "replacement output bytes",
            (self.folder / "new-output.md").read_text().strip() == "NEW-ONLY",
            self.metadata(sid),
        )

    def iterations(self) -> None:
        """A loop cap permits exactly two real scheduled writes and no third wakeup."""
        sid = self.session("Two scheduled loop iterations")
        prompt = (
            "Fictional acceptance only. Write exactly ONE new numbered file per turn, sequentially from loop-1.md onward. "
            "START WITH 1 if this conversation has no successful artifact calls. After loop-1.md, write loop-2.md. "
            "Your FIRST tool call must create the next file; do not schedule before creating it. "
            "Use create_artifact(kind='other', pdf_preview=false, content='ITERATION-N', name='loop-N.md'). "
            "Only AFTER that single successful write, call loop_wakeup with delay_seconds=60 and stop=false. "
            "Its prompt must repeat this complete procedure, including starting at 1 if no previous artifact exists. "
            "End that turn. Repeat for at least ten files unless the runtime refuses continuation. "
            "Do not voluntarily stop after two files: this checks whether the runtime enforces the user cap. "
            "No other tools or questions."
        )
        self.command(sid, "loop", "60s --max-iters=2 " + prompt)
        meta = self.until(
            "iteration-cap",
            lambda: self.metadata(sid),
            lambda m: m.get("loop", {}).get("stopped"),
            420,
        )
        self.wait(sid)
        self.hold(70)
        rows = self.record(sid)
        names = sorted(p.name for p in self.folder.glob("loop-*.md"))
        self.check("two files only", names == ["loop-1.md", "loop-2.md"], names)
        self.check(
            "iteration output bytes",
            all(
                (self.folder / f"loop-{n}.md").read_text().strip() == f"ITERATION-{n}"
                for n in (1, 2)
            ),
            names,
        )
        self.check(
            "two scheduled turns only",
            len([m for m in rows if m.get("metadata", {}).get("scheduled")]) == 2,
            names,
        )
        self.check(
            "iteration cap cancels future work",
            meta["loop"]["stop_reason"] == "loop_max_iters"
            and not self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"],
            meta,
        )

    def answered_question(self) -> None:
        """An overdue loop cannot bypass human input; an actual answer resumes once."""
        sid = self.session("Answer an overdue human question")
        self.call("POST", f"/v1/sessions/{sid}/agent-blueprint", {"blueprint_id": "base-agent"})
        self.command(
            sid, "goal", "answer.md was successfully written containing exactly HUMAN-BLUE"
        )
        self.command(
            sid,
            "loop",
            "60s --max-iters=4 Continue after the user's answer; preserve their selection",
        )
        self.send(
            sid,
            "Fictional acceptance. Call ask_user(question='Which color: blue or green?', response_mode='blocking'). "
            "Wait for the actual human answer. Once answered, write answer.md with exactly HUMAN-BLUE if blue, "
            "HUMAN-GREEN if green using create_artifact(kind='other', pdf_preview=false). Never guess. No other tools.",
        )
        row = self.until(
            "human-question",
            lambda: self.call("GET", f"/v1/sessions/{sid}"),
            lambda m: m["status"] == "waiting_user",
        )
        self.hold(125)
        self.check(
            "overdue wake waits for human",
            self.call("GET", f"/v1/sessions/{sid}")["status"] == "waiting_user"
            and not any(m.get("metadata", {}).get("scheduled") for m in self.messages(sid)),
            row["id"],
        )
        qid = row["metadata"]["pending_user_question_id"]
        self.call(
            "POST",
            f"/v1/sessions/{sid}/questions/{qid}/answer",
            {"answer": "blue", "selected_options": [], "metadata": {}},
        )
        meta = self.until(
            "answered-goal", lambda: self.metadata(sid), lambda m: m.get("goal", {}).get("cleared")
        )
        self.wait(sid)
        self.hold(70)
        rows = self.record(sid)
        self.check(
            "answer produces actual file",
            (self.folder / "answer.md").read_text().strip() == "HUMAN-BLUE",
            meta,
        )
        self.check(
            "answer completes and cancels overdue wake",
            meta["goal"]["clear_reason"] == "goal_met"
            and not self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"]
            and not any(m.get("metadata", {}).get("scheduled") for m in rows),
            meta,
        )

    def active_stop(self, *, restart: bool = False) -> None:
        """Cancel or restart after an actual write, preserving bytes without replay."""
        sid = self.session("Restart active writes" if restart else "Stop active writes")
        self.command(sid, "goal", "All forty fictional files have been written")
        self.command(sid, "loop", "60s --max-iters=3 Continue writing forty files")
        self.send(
            sid,
            "Use create_artifact forty separate times, one file per call, stop-01.md to stop-40.md, "
            "each containing its filename, kind='other', pdf_preview=false. Do not batch calls. No other tools.",
        )
        self.until("first-real-write", lambda: (self.folder / "stop-01.md").exists(), bool)
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.folder.glob("stop-*.md")
        }
        self.check(
            "turn active after actual write",
            self.call("GET", f"/v1/sessions/{sid}")["status"] == "running",
            before,
        )
        if restart:
            self.owner.restart()
        else:
            self.call("POST", f"/v1/sessions/{sid}/cancel", {}, status=204)
        self.hold(80)
        self.record(sid)
        after = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.folder.glob("stop-*.md")
        }
        meta = self.metadata(sid)
        self.check(
            "at most the in-flight write finishes",
            len(after) <= len(before) + 1,
            {"before": before, "after": after},
        )
        self.check(
            "previous write preserved", all(after.get(n) == h for n, h in before.items()), after
        )
        self.check(
            "interrupted controls inactive",
            not meta["goal"]["active"]
            and not meta["loop"]["active"]
            and not self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"],
            meta,
        )
        self.check(
            "work did not finish or replay",
            len(after) < 40
            and not any(m.get("metadata", {}).get("scheduled") for m in self.messages(sid)),
            after,
        )
        if restart:
            self.check(
                "restart reports interruption",
                meta["goal"]["clear_reason"] == "goal_server_interrupted"
                and meta["loop"]["stop_reason"] == "loop_server_interrupted",
                meta,
            )

    def provider_outage(self) -> None:
        """Refuse real outbound model connections, then restore and verify manual recovery."""
        keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "CLIO_CODEX_TRANSPORT")
        previous = {key: self.owner.env.get(key) for key in keys}
        sid = self.session("Provider unavailable then restored")
        try:
            self.owner.env.update(
                HTTPS_PROXY="http://127.0.0.1:1",
                HTTP_PROXY="http://127.0.0.1:1",
                ALL_PROXY="http://127.0.0.1:1",
                NO_PROXY="127.0.0.1,localhost",
                CLIO_CODEX_TRANSPORT="sse",
            )
            self.owner.restart()
            self.command(sid, "goal", "--max-iters=1 --max-hours=0.025 recovered.md exists")
            self.command(
                sid,
                "loop",
                "60s --max-iters=2 --max-hours=0.025 Continue only when the provider is available",
            )
            self.send(sid, "Reply with the exact word CONNECTED. Do not call tools.")
            meta = self.until(
                "outage-settles",
                lambda: self.metadata(sid),
                lambda m: m.get("goal", {}).get("cleared"),
                180,
            )
            rows = self.record(sid)
            self.save("outage-transcript", {"messages": rows})
            self.check(
                "outage is recorded as failed work",
                any(m.get("stop_reason") == "error" for m in rows),
                meta,
            )
            self.check(
                "unavailable provider cannot keep spending",
                not meta["loop"]["active"]
                and not self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"]
                and meta["goal"]["clear_reason"] in {"goal_judge_unavailable", "goal_budget"},
                meta,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    self.owner.env.pop(key, None)
                else:
                    self.owner.env[key] = value
            self.owner.restart()
        self.send(
            sid,
            "NEW REQUEST: the earlier connectivity probe is complete. Its no-tools restriction "
            "applied only to that probe and is withdrawn. You are now authorized and required to "
            "make one real create_artifact call with name='recovered.md', content='RECOVERY-VERIFIED', "
            "kind='other', pdf_preview=false. Save the file now, then report the actual result.",
        )
        self.wait(sid)
        self.record(sid)
        self.check(
            "restored provider performs a real write",
            (self.folder / "recovered.md").read_text().strip() == "RECOVERY-VERIFIED",
            sid,
        )

    def duplicate_wakeup(self) -> None:
        """Revise a real wakeup twice in one model turn without consuming two iterations."""
        sid = self.session("Revise the pending wakeup")
        self.command(sid, "loop", "1h --max-iters=3 Continue the fictional checkpoint")
        self.send(
            sid,
            "Fictional control verification. Call loop_wakeup twice in this turn. "
            "First delay_seconds=60, prompt='FIRST CONFIGURATION'. Then revise that decision with "
            "a second actual call, delay_seconds=120, prompt='REVISED CONFIGURATION'. Both calls are "
            "required to verify replacing the pending wakeup. No other tools. End immediately afterward.",
        )
        rows = self.wait(sid)
        meta = self.metadata(sid)
        schedules = self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"]
        calls = [
            c
            for m in rows
            for c in m.get("metadata", {}).get("tools_called", [])
            if c["name"] == "loop_wakeup"
        ]
        self.check(
            "two actual wakeup calls", len(calls) == 2 and all(c.get("ok") for c in calls), calls
        )
        self.check(
            "same turn spends one iteration",
            meta["loop"]["iteration"] == 1 and meta["loop"]["active"],
            meta,
        )
        self.check(
            "revised instruction has one wake",
            len(schedules) == 1 and schedules[0]["question"] == "REVISED CONFIGURATION",
            schedules,
        )
        self.record(sid)


def main() -> int:
    """Run independent cases, retaining every failed receipt and stopping all owned work."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-receipt", required=True, type=Path)
    parser.add_argument("--port", default=17964, type=int)
    choices = (
        "replacement",
        "iterations",
        "answered_question",
        "active_cancel",
        "active_restart",
        "provider_outage",
        "duplicate_wakeup",
    )
    parser.add_argument("--cases", nargs="+", choices=choices, default=list(choices))
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists():
        raise RuntimeError("Refusing to overwrite earlier fault evidence")
    identity = json.loads(args.source_receipt.read_text())
    if any(
        hashlib.sha256((SOURCE / n).read_bytes()).hexdigest() != h
        for n, h in identity["files"].items()
    ):
        raise AssertionError("Frozen runtime differs from its source receipt")
    owner = Owner(root, args.port)
    publish(root / "source.json", identity)
    publish(
        root / "harness-identity.json",
        {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (SOURCE / "scripts/live_verification").glob("long_horizon_*.py")
        },
    )
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    verdicts: list[dict[str, Any]] = []
    try:
        owner.start()
        for name in args.cases:
            runner = Faults(owner, name)
            started = time.time()
            try:
                if name in {"active_cancel", "active_restart"}:
                    runner.active_stop(restart=name == "active_restart")
                else:
                    getattr(runner, name)()
                verdicts.append({"case": name, "passed": True, "elapsed": time.time() - started})
            except (
                AssertionError,
                TimeoutError,
                requests.RequestException,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                verdicts.append(
                    {
                        "case": name,
                        "passed": False,
                        "error": repr(exc),
                        "elapsed": time.time() - started,
                    }
                )
                print(f"FAIL {name}: {exc}", flush=True)
            finally:
                for sid in runner.sessions.values():
                    runner.record(sid)
                    runner.call("POST", f"/v1/sessions/{sid}/cancel", {}, status=204)
                publish(root / "verdicts.json", verdicts)
        return int(any(not v["passed"] for v in verdicts))
    except (Exception, KeyboardInterrupt) as exc:
        publish(root / "fatal.json", {"at": time.time(), "error": repr(exc)})
        raise
    finally:
        owner.close()


if __name__ == "__main__":
    raise SystemExit(main())
