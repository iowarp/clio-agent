"""Reminders, batch B: the compaction and context injections as Markdown templates.

Each test pins today's text (the default rendering must be byte-identical, so prompt
caching and the older tests stay meaningful) and, where the person may switch a source
off, that switching it off stops the injection; a required source keeps its text
(docs/design/sdk/reminders.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import injections
from tests._result_room import pin_result_room

# --------------------------------------------------------------------------- #
# summarization, compaction_failed, compaction_skipped                        #
# --------------------------------------------------------------------------- #
_RECALL_SEARCH = (
    '[The steps this summary replaced are kept in full: recall_context(ids=["cmp_1"]) '
    'returns them byte-exact; recall_context(query="...") searches them.]'
)
_RECALL_IDS = (
    '[The steps this summary replaced are kept in full: recall_context(ids=["cmp_1"]) '
    "returns them byte-exact; text search is unavailable here. Use ids, not a query.]"
)


def test_the_summary_ends_with_todays_recall_line_and_it_cannot_be_switched_off() -> None:
    """SABOTAGE: change a word in ``summarization.md`` and the equality fails."""
    from clio_agent.gact.summarization_record import recall_line, summary_text

    assert recall_line("cmp_1") == _RECALL_SEARCH
    assert recall_line("cmp_1", search_enabled=False) == _RECALL_IDS
    assert summary_text("S.", "cmp_1") == f"S.\n\n{_RECALL_SEARCH}"
    for source in ("summarization", "summarization_ids_only"):
        assert injections.update(source, active=False).active  # required: data the agent needs
    assert summary_text("S.", "cmp_1", search_enabled=False) == f"S.\n\n{_RECALL_IDS}"


def _compaction_seams(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], list[Any]]:
    from clio_agent.gact import compaction, compaction_record

    notices: list[Any] = []
    events: list[Any] = []

    def write_notice(app: Any, sid: str, notice: Any) -> tuple[str, str]:
        notices.append(notice)
        return "msg_1", notice.id

    monkeypatch.setattr(compaction_record, "write_notice", write_notice)
    monkeypatch.setattr(compaction, "_event", lambda *args: events.append(args))
    return notices, events


def test_a_failed_compaction_notice_keeps_its_text_and_can_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off, no notice is written, and ``compaction.failed`` still names the failure.
    SABOTAGE: drop the ``text is None`` branch in ``compaction._failed`` and a notice
    without text is written."""
    from clio_agent.gact import compaction

    notices, events = _compaction_seams(monkeypatch)
    base = {"compaction_id": "cmp_1", "trigger": "auto", "scope": "main", "turn_id": "t"}
    error = compaction.CompactionError(502, "upstream_error", "the model timed out")
    compaction._failed(None, "s", base, error)
    assert [n.text for n in notices] == [
        "Summarizing the context failed, so it was left as it was. the model timed out"
    ]
    injections.update("compaction_failed", active=False)
    compaction._failed(None, "s", base, error)
    assert len(notices) == 1
    assert events[-1][2] == "failed" and events[-1][4]["part_id"] == ""
    assert events[-1][4]["error"] == {"code": "upstream_error", "message": "the model timed out"}


