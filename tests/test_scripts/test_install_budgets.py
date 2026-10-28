"""Tests for the install matrix's enforced budgets and the crash test's comparison.

``scripts/ci/check_budgets.py`` holds the recorded install and startup numbers to
``scripts/ci/install_budgets.json``; ``scripts/crash_test.py`` compares what a restarted
server reports with what the API acknowledged before a SIGKILL. Both are pure over their
inputs, so they are tested here without a server.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


budgets_check = _load("check_budgets", SCRIPTS / "ci" / "check_budgets.py")
crash = _load("crash_test", SCRIPTS / "crash_test.py")

BUDGETS = json.loads((SCRIPTS / "ci" / "install_budgets.json").read_text(encoding="utf-8"))


def _row(name: str, seconds: float, outcome: str = "ok") -> dict[str, Any]:
    return {
        "name": name,
        "os": "Linux x86_64",
        "seconds": seconds,
        "exit_code": 0,
        "outcome": outcome,
    }


def test_every_budget_names_its_limit_and_what_was_measured() -> None:
    for name, budget in BUDGETS["timings"].items():
        assert budget["seconds"] > 0, name
        assert budget["budget"] and budget["measured"], name
    for filename, limits in BUDGETS["startup"].items():
        assert filename.startswith("startup-"), filename
        for key, limit in limits.items():
            assert "max" in limit or "equals" in limit, key
    for key, limit in BUDGETS["desktop"].items():
        assert limit["max"] > 0 and limit["budget"], key


def test_the_user_budgets_are_the_limits() -> None:
    assert BUDGETS["startup"]["startup-x1.json"]["task_runtime_ready_s"]["max"] == 30
    assert BUDGETS["desktop"]["backend_ready_seconds"]["max"] == 60
    installs = [row for name, row in BUDGETS["timings"].items() if "install" in name]
    assert installs and all(row["seconds"] == 180 for row in installs)


def test_a_step_within_its_budget_passes_and_an_unbudgeted_step_is_report_only() -> None:
    rows = [
        _row("source: fresh install", 90.0),
        _row("source: stop", 2.0),
        _row("source: re-run over an install", 0.0, "failed"),
    ]
    checked = budgets_check.check_timings(rows, BUDGETS["timings"])
    assert all(row[-1] == "ok" for row in checked)
    assert not any(row[1] == "source: re-run over an install" for row in checked)


def test_a_step_over_its_budget_or_failing_fails_with_the_number() -> None:
    limit = BUDGETS["timings"]["source: fresh install"]["seconds"]
    rows = [_row("source: fresh install", limit + 1), _row("source: stop", 1.0, "failed")]
    verdicts = {row[1]: row[-1] for row in budgets_check.check_timings(rows, BUDGETS["timings"])}
    assert verdicts["source: fresh install"] == (
        f"FAIL: budget 'install < 180 s' exceeded: measured {limit + 1} s, limit {limit} s"
    )
    assert verdicts["source: stop"].startswith("FAIL: budget 'shutdown < 20 s': the step failed")


def test_a_required_step_that_never_reported_fails() -> None:
    checked = budgets_check.check_timings([], BUDGETS["timings"], "script")
    missing = {row[1] for row in checked if "not measured" in row[-1]}
    assert {"source: fresh install", "source: stop"} <= missing
    assert budgets_check.check_timings([], BUDGETS["timings"], "desktop") == []


def test_startup_budgets_read_the_ready_mark_and_the_shutdown(tmp_path: Path) -> None:
    result = {
        "marks": {"task_runtime_ready": 12.5},
        "shutdown_s": 30.0,
        "shutdown_forced": False,
        "daemon_left_running": False,
    }
    (tmp_path / "startup-x1.json").write_text(json.dumps(result), encoding="utf-8")
    verdicts = {
        row[1]: row[-1] for row in budgets_check.check_startup(tmp_path, BUDGETS["startup"])
    }
    assert verdicts["task_runtime_ready_s"] == "ok"
    assert "measured 30.0 s" in verdicts["shutdown_s"]
    assert verdicts["shutdown_forced"] == "ok"


def test_a_missing_startup_result_fails(tmp_path: Path) -> None:
    checked = budgets_check.check_startup(tmp_path, BUDGETS["startup"])
    assert checked and all("not measured" in row[-1] for row in checked)


def test_desktop_launch_receipts_are_held_to_the_launch_budget(tmp_path: Path) -> None:
    for launch, seconds in (("first", 41.0), ("second", 75.0)):
        (tmp_path / f"evidence-{launch}").mkdir()
        receipt = {"backend_ready_seconds": seconds}
        (tmp_path / f"evidence-{launch}" / "receipt.json").write_text(json.dumps(receipt))
    verdicts = {
        row[2]: row[-1] for row in budgets_check.check_desktop(tmp_path, BUDGETS["desktop"])
    }
    assert verdicts["first launch"] == "ok"
    assert verdicts["second launch"].startswith("FAIL: budget 'desktop launch to ready < 60 s'")


def test_a_missing_first_launch_receipt_fails(tmp_path: Path) -> None:
    checked = budgets_check.check_desktop(tmp_path, BUDGETS["desktop"])
    assert [row[2] for row in checked] == ["first launch"]
    assert "not measured" in checked[0][-1]


def test_the_matrix_enforces_the_budgets_and_runs_the_crash_test() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "install-matrix.yml").read_text("utf-8")
    assert "check_budgets.py --startup results" in workflow
    assert 'check_budgets.py --job script --timings "$TIMINGS"' in workflow
    assert '--desktop "$RUNNER_TEMP"' in workflow
    assert "scripts/crash_test.py" in workflow


class _Api:
    """A restarted server's answers, by path."""

    def __init__(self, routes: dict[str, tuple[int, Any]]) -> None:
        self.routes = routes

    def call(self, method: str, path: str, body: Any = None, timeout: float = 0) -> Any:
        return self.routes.get(path, (404, None))

    def messages(self, sid: str) -> Any:
        status, body = self.call("GET", f"/sessions/{sid}/messages")
        return body["messages"] if status == 200 else None

    def tasks(self, sid: str) -> Any:
        status, body = self.call("GET", f"/sessions/{sid}/tasks/query")
        return body["tasks"] if status == 200 else None


