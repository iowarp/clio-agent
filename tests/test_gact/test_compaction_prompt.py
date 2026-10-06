"""The compaction summarizer prompt is a template file researchers swap (no code change).

On a real app over real clio-core, through ``POST /v1/sessions/{sid}/compact``: the
packaged default is used when nothing is configured; a configured file is used
instead (the summarizer LM's prompt is exactly its rendering); the placeholders are
filled; an edit applies at the next compaction; a missing or invalid file fails the
compaction typed (``compaction.failed`` + a notice, nothing folded) and is a
``misconfigured`` doctor row -- never a fallback to the default.

SABOTAGE (run manually, each turns the named test red):

* ``compaction_prompt.configured_prompt_path``: ignore the configured value ->
  ``test_a_configured_file_is_the_prompt``.
* ``compaction_prompt.load_prompt_template``: fall back to the packaged default on a
  missing file -> ``test_a_missing_or_invalid_file_fails_typed_and_folds_nothing``.
* ``compaction_prompt._validate``: drop the unknown-placeholder check ->
  ``test_a_missing_or_invalid_file_fails_typed_and_folds_nothing``.
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

from clio_agent.compaction_prompt import probe_compaction_prompt, render_compaction_prompt
from clio_agent.gact.app import build_app
from clio_agent.runtime.status import IntegrationState

from .test_compaction import _SCOPE, _CapturingAgent, _seed, _text_message
from .test_post_messages import _create_session

REPO = Path(__file__).resolve().parents[2]
PACKAGED = REPO / "src" / "clio_agent" / "prompt_packs" / "builtin" / "compaction.md"


def _compact(
    tmp_path: Path, texts: list[str], *, focus: str = "", files: dict[str, Any] | None = None
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
        response = client.post(f"/v1/sessions/{sid}/compact", json={"focus": focus})
        response.app_state = app  # type: ignore[attr-defined]
    return response, agent.prompts, events, sid


def test_the_packaged_default_is_the_prompt_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIO_COMPACTION_PROMPT_FILE", raising=False)
    response, prompts, _events, _sid = _compact(tmp_path, ["DEFAULT-ROW"])
    assert response.status_code == 200, response.text
    [prompt] = prompts
    template = PACKAGED.read_text(encoding="utf-8")
    assert prompt == template.format_map(
        {"transcript": "USER: DEFAULT-ROW", "focus": "", "files": ""}
    )
    assert prompt.startswith("Create an evidence-preserving compact memory")


def test_a_configured_file_is_the_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "my_prompt.md"
    custom.write_text("RESEARCH RULES {{literal}}\n{transcript}\nEND", encoding="utf-8")
    monkeypatch.setenv("CLIO_COMPACTION_PROMPT_FILE", str(custom))
    response, prompts, _events, _sid = _compact(tmp_path, ["CUSTOM-ROW"])
    assert response.status_code == 200, response.text
    assert prompts == ["RESEARCH RULES {literal}\nUSER: CUSTOM-ROW\nEND"]


def test_focus_and_files_fill_their_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "p.md"
    custom.write_text("R{focus}{files}\n<<{transcript}>>", encoding="utf-8")
    monkeypatch.setenv("CLIO_COMPACTION_PROMPT_FILE", str(custom))
    files = {"a.csv": {"path": "a.csv", "display_path": "data/a.csv", "mode": "read"}}
    response, prompts, _events, _sid = _compact(tmp_path, ["ROW"], focus="units", files=files)
    assert response.status_code == 200, response.text
    assert prompts == [
        "R\n\nFocus the summary on: units"
        "\n\n--- attached session files ---\n- path=data/a.csv; mode=read\n--- end files ---"
        "\n<<USER: ROW>>"
    ]
    assert render_compaction_prompt("T") == "R\n<<T>>"  # empty sections render nothing


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (None, "the file does not exist"),
        ("rules {transcript} and {model}", "unknown placeholder {model}"),
        ("rules with no transcript", "no {transcript} placeholder"),
        ("rules {transcript} {", "invalid template"),
        ("rules {transcript!r}", "unknown placeholder {transcript}"),
    ],
)
def test_a_missing_or_invalid_file_fails_typed_and_folds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None, problem: str
) -> None:
    custom = tmp_path / "broken.md"
    if content is not None:
        custom.write_text(content, encoding="utf-8")
    monkeypatch.setenv("CLIO_COMPACTION_PROMPT_FILE", str(custom))
    response, prompts, events, sid = _compact(tmp_path, ["ROW"])

    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert error["error"] == "compaction_prompt_invalid"
    assert error["details"]["path"] == str(custom) and problem in error["details"]["problem"]
    assert prompts == []  # no LM call, no fallback to the packaged default
    seq = [e for e in events if e.event_type.startswith("compaction.")]
    assert [e.event_type for e in seq] == ["compaction.started", "compaction.failed"]
    assert seq[1].payload["error"]["code"] == "compaction_prompt_invalid"
    app = response.app_state
    live = app.state.arc.render_working_set(sid, _SCOPE)
    assert [(s.kind, s.content["text"]) for s in live] == [("user", "ROW")]
    [notice] = [p for m in app.state.messages[sid] for p in m.parts if p.type == "notice"]
    assert (notice.code, notice.source) == ("compaction_prompt_invalid", "compaction_failed")
    assert str(custom) in notice.text


def test_an_edit_applies_at_the_next_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "p.md"
    custom.write_text("FIRST {transcript}", encoding="utf-8")
    monkeypatch.setenv("CLIO_COMPACTION_PROMPT_FILE", str(custom))
    agent = _CapturingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = _create_session(client)
        _seed(client, sid, [_text_message("msg_a", sid, "A")])
        assert client.post(f"/v1/sessions/{sid}/compact", json={}).status_code == 200
        custom.write_text("SECOND {transcript}", encoding="utf-8")
        app.state.arc.append_segment(sid, _SCOPE, "user", {"text": "B"}, turn_id="t2")
        assert client.post(f"/v1/sessions/{sid}/compact", json={}).status_code == 200
    assert [p.split(" ", 1)[0] for p in agent.prompts] == ["FIRST", "SECOND"]


def test_the_doctor_row_validates_the_configured_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.runtime.status import RuntimeProbe

    monkeypatch.delenv("CLIO_COMPACTION_PROMPT_FILE", raising=False)
    ready = probe_compaction_prompt()
    assert (ready.state, ready.details["path"]) == (IntegrationState.READY, str(PACKAGED))

    missing = tmp_path / "nope.md"
    monkeypatch.setenv("CLIO_COMPACTION_PROMPT_FILE", str(missing))
    row = probe_compaction_prompt()
    assert row.state is IntegrationState.MISCONFIGURED
    assert row.details == {"path": str(missing), "problem": "the file does not exist"}
    assert row.config_source == "compaction.prompt_file"
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
    packaged = resources.files("clio_agent.prompt_packs.builtin") / "compaction.md"
    assert packaged.is_file()