def test_a_skipped_compaction_notice_keeps_its_text_and_can_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: drop the ``text is None`` return in ``_notice_above_threshold`` and the
    switched-off notice is still written."""
    from clio_agent.gact import compaction

    today = (
        "Context is still over the summarizing threshold after summarizing; the next "
        "summary waits until more of the remaining room is used."
    )
    notices, _ = _compaction_seams(monkeypatch)
    assert compaction.ABOVE_THRESHOLD_NOTICE == today  # the constant still reads it
    compaction._notice_above_threshold(None, "s", "main", "cmp_1")
    assert [n.text for n in notices] == [today]
    injections.update("compaction_skipped", active=False)
    compaction._notice_above_threshold(None, "s", "main", "cmp_1")
    assert len(notices) == 1


# --------------------------------------------------------------------------- #
# context_recovery                                                            #
# --------------------------------------------------------------------------- #
_RECOVERED = (
    "Recovered this session's context from saved transcript; provider continuation is "
    "unavailable. No operation was replayed."
)


def _carry_over(monkeypatch: pytest.MonkeyPatch, ledger: list[Any]) -> tuple[Any, list[Any]]:
    from clio_agent.arc.history_plane import HistoryPlane
    from clio_agent.gact import context
    from clio_agent.gact.agents import clio_react_record
    from clio_agent.gact.agents.clio_react_record import StepRecorder

    told: list[Any] = []
    monkeypatch.setattr(context, "active_app", lambda: None)
    monkeypatch.setattr(clio_react_record, "emit_injection", lambda *a, **k: told.append(a))
    plane = HistoryPlane()
    StepRecorder(plane, "s", "main", expert_id="main").carry_over(ledger)
    return plane.render_working_set("s", "main"), told


_LEDGER = [
    {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "which station?"}]},
    {"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "P595."}]},
    {"id": "u2", "role": "user", "parts": [{"type": "text", "text": "by how much?"}]},
]


def test_the_recovery_note_keeps_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    segments, told = _carry_over(monkeypatch, _LEDGER)
    notes = [s.content["text"] for s in segments if s.content.get("source") == "context_recovery"]
    assert notes == [_RECOVERED]
    assert told == [("context_recovery", _RECOVERED)]
    assert injections.render("context_recovery_operations", {}) == (
        "Recovered this session's context from recorded context operations. "
        "No operation was replayed."
    )
    assert injections.render("context_recovery_unconfirmed", {}) == (
        "Prepared model input was found without independent delivery proof; its content "
        "remains outside model context until normal mailbox authorization and commitment."
    )


def test_a_switched_off_recovery_note_reaches_neither_the_model_nor_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The context is still restored; only the note is gone, from both places.
    SABOTAGE: append the notice segment even when the note is empty and the model
    context gains an empty CLIO note."""
    injections.update("context_recovery_transcript", active=False)
    segments, told = _carry_over(monkeypatch, _LEDGER)
    assert [s.content.get("text") for s in segments] == ["which station?", "P595."]
    assert told == []


