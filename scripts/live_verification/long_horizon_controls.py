"""Bounded real-model acceptance of goals, paced loops, limits and backend restart.

Uses fictional checkpoints and real artifact writes. Nothing here certifies science or
claims a multi-day soak. Requests, transcripts, schedule state and verdicts are retained.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Callable

import requests
from conversation_controls import Acceptance


class LongHorizonAcceptance(Acceptance):
    """Exercise production routes without fixture responses or altered clocks."""

    def until(
        self, label: str, probe: Callable[[], Any], ready: Callable[[Any], bool], seconds: int = 360
    ) -> Any:
        """Wait with bounded HTTP calls, retaining the last observed failure."""
        deadline = time.monotonic() + seconds
        value: Any = None
        while time.monotonic() < deadline:
            try:
                value = probe()
            except requests.ConnectionError as exc:
                value = {"connection_error": str(exc)}
                time.sleep(2)
                continue
            if ready(value):
                return value
            time.sleep(2)
        self.save("timeout-" + label, value)
        raise TimeoutError(label)

    def metadata(self, sid: str) -> dict[str, Any]:
        """Read the service's persisted control state."""
        return self.call("GET", f"/v1/sessions/{sid}").get("metadata") or {}

    def session(self, name: str) -> str:
        """Create a public-safe conversation with narrowly scoped demo permission."""
        sid = self.call(
            "POST",
            "/v1/sessions",
            {
                "title": name,
                "workspace_id": self.workspace,
                "approval_mode": "bypass",
                "metadata": {"effort": "low"},
            },
        )["id"]
        self.sessions[name] = sid
        return sid

    def command(self, sid: str, name: str, text: str, **bounds: Any) -> Any:
        """Use the same command route as the composer."""
        return self.call(
            "POST", f"/v1/sessions/{sid}/commands/{name}", {"input": text, "args": bounds}
        )

    def goal_case(self) -> None:
        """A real model writes one checkpoint per turn and the judge closes the goal."""
        sid = self.session("Goal: three checkpoints")
        condition = "Three fictional checkpoints are recorded in checkpoint-1.md, checkpoint-2.md and checkpoint-3.md, with a successful create_artifact call for each."
        self.command(sid, "goal", condition, max_goal_iters=4, max_wallclock_s=300)
        self.call(
            "POST",
            f"/v1/sessions/{sid}/messages",
            {
                "text": "Public acceptance example, no science. In EACH turn write exactly ONE new checkpoint, then end your turn. "
                "Determine the next number from the conversation's successful create_artifact calls. "
                "Start at 1; stop at 3. Use create_artifact with content='Fictional checkpoint N complete.', "
                "name='checkpoint-N.md', kind='other', pdf_preview=false. Do not use shell, ask_user, subagents, "
                "loops or other tools. Report the checkpoint number actually written. End after that single file. "
                "On a goal continuation do the next single checkpoint, never all at once."
            },
        )
        meta = self.until(
            "goal", lambda: self.metadata(sid), lambda m: m.get("goal", {}).get("cleared")
        )
        self.save("goal-messages", self.messages(sid))
        self.check(
            "goal stops successfully", meta["goal"].get("clear_reason") == "goal_met", meta["goal"]
        )

    def paced_case(self, service: Path) -> None:
        """Real scheduled turns survive a waiting restart and respect the minute floor."""
        sid = self.session("Loop: paced checkpoints")
        condition = "The conversation contains successful create_artifact calls for paced-1.md and paced-2.md."
        self.command(sid, "goal", condition, max_goal_iters=4, max_wallclock_s=480)
        prompt = (
            "Public acceptance example: write ONE fictional checkpoint per turn. "
            "Read prior successful artifact calls in this conversation to choose 1 or 2. "
            "Use create_artifact with name='paced-N.md', content='Fictional paced checkpoint N complete.', "
            "kind='other', pdf_preview=false. Then call loop_wakeup(delay_seconds=60, "
            "prompt='Continue the same fictional checkpoint procedure; write only the next single paced-N.md file, then reschedule for 60 seconds.'). "
            "Report the checkpoint number and end. Do not use shell, ask_user, subagents or other tools."
        )
        self.command(sid, "loop", "60s " + prompt, max_iters=4, max_wallclock_s=480)
        original = self.metadata(sid)["loop"]["pending_schedule_id"]
        generation = json.loads((service / "server.json").read_text())["restart_count"]
        (service / "restart.request").touch()
        self.until(
            "restart-generation",
            lambda: json.loads((service / "server.json").read_text()),
            lambda row: row["restart_count"] > generation,
            seconds=60,
        )
        self.until(
            "restart-ready",
            lambda: requests.get(self.endpoint + "/v1/health", timeout=15).status_code,
            lambda status: status == 200,
            seconds=60,
        )
        meta = self.metadata(sid)
        self.check(
            "waiting loop survives restart",
            meta["loop"].get("pending_schedule_id") == original and meta["loop"].get("active"),
            meta["loop"],
        )
        meta = self.until(
            "paced-goal",
            lambda: self.metadata(sid),
            lambda m: m.get("goal", {}).get("cleared"),
            seconds=480,
        )
        rows = self.messages(sid)
        self.save("paced-messages", rows)
        self.check("paced goal met", meta["goal"].get("clear_reason") == "goal_met", meta["goal"])
        self.check(
            "goal cancels pending loop",
            meta["loop"].get("stop_reason") == "loop_goal_met"
            and not self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"],
            meta["loop"],
        )
        scheduled = [
            row
            for row in rows
            if row["role"] == "user" and row.get("metadata", {}).get("scheduled")
        ]
        self.check(
            "two real paced turns", len(scheduled) == 2, [row["created_at"] for row in scheduled]
        )

    def expiration_case(self, *, goal_limit: bool = False) -> None:
        """Elapsed waiting time expires a loop without spending a model/tool turn."""
        sid = self.session("Loop: expired while waiting")
        self.command(sid, "goal", "A checkpoint is written", max_wallclock_s=1 if goal_limit else 0)
        self.command(
            sid,
            "loop",
            "60s Write a checkpoint",
            max_wallclock_s=3600 if goal_limit else 1,
            max_iters=2,
        )
        meta = self.until(
            "expired-loop",
            lambda: self.metadata(sid),
            lambda m: m.get("loop", {}).get("stopped"),
            seconds=150,
        )
        self.check(
            "expired wakeup spends no turn",
            not any(
                row["role"] == "assistant"
                and row.get("metadata", {}).get("synthetic") != "command_result"
                for row in self.messages(sid)
            ),
            meta["loop"],
        )
        self.check(
            "expired loop and goal settle",
            meta["loop"].get("stop_reason") == ("loop_goal_capped" if goal_limit else "loop_budget")
            and meta["goal"].get("clear_reason")
            == ("goal_budget" if goal_limit else "goal_loop_stopped"),
            meta,
        )

    def stop_case(self) -> None:
        """User limits parse through production commands, and stopping cancels both."""
        sid = self.session("Loop: user stop")
        self.command(sid, "goal", "--max-iters=72 --max-hours=72 Save a final report")
        self.command(
            sid, "loop", "1h --max-iters=72 --max-hours=72 --max-tokens=1000000 One checkpoint"
        )
        meta = self.metadata(sid)
        self.check(
            "multi-day options applied",
            meta["loop"]["max_wallclock_s"] == 259200 and meta["goal"]["max_goal_iters"] == 72,
            meta,
        )
        self.command(sid, "loop", "stop")
        meta = self.metadata(sid)
        self.check(
            "user stop cancels paired work",
            meta["loop"]["stop_reason"] == "loop_user_cancelled"
            and meta["goal"]["clear_reason"] == "goal_loop_stopped",
            meta,
        )
        self.check(
            "user stop removes wakeup",
            not self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"],
            sid,
        )

    def budget_case(self) -> None:
        """A recorded token cap stops after a real turn without another continuation."""
        sid = self.session("Goal: token cap")
        self.command(sid, "goal", "--max-iters=4 --max-tokens=1 Ten checkpoint files exist")
        self.command(sid, "loop", "1h --max-iters=4 One checkpoint")
        self.call(
            "POST",
            f"/v1/sessions/{sid}/messages",
            {
                "text": "Reply with one sentence: This fictional budget check is complete. Do not call tools or schedule work."
            },
        )
        meta = self.until(
            "token-cap", lambda: self.metadata(sid), lambda m: m.get("goal", {}).get("cleared")
        )
        self.check(
            "token cap stops goal and loop",
            meta["goal"]["clear_reason"] == "goal_budget"
            and meta["loop"]["stop_reason"] == "loop_goal_capped",
            meta,
        )
        self.save("budget-messages", self.messages(sid))

    def interrupted_case(self, service: Path) -> None:
        """Interrupt an accepted real turn; never replay it as fresh autonomous work."""
        sid = self.session("Restart: interrupted work")
        self.command(sid, "goal", "Forty numbered fictional observations have been written")
        self.command(sid, "loop", "1h --max-iters=2 Continue the fictional observations")
        self.call(
            "POST",
            f"/v1/sessions/{sid}/messages",
            {
                "text": "Write forty distinct numbered fictional observations, each one sentence. No tools, no real science."
            },
        )
        before = self.call("GET", f"/v1/sessions/{sid}")
        self.check(
            "real turn running before restart", before["status"] == "running", before["status"]
        )
        generation = json.loads((service / "server.json").read_text())["restart_count"]
        (service / "restart.request").touch()
        self.until(
            "interrupted-generation",
            lambda: json.loads((service / "server.json").read_text()),
            lambda row: row["restart_count"] > generation,
            seconds=60,
        )
        self.until(
            "interrupted-ready",
            lambda: requests.get(self.endpoint + "/v1/health", timeout=15).status_code,
            lambda status: status == 200,
            seconds=60,
        )
        meta = self.metadata(sid)
        self.check(
            "interrupted controls stop explicitly",
            meta["goal"]["clear_reason"] == "goal_server_interrupted"
            and meta["loop"]["stop_reason"] == "loop_server_interrupted",
            meta,
        )
        self.check(
            "interrupted work has no pending wakeup",
            not self.call("GET", f"/v1/sessions/{sid}/schedules")["schedules"],
            sid,
        )
        self.save("interrupted-messages", self.messages(sid))

    def question_case(self) -> None:
        """A due loop must not talk over a pending human question."""
        sid = self.session("Loop: waits for your answer")
        self.call("PATCH", f"/v1/sessions/{sid}/agent", {"ref": "clio/standard"})
        self.command(sid, "goal", "The user has selected a color and the selection is recorded")
        self.command(sid, "loop", "60s --max-iters=2 Continue only after the user answers")
        self.call(
            "POST",
            f"/v1/sessions/{sid}/messages",
            {
                "text": "Call the declared ask_user tool now with question='Which color should the fictional report use: blue or green?', response_mode='blocking'. Do not ask only in prose and do not choose for me. No other tools. This is fictional acceptance data."
            },
        )
        row = self.until(
            "question",
            lambda: self.call("GET", f"/v1/sessions/{sid}"),
            lambda row: row["status"] == "waiting_user",
        )
        started = time.monotonic()
        while time.monotonic() - started < 125:
            time.sleep(5)
            row = self.call("GET", f"/v1/sessions/{sid}")
            if row["status"] != "waiting_user":
                break
        messages = self.messages(sid)
        self.save("question-messages", messages)
        self.check(
            "due loop waits for human",
            row["status"] == "waiting_user"
            and not any(message.get("metadata", {}).get("scheduled") for message in messages),
            row["status"],
        )
        self.command(sid, "loop", "stop")


