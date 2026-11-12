"""Working-state reminders: session facts, the plan, the todos and plan mode as templates.

Each built-in template renders exactly the text the code built before the migration
(prompt caching keys on it), and switching a reminder off stops its injection
(docs/design/sdk/reminders.md).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import injections
from clio_agent.gact.agents.composition import _runtime_active_workspace_context
from clio_agent.gact.app import build_app
from clio_agent.gact.plan_mode import PLAN_MODE_REMINDER_MARKER, plan_mode_reminder_block
from clio_agent.gact.planning import (
    PLAN_VARIANT_SMALL,
    PLAN_VARIANT_WORKFLOW,
    Playbook,
    PlaybookStep,
    plan_variant_guidance,
)
from clio_agent.gact.session_plan import PERSON_EDIT_KEY, plan_injection, update_plan
from clio_agent.gact.todos import TODO_RECITATION_MARKER, todo_recitation
from clio_agent.gact.working_state import working_state_injections

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_ROOT = "/w/project"
_PLAN_FILE = "/tmp/plans/x.md"

# --------------------------------------------------------------------------- #
# session_facts                                                                #
# --------------------------------------------------------------------------- #

_WORKSPACE_FACTS = (
    "Active workspace root: /w/project\n"
    "Write generated artifacts (files, charts, exports) inside the active workspace root "
    "using absolute paths so they stay with the session. Read inputs from wherever the user "
    "points you.\n"
    "Writes are only permitted under: /w/project, /w/shared.\n"
    "Machine: node-a\n"
    "Scratch folder for this session: /s/sess_f. It is yours alone and the person never "
    'sees it: their files, and "this folder", are the workspace. Use it for your own '
    "temporary files (extracted copies, intermediate results); it is removed after the "
    "session."
)
_NODE_FACTS = (
    "Active workspace root: /w/project\n"
    "Your node folder: /w/project/run/step\n"
    "This session is one node of an isolated workflow run. Your shell starts in your node "
    "folder, and relative output paths land there. You may write only inside it; you may "
    "read the whole workspace.\n"
    "Machine: node-a"
)


@pytest.fixture
def facts(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The session's facts with fixed values; ``node``/``scratch`` choose the branch."""
    from clio_agent.gact import node_folders
    from clio_agent.gact.agents import composition, resolution
    from clio_agent.runtime import session_scratch

    world: dict[str, Any] = {"node": "", "scratch": "/s/sess_f"}
    monkeypatch.setattr(
        resolution, "_runtime_workspace_catalog_cwd", lambda app, *, session_id: _ROOT
    )
    monkeypatch.setattr(composition, "_session_write_roots", lambda root: [_ROOT, "/w/shared"])
    monkeypatch.setattr(node_folders, "session_node_folder", lambda app, sid: world["node"])
    monkeypatch.setattr(session_scratch, "machine_name", lambda: "node-a")
    monkeypatch.setattr(session_scratch, "session_scratch_dir", lambda sid: world["scratch"])
    return world


def _facts() -> str:
    return _runtime_active_workspace_context(SimpleNamespace(state=None), session_id="sess_f")


def test_session_facts_render_todays_text(facts: dict[str, Any]) -> None:
    """SABOTAGE: drop the ``\\n`` join (or a piece) in ``composition`` and this fails."""
    assert _facts() == _WORKSPACE_FACTS
    facts.update(node="/w/project/run/step", scratch="")
    assert _facts() == _NODE_FACTS


def test_switching_session_facts_off_stops_them(facts: dict[str, Any]) -> None:
    """The opening line is the source's switch; a piece's switch drops only that piece.
    SABOTAGE: ignore ``head is None`` in ``composition`` and the first assertion fails."""
    injections.update("session_facts", active=False)
    assert _facts() == ""
    injections.update("session_facts", active=True)
    injections.update("session_facts_scratch", active=False)
    assert _facts() == _WORKSPACE_FACTS.rpartition("\n")[0]


# --------------------------------------------------------------------------- #
# plan                                                                         #
# --------------------------------------------------------------------------- #

_PLAN_TEXT = "# Objective\nFind the regression."


@pytest.fixture
def plan_session(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "hunt"}).json()["id"]
        update_plan(app, sid, content=_PLAN_TEXT)
        yield app, sid


