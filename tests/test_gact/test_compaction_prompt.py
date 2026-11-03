"""The compaction prompt is the registry prompt ``clio.compaction`` (no code change).

On a real app over real clio-core, through ``POST /v1/sessions/{sid}/compact``: the
shipped template is used when nothing overrides it; a session's own copy (saved as the
Prompts page saves it) is used instead, for that session only (the summarizer's last
message is exactly its rendering, after the scope's own context); the placeholders are
filled; an edit applies at the next compaction; an invalid template, or the removed
``compaction.prompt_file`` setting still set, fails the compaction typed
(``compaction.failed`` + a notice, nothing folded) and is a ``misconfigured`` doctor row
-- never a fallback to the shipped text.

SABOTAGE (run manually, each turns the named test red):

* ``compaction_prompt.load_prompt_template``: resolve without the session
  (``registry_for(app, "")``) -> ``test_a_sessions_copy_is_its_prompt``.
* ``compaction_prompt.load_prompt_template``: drop the unknown-placeholder check ->
  ``test_an_invalid_template_or_the_removed_setting_fails_typed_and_folds_nothing``.
* cache the template on first read -> ``test_an_edit_applies_at_the_next_compaction``.
"""

from __future__ import annotations

import subprocess
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.compaction_prompt import PROMPT_ID, probe_compaction_prompt
from clio_agent.gact.app import build_app
from clio_agent.runtime.status import IntegrationState
from tests._compaction_summary import keep_open_request_only, split_seen

from .test_compaction import _SCOPE, _CapturingAgent, _seed, _text_message
from .test_post_messages import _create_session

REPO = Path(__file__).resolve().parents[2]
PACKAGED = (
    REPO / "src" / "clio_agent" / "prompt_packs" / "builtin" / "templates" / "clio.compaction.md"
)


def _save(app: Any, sid: str, text: str) -> None:
    """Save the session's own copy, as the Prompts page saves it."""
    registry = app.state.prompt_registry_for_request(session_id=sid, write_scope="session")
    registry.save(PROMPT_ID, text=text, metadata={"kind": "template", "variables": "focus, files"})


def _shipped_body() -> str:
    return PACKAGED.read_text(encoding="utf-8").split("---\n", 2)[2]


def _compact(
    tmp_path: Path,
    texts: list[str],
    *,
    focus: str = "",
    files: dict[str, Any] | None = None,
    override: str | None = None,
) -> tuple[Any, list[str], list[Any], str]:
    """One manual compaction on a fresh session; ``(response, prompts, events, sid)``."""
    agent = _CapturingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    events: list[Any] = []
    real_emit = app.state.semantic_event_sink.emit
    app.state.semantic_event_sink.emit = lambda e: (events.append(e), real_emit(e))[1]
    with TestClient(app) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message(f"msg_{i}", sid, t) for i, t in enumerate(texts)])
        if files:
            app.state.context_files[sid] = files
        if override is not None:
            _save(app, sid, override)
        response = client.post(f"/v1/sessions/{sid}/compact", json={"focus": focus})
        response.app_state = app  # type: ignore[attr-defined]
    return response, agent.prompts, events, sid


def test_the_shipped_template_is_the_prompt_when_nothing_overrides_it(
    tmp_path: Path,
) -> None:
    response, prompts, _events, _sid = _compact(tmp_path, ["DEFAULT-ROW"])
    assert response.status_code == 200, response.text
    [(history, prompt)] = [split_seen(p) for p in prompts]
    assert history == "USER: DEFAULT-ROW"  # the scope's own context, then the prompt
    expected = _shipped_body().replace("{{focus}}", "").replace("{{files}}", "").strip()
    assert prompt == expected
    assert prompt.startswith("The context window is nearly full")


def test_a_sessions_copy_is_its_prompt(tmp_path: Path) -> None:
    response, prompts, _events, _sid = _compact(
        tmp_path, ["CUSTOM-ROW"], override="RESEARCH RULES {literal}{{focus}}"
    )
    assert response.status_code == 200, response.text
    assert [split_seen(p) for p in prompts] == [("USER: CUSTOM-ROW", "RESEARCH RULES {literal}")]
    # Another session still sees the shipped template.
    _response, others, _e, _s = _compact(tmp_path / "other", ["ROW"])
    assert split_seen(others[0])[1].startswith("The context window is nearly full")


def test_focus_and_files_fill_their_placeholders(tmp_path: Path) -> None:
    files = {"a.csv": {"path": "a.csv", "display_path": "data/a.csv", "mode": "read"}}
    response, prompts, _events, _sid = _compact(
        tmp_path, ["ROW"], focus="units", files=files, override="R{{focus}}{{ files }}"
    )
    assert response.status_code == 200, response.text
    assert [split_seen(p)[1] for p in prompts] == [
        "R\n\nFocus the summary on: units"
        "\n\n--- attached session files ---\n- path=data/a.csv; mode=read\n--- end files ---"
    ]


