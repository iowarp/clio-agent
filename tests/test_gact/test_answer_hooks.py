"""``Answer`` hooks see the turn's final answer and act on it, visibly.

A hook's note and check land as ``notice`` parts under the answer, attributed to the
hook; a replacement lands as a revision part while the original stays, marked
superseded. Driven through a real turn and the real subprocess and Python adapters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.hooks import install_global_dispatcher
from clio_agent.gact.hooks.config import HookEntry, HookMatch, HookRun
from clio_agent.gact.hooks.events import ANSWER
from clio_agent.gact.hooks.plugin_hooks import PythonHookAdapter
from clio_agent.gact.hooks.wire import HookEnvelope
from clio_agent.gact.protocol.v3.message import part_to_v3_block
from clio_agent.sdk import HOOK_EVENTS
from tests.test_gact._hook_fixtures import make_command_dispatcher

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "The mean is 289.91 K."
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str = "default") -> _Pred:
        return _Pred()


def _turn(tmp_path: Path, body: str) -> dict:
    install_global_dispatcher(make_command_dispatcher(tmp_path, event=ANSWER, body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as client:
            from .conftest import complete_turn

            sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
            return complete_turn(client, sid, "what is the mean?")
    finally:
        install_global_dispatcher(None)


def test_a_hook_sees_the_answer_and_its_note_and_check_land_under_it(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    body = (
        "import json, sys\n"
        "envelope = json.load(sys.stdin)\n"
        f"open({str(seen)!r}, 'w').write(json.dumps(envelope['payload']))\n"
        "print(json.dumps({'note': 'Checked against data.csv.',\n"
        "                  'check': {'passed': False, 'text': 'No source is cited.'}}))\n"
    )
    message = _turn(tmp_path, body)
    assert json.loads(seen.read_text())["answer"] == "The mean is 289.91 K."
    notices = [p for p in message["parts"] if p["type"] == "notice"]
    assert [(n["source"], n["code"], n["text"]) for n in notices] == [
        ("answer_hook", "note", "Checked against data.csv."),
        ("answer_hook", "check_failed", "No source is cited."),
    ]
    texts = [p for p in message["parts"] if p["type"] == "text"]
    assert [t["text"] for t in texts] == ["The mean is 289.91 K."]


def test_a_replacement_lands_as_a_visible_revision_and_the_original_stays(
    tmp_path: Path,
) -> None:
    body = (
        "import json\n"
        "print(json.dumps({'updatedAnswer': 'The mean is 289.91 K (from data.csv).',\n"
        "                  'reason': 'added the source'}))\n"
    )
    message = _turn(tmp_path, body)
    original, notice, revision = [p for p in message["parts"] if p["type"] in ("text", "notice")]
    assert (notice["source"], notice["code"], notice["text"]) == (
        "answer_hook",
        "revised",
        "added the source",
    )
    assert notice["metadata"] == {"hook": "test-hook", "revises": original["id"]}
    # The client's block keeps both, so it can fold the original behind the notice.
    assert part_to_v3_block(notice)["metadata"] == notice["metadata"]
    assert original["text"] == "The mean is 289.91 K."
    assert revision["text"] == "The mean is 289.91 K (from data.csv)."
    assert revision["metadata"]["revises"] == original["id"]
    assert revision["metadata"]["revised_by"] == "test-hook"
    assert revision["metadata"]["reason"] == "added the source"
    assert original["metadata"]["superseded_by"] == revision["id"]


def test_a_hook_that_returns_nothing_adds_nothing(tmp_path: Path) -> None:
    message = _turn(tmp_path, "import sys\nsys.exit(0)\n")
    assert [p["type"] for p in message["parts"]] == ["text"]


def test_an_sdk_answer_hook_reads_the_answer_and_returns_note_check_and_revision(
    tmp_path: Path,
) -> None:
    assert HOOK_EVENTS["answer"] == ANSWER
    hooks_dir = tmp_path / "ai.iowarp.clio" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "review.py").write_text(
        "from clio_agent.sdk import hook\n\n"
        "@hook('answer')\n"
        "def review(ev):\n"
        "    assert ev.answer == 'draft'\n"
        "    return {'note': 'n', 'check': {'passed': True, 'text': 'ok'}, 'answer': 'final'}\n",
        encoding="utf-8",
    )
    entry = HookEntry(
        id="p.review",
        on=frozenset({ANSWER}),
        match=HookMatch(),
        run=HookRun(type="python", command=str(tmp_path), args=("hooks/review.py", "review")),
    )
    envelope = HookEnvelope(hook_event_name=ANSWER, session_id="s", payload={"answer": "draft"})
    decision = PythonHookAdapter().invoke(entry, envelope)
    assert decision.note == "n"
    assert decision.check == {"passed": True, "text": "ok"}
    assert decision.updated_output == "final"