def _plan(app: Any, sid: str) -> tuple[str, str]:
    session = app.state.sessions.get(sid)
    return plan_injection(app, sid, session), session.metadata["plan_file"]


def test_the_plan_renders_todays_text(plan_session: Any) -> None:
    app, sid = plan_session
    text, path = _plan(app, sid)
    assert text == (
        f"## The plan (active, {path})\n\n"
        "If this plan is relevant to the current work and not finished, continue on it, and "
        "update it with plan_update when your understanding or direction changes.\n\n"
        f"{_PLAN_TEXT}"
    )
    app.state.sessions.update(sid, metadata_patch={PERSON_EDIT_KEY: True})
    text, _ = _plan(app, sid)
    assert text == (
        f"## The plan, as the person edited it ({path})\n\n"
        "The person changed the plan on the Work rail. Treat their changes as their "
        "direction: continue from this version.\n\n"
        f"{_PLAN_TEXT}"
    )


def test_a_long_plan_ends_with_todays_note(
    plan_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop the ``\\n\\n`` before the note in ``session_plan`` and this fails."""
    # The window's share for one result is 3 tokens: the plan is cut after 12 characters.
    monkeypatch.setattr("clio_agent.tools.result_room.window_share_tokens", lambda: 3)
    app, sid = plan_session
    text, path = _plan(app, sid)
    assert text.endswith(
        f"# Objective\n\n[The plan continues; read it whole at {path} with fs_read_file.]"
    )


def test_switching_the_plan_off_stops_it(plan_session: Any) -> None:
    """SABOTAGE: return the text when ``render`` gives ``None`` and the plan stays."""
    app, sid = plan_session
    session = app.state.sessions.get(sid)
    assert "plan" in dict(working_state_injections(app, sid, session))
    injections.update("plan", active=False, app=app)
    assert "plan" not in dict(working_state_injections(app, sid, session))


def test_with_the_person_edit_note_off_the_plan_is_offered_as_usual(plan_session: Any) -> None:
    """SABOTAGE: return ``""`` instead of falling through in ``plan_injection`` and this fails."""
    app, sid = plan_session
    app.state.sessions.update(sid, metadata_patch={PERSON_EDIT_KEY: True})
    injections.update("plan_person_edit", active=False, app=app)
    text, path = _plan(app, sid)
    assert text.startswith(f"## The plan (active, {path})")


# --------------------------------------------------------------------------- #
# todos                                                                        #
# --------------------------------------------------------------------------- #


def _todo_session() -> Any:
    todos = [
        {"content": "read the log", "status": "completed"},
        {"content": "bisect", "status": "in_progress"},
        {"content": "write the fix", "status": "pending"},
    ]
    return SimpleNamespace(mode="edit", metadata={"todos": todos})


def test_the_todos_render_todays_text() -> None:
    assert todo_recitation(None, "sess_t", _todo_session()) == (
        "## Todo checklist (your current task list — reconcile before finishing)\n\n"
        "[x] read the log\n[~] bisect\n[ ] write the fix"
    )
    assert todo_recitation(None, "sess_t", _todo_session()).startswith(TODO_RECITATION_MARKER)


def test_switching_the_todos_off_stops_them() -> None:
    """SABOTAGE: return the template's text regardless of ``render`` and this fails."""
    injections.update("todos", active=False)
    assert todo_recitation(None, "sess_t", _todo_session()) == ""


# --------------------------------------------------------------------------- #
# plan_mode (the default full and short blocks are pinned in test_plan_variants)  #
# --------------------------------------------------------------------------- #

_CONTRACT_TAIL = (
    "- The plan file holds your plan, not the deliverable the person asked for: it says how "
    "the requested result will be made and checked after approval. The deliverable's names "
    "and places come from the person's request; the plan file's path is CLIO's own.\n"
    "- Keep what you know apart from what you still must find out: findings carry their "
    "references, and what is unknown stays an open thread until you look it up.\n"
    "- If a plan already exists, judge whether it still fits this task before editing it; "
    "a new task gets a fresh plan.\n"
    "- Show the plan to the person in your reply as well; they read your reply, not the file.\n"
    "- Ending the turn: when the plan is complete, call plan_exit to hand the saved plan back "
    "for approval and end your turn there. Nothing is carried out in plan mode; the work "
    "starts after approval."
)
_CONTRACT_HEAD = (
    "## Plan Mode active — read-only except the plan file\n\n"
    "You are in plan mode. Investigate freely; nothing else changes until the plan is "
    "approved, so every write, edit and file-mutating tool is blocked except plan_update.\n"
    "- A plan file already exists at /tmp/plans/x.md. Update it with plan_update as you "
    "learn (old and new replace one passage).\n"
)


def test_the_plan_variants_render_todays_text() -> None:
    """SABOTAGE: drop ``extra_sources`` from the bullets in ``planning`` and this fails."""
    workflow = plan_mode_reminder_block(
        full=True,
        plan_file=_PLAN_FILE,
        exists=True,
        guidance=plan_variant_guidance(PLAN_VARIANT_WORKFLOW),
    )
    assert workflow == (
        _CONTRACT_HEAD + "- Structure the plan as an explicit, numbered implementation "
        "workflow: number every step in order, give each step its own concrete verification, "
        "and make cross-step ordering and dependencies explicit.\n"
        "- Add a Risks & Dependencies section to the plan: the risks, external/ordering "
        "dependencies, and a rollback or mitigation for each.\n" + _CONTRACT_TAIL
    )
    small = plan_mode_reminder_block(
        full=True,
        plan_file=_PLAN_FILE,
        exists=True,
        guidance=plan_variant_guidance(PLAN_VARIANT_SMALL),
    )
    assert small == (
        _CONTRACT_HEAD + "- Keep the plan short and lightweight — a brief Changes list plus a "
        "one-line Verification; do not build heavy scaffolding for a small task.\n" + _CONTRACT_TAIL
    )


def test_the_playbook_renders_todays_text() -> None:
    playbook = Playbook(
        name="triage",
        steps=(
            PlaybookStep(name="Reproduce", guidance="run the failing test"),
            PlaybookStep(name="Fix", tools_allowed=("fs_edit_file", "shell_*")),
        ),
    )
    block = plan_mode_reminder_block(
        full=True, plan_file=_PLAN_FILE, exists=True, playbook=playbook
    )
    assert block == (
        _CONTRACT_HEAD + '- Follow the operator playbook "triage" as the REQUIRED plan '
        "structure — author one plan section per numbered step below, in order; do not "
        "substitute your own top-level structure:\n"
        "  1. Reproduce — run the failing test\n"
        "  2. Fix (tools_allowed: fs_edit_file, shell_*)\n" + _CONTRACT_TAIL
    )


def test_the_plan_mode_contract_cannot_be_switched_off() -> None:
    """It is the agent's only signal that writes are blocked and plan_exit ends the mode."""
    injections.update("plan_mode", active=False)
    injections.update("plan_mode_playbook", active=False)
    full = plan_mode_reminder_block(full=True, plan_file=_PLAN_FILE, exists=False)
    assert full.startswith(PLAN_MODE_REMINDER_MARKER + "\n\nYou are in plan mode.")
    assert injections.active("plan_mode") and injections.active("plan_mode_playbook")


def test_switching_a_plan_mode_piece_off_drops_only_that_piece() -> None:
    """SABOTAGE: keep empty bullets in ``plan_mode_reminder_block`` and the blank line shows."""
    injections.update("plan_mode_structure", active=False)
    full = plan_mode_reminder_block(full=True, plan_file=_PLAN_FILE, exists=True)
    assert full == _CONTRACT_HEAD + _CONTRACT_TAIL
    injections.update("plan_mode_short", active=False)
    assert plan_mode_reminder_block(full=False, plan_file=_PLAN_FILE, exists=True) == ""


def test_the_marker_constants_open_the_builtin_templates() -> None:
    """Tests and greps detect the blocks by these headings; the templates must keep them."""
    short = plan_mode_reminder_block(full=False, plan_file=_PLAN_FILE, exists=True)
    assert short.startswith(PLAN_MODE_REMINDER_MARKER + " (")
    assert injections.injection("todos").text.startswith(TODO_RECITATION_MARKER + "\n\n")