def test_a_call_whose_outcome_was_lost_keeps_its_required_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: change ``context_recovery_uncertain.md`` and the equality fails."""
    assert injections.update("context_recovery_uncertain", active=False).active  # required
    call = {"type": "tool_call", "call_id": "c", "tool_name": "read", "input": {"path": "x"}}
    segments, _ = _carry_over(monkeypatch, [{"role": "assistant", "parts": [call]}])
    [result] = [s for s in segments if s.kind == "observation"]
    assert result.content["text"] == (
        "Operation outcome is uncertain after context loss. No operation was replayed; "
        "inspect retained handles and effects before retrying."
    )


# --------------------------------------------------------------------------- #
# result_spilled                                                              #
# --------------------------------------------------------------------------- #
_TEXT = "line\n" * 200  # 1,000 characters; the room is 400


def _bounded(monkeypatch: pytest.MonkeyPatch, spilled: Path | Exception) -> tuple[str, list]:
    from clio_agent import injections
    from clio_agent.tools import mcp_result_projection as projection

    def spill(text: str, **_: Any) -> Path:
        if isinstance(spilled, Exception):
            raise spilled
        return spilled

    told: list[Any] = []
    pin_result_room(monkeypatch, 400)
    monkeypatch.setattr(projection, "_spill", spill)
    monkeypatch.setattr(injections, "note", lambda source, text: told.append((source, text)))
    return projection.bounded_model_tool_result(_TEXT), told


@pytest.mark.parametrize(
    ("spilled", "where"),
    [
        (
            Path("/w/out.txt"),
            "the full result is in `/w/out.txt`; read on with "
            'fs_read_file(path="/w/out.txt", offset={offset}), or search it with fs_search_text',
        ),
        (
            Path("/w/out.json"),
            "the full result is in `/w/out.json`; it is one JSON document, so search it with "
            "fs_search_text or load it in a script",
        ),
        (
            OSError("disk full"),
            "the full result could not be saved (disk full), so the rest is not available",
        ),
    ],
)
def test_a_cut_result_keeps_todays_note_and_it_cannot_be_switched_off(
    monkeypatch: pytest.MonkeyPatch, spilled: Any, where: str
) -> None:
    """SABOTAGE: change a word in any ``result_spilled*.md`` and its case fails."""
    result, told = _bounded(monkeypatch, spilled)
    head = result.split("\n\n", 1)[1]
    assert head and len(result) <= 400
    where = where.replace("{offset}", str(head.count("\n") + 2))
    note = (
        f"[clio: result_spilled] This result is 1,000 characters, more than the {len(head):,} "
        f"shown to you. The first characters follow; {where}."
    )
    assert result.startswith(note + "\n\n") and told == [("result_spilled", result)]
    for source in ("result_spilled", "result_spilled_read_on", "result_spilled_json"):
        injections.update(source, active=False)
    injections.update("result_spilled_unsaved", active=False)
    assert _bounded(monkeypatch, spilled)[0] == result  # required: the rest's whereabouts


# --------------------------------------------------------------------------- #
# media_unavailable                                                           #
# --------------------------------------------------------------------------- #
def test_the_refused_document_note_keeps_its_text_and_cannot_be_switched_off() -> None:
    """SABOTAGE: change ``media_unavailable.md`` and the equality fails."""
    from clio_agent.providers.codex import file_inputs

    today = (
        "CLIO: The connected Codex backend rejected native document input because its file "
        "preparation is unsupported. The document's contents were NOT read. Do not repeat "
        "view_pdf for this document. Use its workspace path from the question or tool call "
        "with prepare_document(action='inspect') for bounded extracted text. If visual "
        "evidence is needed, render the relevant pages and inspect them with view_image "
        "when available. Continue other work and state any remaining evidence limitation."
    )
    assert file_inputs.file_recovery_note() == today
    assert file_inputs.FILE_RECOVERY_NOTE == today  # the old constant still reads it
    injections.update("media_unavailable", active=False)
    assert file_inputs.file_recovery_note() == today


# --------------------------------------------------------------------------- #
# memory                                                                      #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Name:
    scope: str
    category: str
    path: str

    def __str__(self) -> str:
        return self.path


def _stored(scope: str, category: str, path: str, facts: list[str], about: str = "") -> Any:
    live = tuple(SimpleNamespace(text=t, id=f"f{i}") for i, t in enumerate(facts, 1))
    document = SimpleNamespace(category=category, description=about, live_facts=live)
    return SimpleNamespace(name=_Name(scope, category, path), document=document)


def _snapshot(monkeypatch: pytest.MonkeyPatch) -> str:
    from clio_agent.gact.memory import snapshot

    documents = [
        _stored("user", "profile", "user/profile", ["Prefers SI units"]),
        _stored("user", "areas", "user/areas/a", [], "About A"),
        _stored("user", "areas", "user/areas/b", [], "About B"),
    ]
    service = SimpleNamespace(documents=lambda scopes: documents)
    usage = SimpleNamespace(get=lambda subject: SimpleNamespace(last_read=subject))
    monkeypatch.setattr(snapshot, "setting", lambda key, default: 10)  # one index line fits
    return snapshot.build_snapshot(service, usage, ("user",))["text"]  # type: ignore[arg-type]


def test_the_memory_snapshot_keeps_todays_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: change ``memory.md`` or ``memory_omitted.md`` and the equality fails."""
    assert _snapshot(monkeypatch) == (
        "## Memory\n"
        "What you remember about the user, their projects and how they work. Search and read "
        "the indexed documents with memory_search and memory_read when they bear on the work.\n"
        "\n"
        "### Account profile (user/profile)\n"
        "- Prefers SI units (fact f1)\n"
        "\n"
        "### Index (2 documents)\n"
        "- user/areas/b (areas): About B\n"
        "- 1 more not listed; find them with memory_search."
    )
    assert injections.render("memory_empty", {}) == "- (nothing yet)"


