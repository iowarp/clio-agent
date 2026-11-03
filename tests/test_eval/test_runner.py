"""Usage guard, scheduling, turn waiting and one full run against a fake GACT client."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from scripts.eval.tool_calling import tasks
from scripts.eval.tool_calling.http_client import GactClient, GactError
from scripts.eval.tool_calling.runner import plan_runs, schedule
from scripts.eval.tool_calling.session_run import (
    RunContext,
    RunSpec,
    execute_run,
    result_path,
    wait_for_turn,
)
from scripts.eval.tool_calling.summarize import load_runs, render
from scripts.eval.tool_calling.usage_guard import GuardDecision, decide, window_deltas
from tests.test_eval import _runs

# ------------------------------------------------------------------ usage guard


def test_guard_stops_at_threshold() -> None:
    assert not decide(_runs.fleet(92.9)).stop
    stop = decide(_runs.fleet(93.0))
    assert stop.stop and stop.window == "Weekly" and stop.utilization == pytest.approx(0.93)
    assert "resets at 2026-11-08" in stop.reason
    assert decide(_runs.fleet(50.0), stop_at=0.5).stop


def test_guard_reads_the_highest_weekly_window() -> None:
    fleet = _runs.fleet(40.0)
    fleet["accounts"][0]["usage"]["windows"].append(
        {"name": "Weekly (Sonnet)", "used_percent": 95.5, "window_minutes": 10080}
    )
    decision = decide(fleet)
    assert decision.stop and decision.window == "Weekly (Sonnet)"


def test_guard_without_a_reading_starts_and_says_why() -> None:
    fleet = {
        "accounts": [{"id": "claude_code", "usage": None, "usage_missing_reason": "no_reading_yet"}]
    }
    decision = decide(fleet)
    assert not decision.stop and "no_reading_yet" in decision.reason
    assert "not in the fleet" in decide({"accounts": []}).reason
    named_only = {
        "accounts": [
            {"id": "claude_code", "usage": {"windows": [{"name": "Weekly", "used_percent": 99}]}}
        ]
    }
    assert decide(named_only).stop  # fmt: skip


def test_window_deltas_skip_a_reset() -> None:
    deltas = window_deltas(_runs.fleet(40.0, five=10.0), _runs.fleet(41.5, five=12.0))
    assert deltas["Weekly"]["delta_points"] == 1.5 and deltas["5-hour"]["delta_points"] == 2.0
    reset = window_deltas(_runs.fleet(90.0), _runs.fleet(1.0, resets="2026-11-15T00:00:00+00:00"))
    assert reset["Weekly"]["delta_points"] is None
    assert window_deltas(None, _runs.fleet(1.0))["Weekly"]["before_percent"] is None


# ------------------------------------------------------------------- scheduling


def test_schedule_stops_when_the_guard_says_so() -> None:
    decisions = iter(
        [GuardDecision(False, "ok"), GuardDecision(False, "ok"), GuardDecision(True, "full")]
    )
    ran: list[RunSpec] = []
    specs = [RunSpec("P1", i) for i in range(1, 6)]
    finished, stopped, never = schedule(
        specs, concurrency=1, guard=lambda: next(decisions), run=lambda s: ran.append(s) or {}
    )
    assert [s.index for s in ran] == [1, 2] and len(finished) == 2
    assert stopped is not None and stopped.reason == "full"
    assert [s.index for s in never] == [3, 4, 5]


def test_schedule_respects_concurrency_and_survives_failures() -> None:
    lock, active, peak = threading.Lock(), [0], [0]
    gate = threading.Barrier(2, timeout=10)

    def run(spec: RunSpec) -> dict[str, Any]:
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        if spec.index <= 2:
            gate.wait()
        with lock:
            active[0] -= 1
        if spec.index == 3:
            raise RuntimeError("boom")
        return {}

    specs = [RunSpec("P1", i) for i in range(1, 5)]
    finished, stopped, never = schedule(
        specs, concurrency=2, guard=lambda: GuardDecision(False, "ok"), run=run
    )
    assert peak[0] == 2 and stopped is None and not never
    assert sum(isinstance(o, RuntimeError) for _, o in finished) == 1


def test_plan_runs_skips_existing_results(tmp_path: Path) -> None:
    path = result_path(tmp_path, "base", "P2", 2)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    assert RunSpec("P2", 2) not in plan_runs(["P1", "P2"], 3, tmp_path, "base", force=False)
    assert len(plan_runs(["P1", "P2"], 3, tmp_path, "base", force=True)) == 6


def test_task_selection_and_prompts() -> None:
    assert tasks.select("P1,P3-P5,H*") == [
        "P1", "P3", "P4", "P5", "H1", "H2", "H3", "H4", "H5", "H6",
    ]  # fmt: skip
    assert tasks.select("all") == list(tasks.ALL_TASKS)
    with pytest.raises(KeyError):
        tasks.select("P42")
    manifest = {"tasks": {"P1": {"answer": {"function": "parse_prompt_text"}}}}
    assert "`parse_prompt_text`" in tasks.task("P1", manifest).prompt
    p6 = tasks.task("P6", manifest)
    assert p6.agent_delta() == {"add": ["clio-eval/eval-records-200"], "remove": []}
    h3 = tasks.task("H3", manifest, h3_autocompact_pct=0.2)
    assert h3.metadata["context_preferences"]["autocompact_pct"] == 0.2
    h5 = tasks.task("H5", manifest, h3_autocompact_pct=0.2)
    assert h5.prompt == tasks.task("H4", manifest).prompt
    assert h5.metadata == h3.metadata and not tasks.task("H4", manifest).metadata
    for task_id in ("P3", "P5", "P8", "P9", "P10", "H1", "H2", "H4"):
        prompt = tasks.task(task_id, manifest).prompt.lower()
        assert not any(word in prompt for word in ("search_tools", "run_code", "fs_", "tool"))


def test_h4_prompt_names_no_file_step_or_finding() -> None:
    prompt = tasks.task("H4", {}).prompt.lower()
    for word in (
        "readme", "manifest", ".csv", ".h5", "parquet", "hdf5", ".json", "log", "script",
        "calibrat", "unit", "duplicate", "clock", "far-red", "dark", "chamber", "run_",
    ):  # fmt: skip
        assert word not in prompt, word


def test_p9_prompt_shares_no_word_with_its_tool() -> None:
    words = set(tasks.task("P9", {}).prompt.lower().replace(".", "").split())
    assert not words & set("irrigation_run_zone".split("_"))


# ------------------------------------------------------------------ turn waiting


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeClient:
    """Answers GactClient calls from a script of session states."""

    base_url = "http://fake"

    def __init__(self, statuses: list[str], final: dict[str, Any] | None = None) -> None:
        self.statuses = statuses
        self.polls = 0
        self.final = final or {
            "id": "m2",
            "role": "assistant",
            "stop_reason": "end_turn",
            "updated_at": "t",
        }
        self.calls: list[tuple[str, Any]] = []
        self.pending: list[dict[str, Any]] = []

    def pending_permissions(self) -> list[dict[str, Any]]:
        return list(self.pending)

    def respond_permission(self, pid: str, action: str) -> None:
        self.calls.append(("permission", (pid, action)))
        self.pending = [row for row in self.pending if row["id"] != pid]

    def session(self, sid: str) -> dict[str, Any]:
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        return {"id": sid, "status": status, "message_count": 2}

    def messages(self, sid: str, limit: int = 5) -> list[dict[str, Any]]:
        idle = self.statuses[min(self.polls - 1, len(self.statuses) - 1)] == "idle"
        return [self.final] if idle and self.polls > 1 else []

    # -- the rest of a run
    def workspaces(self) -> list[dict[str, Any]]:
        return []

    def create_workspace(self, name: str, root: str) -> dict[str, Any]:
        self.calls.append(("workspace", root))
        return {"id": "ws_1", "root_path": root}

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("session", body))
        return {"id": "sess_root"}

    def fleet(self) -> dict[str, Any]:
        self.calls.append(("fleet", None))
        return _runs.fleet(40.0 + len([c for c in self.calls if c[0] == "fleet"]))

    def send(self, sid: str, text: str) -> dict[str, Any]:
        self.calls.append(("send", text))
        return {"message_id": "m1", "state": "started"}

    def export(self, sid: str) -> dict[str, Any]:
        return _runs.export()

    def cancel(self, sid: str) -> None:
        self.calls.append(("cancel", sid))


def _wait(client: FakeClient, clock: _Clock, **kw: float) -> dict[str, Any]:
    args = {"poll_s": 2.0, "settle_s": 6.0, "timeout_s": 100.0, "interaction_timeout_s": 30.0, **kw}
    return wait_for_turn(client, "s", clock=clock, sleep=clock.sleep, **args)  # type: ignore[arg-type]


def test_wait_settles_after_a_quiet_period() -> None:
    clock = _Clock()
    client = FakeClient(["idle", "running", "running", "idle", "idle"])
    result = _wait(client, clock)
    assert result["state"] == "settled" and result["polls"] == 5
    assert clock.now == pytest.approx(2.0 + 2.0 + 2.0 + 6.0)


def test_wait_restarts_when_a_new_turn_begins_while_settling() -> None:
    clock = _Clock()
    client = FakeClient(["running", "idle", "running", "idle", "idle"])
    assert _wait(client, clock)["state"] == "settled"
    assert client.polls == 5


def test_wait_gives_up_on_a_pending_question_and_on_timeout() -> None:
    clock = _Clock()
    assert _wait(FakeClient(["running", "waiting_user"]), clock)["state"] == "waiting_user"
    assert clock.now <= 40.0
    assert _wait(FakeClient(["running"]), _Clock(), timeout_s=10.0)["state"] == "timed_out"


def test_a_childs_question_is_declined_after_the_wait_not_left_to_time_out() -> None:
    """A child waiting on a permission question leaves the root "running"; the run
    answers it ``deny`` after the wait, as a person who says no would, and records it.
    SABOTAGE: drop the ``_decline_waiting`` call and nothing is answered."""
    clock = _Clock()
    client = FakeClient(["running"] * 8 + ["idle", "idle"])
    client.pending = [{"id": "perm_1", "session_id": "sess_child", "tool": "fs_search_text"}]
    result = _wait(client, clock, decline_after_s=5.0)
    assert ("permission", ("perm_1", "deny")) in client.calls
    [row] = result["declined"]
    assert row["id"] == "perm_1" and row["session_id"] == "sess_child" and row["waited_s"] >= 5.0
    assert result["state"] == "settled"


# ----------------------------------------------------------------- a full run


def test_execute_run_writes_a_result_that_metrics_grade_and_summarize_read(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "p2").mkdir(parents=True)
    (fixtures / "p2" / "station_registry.py").write_text("X = 1\n")
    manifest = {
        "tasks": {
            "P2": {
                "fixture": "p2",
                "answer": {"file": "station_registry.py", "line": 1, "value": 42, "name": "X"},
            }
        }
    }
    (fixtures / "manifest.json").write_text(json.dumps(manifest))
    client = FakeClient(["running", "idle", "idle"])
    ctx = RunContext(
        client=client,  # type: ignore[arg-type]
        fixtures=fixtures,
        manifest=manifest,
        results=tmp_path / "results",
        work_root=tmp_path / "work",
        config="base",
        poll_s=0.0,
        settle_s=0.0,
        fleet_settings={"roles": {}},
    )
    result = execute_run(ctx, RunSpec("P2", 1))
    stored = json.loads(result_path(ctx.results, "base", "P2", 1).read_text())
    assert stored["session_id"] == "sess_root" and stored["handles"] == ["sess_root", "sess_child"]
    assert stored["outcome"]["final"] is True
    assert stored["usage_windows"]["Weekly"]["delta_points"] == 1.0
    assert (tmp_path / "work" / "base" / "P2" / "run-1" / "station_registry.py").is_file()
    body = dict(client.calls)["session"]
    assert body["metadata"]["agent_ref"] == "clio/standard"
    assert body["model"] == {"provider_id": "claude_code", "model_id": "claude-sonnet-5-5"}
    assert body["approval_mode"] == "bypass"
    # The synthetic export's clock is months behind: the poll time is the end.
    assert result["wall_s"] >= 0 and result["wall_s_source"] == "poll"
    table = render(load_runs(ctx.results), manifest)
    row = next(line for line in table.splitlines() if line.startswith("| P2 | base |"))
    # 3 root + 2 child steps, 4 tool calls; no stream audit, so no reset count.
    assert row.startswith("| P2 | base | 5 (1/1 valid) | 0.80 | 322 / 12,600 | 191 |")
    assert "| 350 / 1,400 ms | 1.00 | 433 ms | — |" in row
    assert "| 0/1 | `sess_root` |" in row  # the value is named, its line is not
    assert "| P2 (base) |" in table


def test_client_waits_out_a_booting_server() -> None:
    answers = iter(
        [
            (503, b'{"error": {"error": "task_runtime_starting", "recoverable": true}}'),
            (503, b'{"error": {"error": "task_runtime_starting", "recoverable": true}}'),
            (200, b'{"marketplaces": [{"name": "clio"}]}'),
        ]
    )
    slept: list[float] = []
    client = GactClient(
        "http://x", transport=lambda *a: next(answers), sleep=slept.append, starting_wait_s=10
    )
    assert client.marketplaces() == [{"name": "clio"}]
    assert slept == [2.0, 2.0]
    stuck = GactClient(
        "http://x",
        transport=lambda *a: (
            503,
            b'{"error": {"error": "task_runtime_starting", "recoverable": true}}',
        ),
        sleep=lambda s: None,
        starting_wait_s=4,
    )
    with pytest.raises(GactError):
        stuck.health()
    other = GactClient(
        "http://x", transport=lambda *a: (503, b'{"error": "down"}'), sleep=slept.append
    )
    with pytest.raises(GactError):
        other.health()


def test_client_raises_typed_errors() -> None:
    def transport(method: str, url: str, headers: dict, data: Any, timeout: float):
        assert headers["Authorization"] == "Bearer secret"
        return 422, json.dumps({"detail": {"error": {"error": "agent_unavailable"}}}).encode()

    client = GactClient("http://127.0.0.1:8787/", token="secret", transport=transport)
    with pytest.raises(GactError) as caught:
        client.create_session({})
    assert caught.value.status == 422 and "agent_unavailable" in str(caught.value)


class _DeadServer(FakeClient):
    def session(self, sid: str) -> dict[str, Any]:
        raise TimeoutError("timed out")


def test_a_run_whose_server_stops_answering_cancels_its_turn(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "p2").mkdir(parents=True)
    answer = {"file": "station_registry.py", "line": 1, "value": 42, "name": "X"}
    manifest = {"tasks": {"P2": {"fixture": "p2", "answer": answer}}}
    client = _DeadServer(["running"])
    ctx = RunContext(
        client=client,  # type: ignore[arg-type]
        fixtures=fixtures,
        manifest=manifest,
        results=tmp_path / "results",
        work_root=tmp_path / "work",
        config="base",
        poll_s=0.0,
        settle_s=0.0,
        fleet_settings={"roles": {}},
    )
    with pytest.raises(TimeoutError):
        execute_run(ctx, RunSpec("P2", 1))
    assert ("cancel", "sess_root") in client.calls


def test_the_shared_root_is_emptied_except_the_call_log(tmp_path: Path) -> None:
    from scripts.eval.tool_calling.session_run import reset_shared_root

    root = tmp_path / "roots"
    (root / "calls").mkdir(parents=True)
    (root / "calls" / "eval-calls.jsonl").write_text("{}\n")
    (root / "old_check" / "pkg").mkdir(parents=True)
    (root / "note.txt").write_text("x")
    removed = reset_shared_root(root, root / "calls" / "eval-calls.jsonl")
    assert removed == ["note.txt", "old_check"]
    assert sorted(p.name for p in root.iterdir()) == ["calls"]
    assert (root / "calls" / "eval-calls.jsonl").read_text() == "{}\n"


def test_io_summary_counts_this_runs_puts_and_cache_tokens(tmp_path: Path) -> None:
    from scripts.eval.tool_calling.session_run import summarize_io

    log = tmp_path / "audit.jsonl"
    rows = [
        {"stage": "store.put", "ts": 100.0, "name": "sess_a__~events~", "size": 4096},
        {"stage": "store.put", "ts": 101.0, "name": "r.sess_a.x", "size": 100},
        {"stage": "store.put", "ts": 101.0, "name": "sess_other__~events~", "size": 9},
        {"stage": "store.put", "ts": 500.0, "name": "sess_a__late", "size": 9},
        {
            "stage": "provider.call_usage",
            "ts": 102.0,
            "session_id": "sess_a",
            "usage_cache_creation_input_tokens": 300,
            "usage_cache_read_input_tokens": 9000,
        },
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    io = summarize_io(log, 99.0, 110.0, {"sess_a"})
    assert {
        k: io[k]
        for k in ("store_puts", "store_put_bytes", "cache_creation_tokens", "cache_read_tokens")
    } == {
        "store_puts": 2,
        "store_put_bytes": 4196,
        "cache_creation_tokens": 300,
        "cache_read_tokens": 9000,
    }
    assert summarize_io(None, 0.0, 1.0, {"sess_a"})["store_puts"] == 0


def test_a_follow_up_is_sent_mid_run_and_the_run_waits_for_it() -> None:
    """H6: the person's change arrives while the work runs; a turn that ends before the
    change is due is not the end of the run. SABOTAGE: drop the ``if pending`` wait in
    ``wait_for_turn`` and the run settles before the change is sent."""
    clock = _Clock()
    client = FakeClient(["running", "idle", "idle", "idle", "idle", "idle", "idle", "idle"])
    result = wait_for_turn(
        client,  # type: ignore[arg-type]
        "s",
        poll_s=2.0,
        settle_s=6.0,
        timeout_s=100.0,
        interaction_timeout_s=30.0,
        follow_ups=((30.0, "Change of plan."),),
        clock=clock,
        sleep=clock.sleep,
    )
    assert result["state"] == "settled"
    assert [text for kind, text in client.calls if kind == "send"] == ["Change of plan."]
    assert [s["text"] for s in result["sent"]] == ["Change of plan."]
    assert result["sent"][0]["after_s"] >= 30.0


def test_summarize_io_sums_clio_core_time_for_the_run(tmp_path: Path) -> None:
    from scripts.eval.tool_calling.session_run import summarize_io

    log = tmp_path / "audit.jsonl"
    rows = [
        {"stage": "store.rpc", "ts": 10.0, "op": "put", "ms": 4.0},
        {"stage": "store.rpc", "ts": 11.0, "op": "get", "ms": 9.0},
        {"stage": "core_io.job", "ts": 12.0, "wait_ms": 30.0, "run_ms": 5.0},
        {"stage": "highway.flush", "ts": 13.0, "session_id": "s1", "ms": 7.0},
        {"stage": "highway.flush", "ts": 13.0, "session_id": "other", "ms": 99.0},
        {"stage": "store.rpc", "ts": 500.0, "op": "put", "ms": 50.0},  # another run
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))
    io = summarize_io(log, 9.0, 20.0, {"s1"})
    assert (io["rpc_count"], io["rpc_ms"], io["rpc_max_ms"]) == (2, 13.0, 9.0)
    assert (io["writer_jobs"], io["writer_wait_ms"], io["writer_run_ms"]) == (1, 30.0, 5.0)
    assert (io["barrier_ms"], io["barrier_max_ms"]) == (7.0, 7.0)