def main() -> int:
    """Run one bounded case and preserve exact evidence on failure too."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:17963")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--service", type=Path, required=True)
    parser.add_argument(
        "--case",
        choices=[
            "goal",
            "paced",
            "expiration",
            "goal-expiration",
            "stop",
            "budget",
            "interrupted",
            "question",
        ],
        required=True,
    )
    args = parser.parse_args()
    runner = LongHorizonAcceptance(args.endpoint, args.out)
    provider = runner.call("GET", "/v1/providers/lm")
    if provider.get("provider") != "codex" or provider.get("model") != "gpt-6-luna":
        runner.call(
            "PUT", "/v1/providers/lm", {"provider": "codex", "model": "gpt-6-luna", "api_base": ""}
        )
    runner.call("GET", "/v1/providers/lm/wait?timeout=20")
    workspace = args.workspace / "long-horizon" / args.out.name
    workspace.mkdir(parents=True, exist_ok=True)
    runner.workspace = runner.call(
        "POST",
        "/v1/workspaces",
        {"name": "Unattended controls", "root_path": str(workspace)},
        status=201,
    )["id"]
    started = time.time()
    try:
        if args.case == "goal":
            runner.goal_case()
        elif args.case == "paced":
            runner.paced_case(args.service)
        elif args.case == "expiration":
            runner.expiration_case()
        elif args.case == "goal-expiration":
            runner.expiration_case(goal_limit=True)
        elif args.case == "stop":
            runner.stop_case()
        elif args.case == "interrupted":
            runner.interrupted_case(args.service)
        elif args.case == "question":
            runner.question_case()
        else:
            runner.budget_case()
    finally:
        for sid in runner.sessions.values():
            meta = runner.metadata(sid)
            if meta.get("loop", {}).get("active"):
                runner.command(sid, "loop", "stop")
            if meta.get("goal", {}).get("active"):
                runner.command(sid, "goal", "clear")
        runner.save(
            "run",
            {
                "started": started,
                "elapsed_seconds": time.time() - started,
                "case": args.case,
                "sessions": runner.sessions,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