def test_a_switched_off_memory_piece_or_snapshot_is_not_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: remove the ``active`` check in ``memory_snapshot_text`` and the
    switched-off snapshot is still added."""
    from clio_agent.gact.memory import snapshot

    injections.update("memory_omitted", active=False)
    assert _snapshot(monkeypatch).endswith("- user/areas/b (areas): About B")
    monkeypatch.setattr(snapshot, "session_snapshot", lambda app, session: {"text": "M"})
    app = SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(get=lambda sid: None)))
    assert snapshot.memory_snapshot_text(app, "s", object(), "t") == "M"
    injections.update("memory", active=False)
    assert snapshot.memory_snapshot_text(app, "s", object(), "t") == ""


# --------------------------------------------------------------------------- #
# plugin_change                                                               #
# --------------------------------------------------------------------------- #
def _note(monkeypatch: pytest.MonkeyPatch, metadata: dict[str, Any]) -> str:
    from clio_agent.gact.agents import prompt_basis, resolution

    fragment = {"plugin": "hdf5", "text": "Use hdf5_read for HDF5 files.\n"}
    current = SimpleNamespace(
        prompt={"sections": [{"fragments": [fragment]}]},
        skills=[{"id": "hdf5-basics", "description": "Read HDF5"}],
    )
    change = {"pending": True, "added": ["hdf5"], "removed": ["shell"]}
    monkeypatch.setattr(resolution, "session_resolution", lambda app, sid: current)
    monkeypatch.setattr(prompt_basis, "basis_resolution", lambda app, sid, cur: cur)
    monkeypatch.setattr(
        prompt_basis, "pending_change", lambda basis, cur: {**change, "skills_now": ["hdf5-basics"]}
    )
    session = SimpleNamespace(metadata=metadata)
    app = SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(get=lambda sid: session)))
    return prompt_basis.plugin_change_note(app, "s")


def test_the_plugin_change_note_keeps_todays_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: change a word in any ``plugin_change*.md`` and the equality fails."""
    from clio_agent.gact.agents.prompt_basis import DEFERRED_ADDED_KEY, PROMPT_BASIS_KEY

    note = _note(monkeypatch, {PROMPT_BASIS_KEY: "d1", DEFERRED_ADDED_KEY: ["home_new"]})
    assert note == (
        "The person changed this conversation's plugins.\n"
        "Removed: shell. Do not use their tools or follow their instructions, even though "
        "they still appear above.\n"
        "Added: hdf5.\n"
        "Use hdf5_read for HDF5 files.\n"
        "Skills you can load now with load_skill:\n"
        "- hdf5-basics: Read HDF5\n"
        "Tools added since your prompt was built (load them with search_tools): home_new.\n"
        "Your system prompt will include these changes after the next compaction."
    )
    assert _note(monkeypatch, {DEFERRED_ADDED_KEY: ["a", "b"]}) == (
        "Tools added since your prompt was built (load them with search_tools): a, b."
    )


def test_plugin_change_pieces_switch_off_only_where_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The removal, the additions and the tool introductions are required; the deferred
    tools line and the footer can be switched off. SABOTAGE: stop filtering ``None``
    pieces in ``plugin_change_note`` and the join fails."""
    from clio_agent.gact.agents import prompt_basis
    from clio_agent.gact.agents.prompt_basis import DEFERRED_ADDED_KEY, PROMPT_BASIS_KEY

    for source in (
        "plugin_change",
        "plugin_change_removed",
        "plugin_change_added",
        "plugin_change_skills",
        "plugin_change_tool_introductions",
    ):
        assert injections.update(source, active=False).active, source
    injections.update("plugin_change_footer", active=False)
    injections.update("plugin_change_deferred_tools", active=False)
    note = _note(monkeypatch, {PROMPT_BASIS_KEY: "d1", DEFERRED_ADDED_KEY: ["home_new"]})
    assert note.splitlines()[-1] == "- hdf5-basics: Read HDF5"
    assert note.startswith("The person changed this conversation's plugins.\nRemoved: shell.")
    assert _note(monkeypatch, {DEFERRED_ADDED_KEY: ["a"]}) == ""
    tool = SimpleNamespace(name="hdf5_read", args={"path": {"type": "string"}}, desc="Read.")
    out = dict(prompt_basis.with_tool_introductions([("plugin_change", "Added: x.")], [tool]))
    assert out["plugin_change"] == (
        "Added: x.\nNew tools you can call now, though they are not in your tool list until "
        "the next compaction:\n- hdf5_read(path: string): Read."
    )
