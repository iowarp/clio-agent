"""The multi-turn science chains: loading, round sending, replies and grading."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

from scripts.eval.tool_calling import chains, tasks
from scripts.eval.tool_calling.collect import replies
from scripts.eval.tool_calling.grade import grade
from scripts.eval.tool_calling.session_run import wait_for_turn

_HEAD = [
    "chain_id",
    "round_no",
    "task_type",
    "prompt",
    "tool_ranges",
    "correct_answer",
    "explanation",
]


@pytest.fixture
def chain_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "chains.csv"
    rows = [
        ["c-a", "2", "cross_modality_peak", "A round 2", "", "0; DISAGREES", ""],
        ["c-b", "2", "transition_sensitivity", "B round 2", "", "ISU 262", ""],
        ["c-a", "1", "dose_response_shape", "A round 1", "", "40", ""],
        ["c-b", "1", "dose_response_shape", "B round 1", "", "80", ""],
        ["c-a", "3", "modality_coverage", "A round 3", "", "fc1, rgb2, msc1", ""],
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_HEAD)
        writer.writerows(rows)
    monkeypatch.setenv("CLIO_EVAL_CHAINS", str(path))
    return path


def test_a_chain_is_one_task_with_its_later_rounds_in_order(chain_csv: Path) -> None:
    """C1 is the chain the CSV names first; rounds follow ``round_no``, not row order."""
    first = tasks.task("C1", {})
    assert (first.prompt, first.rounds) == ("A round 1", ("A round 2", "A round 3"))
    assert first.metadata == {"chain_id": "c-a"}
    second = tasks.task("C2", {})
    assert (second.prompt, second.rounds) == ("B round 1", ("B round 2",))
    assert tasks.fixture_dir("C2", {}) == "home"
    assert tasks.select("C2,P1,C1-C2") == ["P1", "C2", "C1"]
    with pytest.raises(KeyError):
        tasks.task("C3", {})


@pytest.mark.parametrize(
    ("gold", "reply", "correct"),
    [
        ("40", "ANSWER: 40", True),
        ("40", "ANSWER: 400", False),
        ("40", "The peak is at 40 but ANSWER: 80", False),
        ("ISU 262", "**ANSWER:** isu 262", True),
        ("0; DISAGREES", "ANSWER: 0; disagrees", True),
        ("0; DISAGREES", "ANSWER: 0", False),
        ("fc1, rgb2, msc1", "ANSWER: msc1, fc1, rgb2", True),
        ("fc1, rgb2, msc1", "ANSWER: msc1, rgb2", False),
        ("69@50", "reasoning first\n69@50", True),
        ("0.5", "ANSWER: 0.50", True),
    ],
)
def test_a_round_is_graded_on_its_answer_line(gold: str, reply: str, correct: bool) -> None:
    assert chains.round_correct(gold, reply) is correct


def test_the_chain_is_correct_only_when_every_round_is(chain_csv: Path) -> None:
    run = {"replies": ["ANSWER: 40", "ANSWER: 0; DISAGREES", "ANSWER: rgb2, fc1, msc1"]}
    graded = grade("C1", {}, run)
    assert graded["graded"] and graded["correct"] and graded["round_recall"] == 1.0
    short = grade("C1", {}, {"replies": run["replies"][:2]})
    assert not short["correct"] and short["round_recall"] == pytest.approx(0.667)
    assert short["rounds"][2] == {
        "round": 3,
        "task_type": "modality_coverage",
        "gold": "fc1, rgb2, msc1",
        "answered": False,
        "correct": False,
    }


def test_replies_pair_each_request_with_its_last_answer() -> None:
    """Text from an earlier step of the same turn is replaced by the turn's last text;
    a request with no answer gets ``""``."""

    def msg(role: str, at: str, text: str = "") -> dict[str, Any]:
        parts = [{"type": "text", "text": text}] if text else [{"type": "tool_call"}]
        return {"role": role, "created_at": at, "parts": parts}

    messages = [
        msg("assistant", "05", "ANSWER: 40"),
        msg("user", "01", "round 1"),
        msg("assistant", "03", "thinking out loud"),
        msg("assistant", "04"),
        msg("user", "06", "round 2"),
        msg("user", "07", "round 3"),
        msg("assistant", "08", "ANSWER: x"),
    ]
    assert replies(messages) == ["ANSWER: 40", "", "ANSWER: x"]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Conversation:
    """A session that answers each request after a lag: for two polls after a send
    the old reply is still the newest (the turn has not started yet), then the turn
    runs for two polls, then the new reply lands."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.polls_since_send = 0
        self.count = 1
        self.reply_id = ""

    def send(self, sid: str, text: str) -> dict[str, Any]:
        self.sent.append(text)
        self.polls_since_send = 0
        self.count += 1
        return {"message_id": f"u{len(self.sent)}"}

    def session(self, sid: str) -> dict[str, Any]:
        self.polls_since_send += 1
        if 2 < self.polls_since_send <= 4:
            return {"status": "running", "message_count": self.count}
        if self.polls_since_send == 5:
            self.reply_id = f"a{len(self.sent)}"
            self.count += 1
        return {"status": "idle", "message_count": self.count}

    def messages(self, sid: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self.reply_id:
            return []
        return [{"id": self.reply_id, "role": "assistant", "stop_reason": "end_turn"}]


def test_each_round_waits_for_a_new_reply_to_the_previous_one() -> None:
    """SABOTAGE: drop the ``answered`` check and round 3 is sent while round 2's turn
    has not started, because the old reply looks settled."""
    clock = _Clock()
    conversation = _Conversation()
    conversation.send("s", "round 1")
    result = wait_for_turn(
        conversation,  # type: ignore[arg-type]
        "s",
        poll_s=2.0,
        settle_s=6.0,
        timeout_s=500.0,
        interaction_timeout_s=30.0,
        rounds=("round 2", "round 3"),
        clock=clock,
        sleep=clock.sleep,
    )
    assert result["state"] == "settled"
    assert conversation.sent == ["round 1", "round 2", "round 3"]
    assert conversation.reply_id == "a3"
    assert [s["round"] for s in result["sent"]] == [2, 3]


def test_the_exploration_task_runs_in_the_data_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X1 is a plain request on a real dataset, outside the generated fixtures: the
    fixtures root joined with its folder is the folder itself."""
    monkeypatch.setenv("CLIO_EVAL_OPAL_DATA", str(tmp_path))
    task = tasks.task("X1", {})
    assert task.suite == "long-horizon" and "tool" not in task.prompt.lower()
    folder = tasks.fixture_dir("X1", {})
    assert Path("/any/fixtures") / folder == tmp_path.resolve()
    assert tasks.select("X1,P1") == ["P1", "X1"]
    monkeypatch.setenv("CLIO_EVAL_OPAL_DATA", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        tasks.fixture_dir("X1", {})


def test_x2_asks_the_chain_from_the_files_and_grades_each_trap_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exp67 QY_max chain: four rounds, no data in the prompt, and each round's trap
    answer (a plain dip, plant means, absolute ranking) graded wrong. SABOTAGE: compare
    round 3's names as a set and the reordered trap list passes."""
    from scripts.eval.tool_calling.explore import grade_x2

    monkeypatch.setenv("CLIO_EVAL_OPAL_DATA", str(tmp_path))
    task = tasks.task("X2", {})
    assert len(task.rounds) == 3 and "ANSWER:" in task.prompt and "0.7" not in task.prompt
    right = [
        "ANSWER: 200",
        "Done.\nANSWER: 500 mg/kg",
        "ANSWER: ISU 210, 9, RIL 407",
        "ANSWER: 0.7434, 0.7552, 0.7414, 0.7905, 0.6598, 0.6593",
    ]
    assert grade_x2(right)["correct"] is True
    traps = [
        "ANSWER: 50",
        "ANSWER: 100",
        "ANSWER: 9, ISU 210, RIL 407",
        "ANSWER: 0.7451, 0.7576, 0.7414, 0.7934, 0.6626, 0.6606",
    ]
    graded = grade_x2(traps)
    assert [r["correct"] for r in graded["rounds"]] == [False, False, False, False]


def test_active_time_counts_the_turn_end_a_person_sees_not_the_last_model_output() -> None:
    """Round 2 is sent 10 s after the run starts; the first turn's model output ended
    at 4 s but the turn ended at 5 s: only the runner's 5 s are left out. SABOTAGE:
    measure from the last model call and this reads 14."""
    from scripts.eval.tool_calling.session_run import active_seconds

    sessions = [
        {"lm_calls": [{"ended_at": 104.0}, {"ended_at": 118.0}], "tool_calls": [],
         "turn_ends": [105.0, 119.5]},
    ]  # fmt: skip
    sent = [{"after_s": 10.0, "round": 2, "text": "next"}]
    assert active_seconds(100.0, 120.0, sent, sessions) == 15.0
    assert active_seconds(100.0, 120.0, [], sessions) == 20.0
    assert active_seconds(100.0, 120.0, sent, [{"turn_ends": []}]) == 20.0


def test_only_a_copy_of_external_data_is_dropped(tmp_path: Path) -> None:
    from scripts.eval.tool_calling.session_run import drop_external_copy

    external, generated = tmp_path / "external", tmp_path / "generated"
    external.mkdir()
    generated.mkdir()
    drop_external_copy("/data/opal", external)
    drop_external_copy("home", generated)
    assert not external.exists() and generated.exists()