def _message(mid: str, role: str, text: str) -> dict[str, Any]:
    return {"id": mid, "role": role, "parts": [{"type": "text", "text": text}], "metadata": {}}


def test_the_crash_comparison_reports_each_kind_of_loss() -> None:
    ledger = crash.Ledger()
    ledger.sessions = {"sess_a": "a", "sess_gone": "gone"}
    ledger.accepted = {
        "msg_kept": {"sid": "sess_a", "text": "hi", "delivery": "start"},
        "msg_steer": {"sid": "sess_a", "text": "more", "delivery": "steer"},
    }
    ledger.messages = {"msg_answer": {"sid": "sess_a", "role": "assistant", "text": "done"}}
    ledger.tasks = {
        "task_root": {"sid": "sess_a", "root": "sess_a", "status": "completed", "terminal": True},
        "task_child": {"sid": "sess_c", "root": "sess_a", "status": "active", "terminal": False},
    }
    api = _Api(
        {
            "/sessions/sess_a": (200, {"title": "a", "status": "idle"}),
            "/sessions/sess_a/messages": (
                200,
                {
                    "messages": [
                        _message("msg_kept", "user", "hi"),
                        _message("msg_answer", "assistant", ""),
                    ]
                },
            ),
            "/sessions/sess_a/tasks/query": (
                200,
                {"tasks": [{"handle": "task_root", "status": "active", "terminal_at": None}]},
            ),
        }
    )
    kinds = sorted(finding["type"] for finding in crash.verify(api, ledger, "restart"))
    assert kinds == [
        "mail_lost",
        "message_changed",
        "session_lost",
        "task_lost",
        "task_status_regressed",
    ]


def test_settled_work_still_running_is_a_liveness_finding_not_a_loss() -> None:
    ledger = crash.Ledger()
    ledger.tasks = {
        "task_root": {"sid": "sess_a", "root": "sess_a", "status": "active", "terminal": False}
    }
    api = _Api(
        {
            "/sessions/sess_a/tasks/query": (
                200,
                {"tasks": [{"handle": "task_root", "status": "active", "terminal_at": None}]},
            )
        }
    )
    findings = crash.verify(api, ledger, "settled")
    assert [finding["type"] for finding in findings] == ["not_settled"]
    assert findings[0]["type"] in crash.LIVENESS and findings[0]["type"] not in crash.LOSS