@pytest.mark.parametrize(
    ("override", "removed", "problem"),
    [
        ("rules {{focus}} and {{model}}", None, "unknown render placeholder: model"),
        ("rules over {{transcript}}", None, "unknown render placeholder: transcript"),
        (None, "/old/compaction.md", "compaction.prompt_file (CLIO_COMPACTION_PROMPT_FILE) was"),
    ],
)
def test_an_invalid_template_or_the_removed_setting_fails_typed_and_folds_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str | None,
    removed: str | None,
    problem: str,
) -> None:
    if removed is not None:
        monkeypatch.setenv("CLIO_COMPACTION_PROMPT_FILE", removed)
    response, prompts, events, sid = _compact(tmp_path, ["ROW"], override=override)

    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert error["error"] == "compaction_prompt_invalid"
    assert error["details"]["prompt_id"] == PROMPT_ID and problem in error["details"]["problem"]
    assert prompts == []  # no LM call, no fallback to the shipped text
    seq = [e for e in events if e.event_type.startswith("compaction.")]
    assert [e.event_type for e in seq] == ["compaction.started", "compaction.failed"]
    assert seq[1].payload["error"]["code"] == "compaction_prompt_invalid"
    app = response.app_state
    live = app.state.arc.render_working_set(sid, _SCOPE)
    assert [(s.kind, s.content["text"]) for s in live] == [("user", "ROW")]
    [notice] = [p for m in app.state.messages[sid] for p in m.parts if p.type == "notice"]
    assert (notice.code, notice.source) == ("compaction_prompt_invalid", "compaction_failed")
    assert PROMPT_ID in notice.text


def test_an_edit_applies_at_the_next_compaction(tmp_path: Path) -> None:
    agent = _CapturingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = _create_session(client)
        _save(app, sid, "FIRST rules")
        _seed(client, sid, [_text_message("msg_a", sid, "A")])
        assert client.post(f"/v1/sessions/{sid}/compact", json={}).status_code == 200
        _save(app, sid, "SECOND rules")
        app.state.arc.append_segment(sid, _SCOPE, "user", {"text": "B"}, turn_id="t2")
        assert client.post(f"/v1/sessions/{sid}/compact", json={}).status_code == 200
    assert [split_seen(p)[1] for p in agent.prompts] == ["FIRST rules", "SECOND rules"]


def test_the_doctor_row_validates_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.runtime.status import RuntimeProbe

    monkeypatch.delenv("CLIO_COMPACTION_PROMPT_FILE", raising=False)
    ready = probe_compaction_prompt()
    assert (ready.state, ready.details["prompt_id"]) == (IntegrationState.READY, PROMPT_ID)

    monkeypatch.setenv("CLIO_COMPACTION_PROMPT_FILE", "/old/compaction.md")
    row = probe_compaction_prompt()
    assert row.state is IntegrationState.MISCONFIGURED
    assert row.details["scope"] == "settings" and "was removed" in row.details["problem"]
    # The runtime report (clio-agent doctor, /v1/health) carries the row.
    monkeypatch.setattr(RuntimeProbe, "probe_gateway", _fake_gateway)
    report = RuntimeProbe(env={}).collect(include_process_census=False)
    [served] = [r for r in report.integrations if r.name == "compaction_prompt"]
    assert served.state is IntegrationState.MISCONFIGURED


def _fake_gateway(self: Any) -> Any:
    from clio_agent.runtime.status import IntegrationStatus

    return IntegrationStatus(
        name="gateway",
        state=IntegrationState.SKIPPED,
        summary="not probed in this test",
        config_source="test",
        next_action="none",
    )


def test_the_default_template_ships_in_the_wheel() -> None:
    """The wheel packs ``src/clio_agent`` whole (hatch ``packages``), minus git-ignored
    files and the build excludes: the template must be tracked and match no exclusion."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    hatch = project["tool"]["hatch"]["build"]
    assert "src/clio_agent" in hatch["targets"]["wheel"]["packages"]
    rel = PACKAGED.relative_to(REPO).as_posix()
    assert not any(rel.startswith(f"{ex.rstrip('/')}/") for ex in hatch.get("exclude", []))
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", rel], cwd=REPO, check=False, capture_output=True
    )
    assert ignored.returncode == 1, f"{rel} is git-ignored, so the wheel would drop it"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel], cwd=REPO, check=False, capture_output=True
    )
    assert tracked.returncode == 0, f"{rel} is not tracked"
    packaged = (
        resources.files("clio_agent.prompt_packs.builtin") / "templates" / "clio.compaction.md"
    )
    assert packaged.is_file()


def test_the_exact_evidence_index_reads_tool_evidence_whole() -> None:
    """A long tool result reaches the code-computed evidence index as it was in the
    context. SABOTAGE: cut the result text in ``_message_lines`` and this fails."""
    from types import SimpleNamespace

    from clio_agent.gact import compaction

    result = "row " * 2_000
    part = SimpleNamespace(
        name="fs_read_file", is_error=False, content=[SimpleNamespace(text=result)]
    )
    message = SimpleNamespace(role="tool", parts=[type("ToolResultPart", (), vars(part))()])
    [line] = compaction._message_lines(message)
    assert line.endswith(result)


@pytest.fixture(autouse=True)
def _open_request_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The keep rule these mechanics tests were written for (see the helper)."""
    keep_open_request_only(monkeypatch)
