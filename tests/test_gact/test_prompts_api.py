from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _write_agent_invocable_command(command_dir: Path, name: str, description: str) -> None:
    """Write a minimal agent-invocable slash-command file to ``command_dir``."""

    command_dir.mkdir(parents=True, exist_ok=True)
    command_dir.joinpath(f"{name}.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"title: {name.title()}",
                f"description: {description}",
                "agent: main",
                "agent-invocable: true",
                "---",
                f"Run {name}.",
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def test_capabilities_advertise_prompt_registry(client: TestClient) -> None:
    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_prompt_registry"] is True


def test_builtin_prompts_are_listed_and_resolvable(client: TestClient) -> None:
    listed = client.get("/v1/prompts")

    assert listed.status_code == 200
    prompts = {row["id"]: row for row in listed.json()["prompts"]}
    assert "clio.chat" in prompts
    assert "default" in prompts["clio.chat"]["profiles"]
    assert "heavy" in prompts["clio.chat"]["profiles"]
    assert prompts["clio.runtime.prompt_user_agent"]["title"] == "Agent without tools"
    assert prompts["clio.runtime.tool_user_agent"]["title"] == "Agent with tools"
    assert "small_model" in prompts["clio.main.planner"]["profiles"]

    resolved = client.get("/v1/prompts/clio.chat").json()["prompt"]
    assert resolved["id"] == "clio.chat"
    assert resolved["profile"] == "default"
    assert "CLIO" in resolved["text"]
    assert "Never claim a tool call" in resolved["text"]
    assert resolved["scope"] == "builtin"
    assert resolved["checksum"]
    assert resolved["source_path"].startswith("package://clio_agent.prompt_packs.builtin/")
    assert resolved["metadata"]["source"] == "packaged_prompt_file"

    heavy = client.get("/v1/prompts/clio.main.planner?profile=heavy").json()["prompt"]
    assert heavy["profile"] == "heavy"
    assert heavy["metadata"]["alignment"] == "public_reference_matrix"
    assert "delegate to scoped child experts" in heavy["text"]
    assert "declared tools and experts" in heavy["text"]


def test_prompt_render_expands_safe_dynamic_placeholders(client: TestClient) -> None:
    resp = client.post(
        "/v1/prompts/clio.main.planner/render",
        json={
            "profile": "heavy",
            "context": {
                "agents.available_tree": "- main: Main agent\n- data: Data expert",
                "tools.available": "- hdf5_list_datasets: inspect HDF5 files",
                "commands.agent_invocable": "- /summarize: summarize data",
                "memory.policy_summary": "same-workspace memory allowed with user intent",
                "permissions.policy_summary": "ask before destructive actions",
                "provider.current": "provider=test model=fake",
                "session.active_pack": "builtin",
            },
        },
    )

    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    assert prompt["profile"] == "heavy"
    assert "{{ agents.available_tree }}" not in prompt["text"]
    assert "- data: Data expert" in prompt["text"]
    assert "- hdf5_list_datasets: inspect HDF5 files" in prompt["text"]
    assert "same-workspace memory allowed" in prompt["text"]
    render = prompt["metadata"]["render"]
    assert "agents.available_tree" in render["placeholders_used"]
    assert "tools.available" in render["placeholders_used"]


def test_prompt_render_reports_unknown_placeholder(client: TestClient) -> None:
    saved = client.put(
        "/v1/prompts/clio.bad_placeholder",
        json={
            "profile": "default",
            "text": "This references {{ unknown.placeholder }}.",
        },
    )
    assert saved.status_code == 200, saved.text

    resp = client.post("/v1/prompts/clio.bad_placeholder/render", json={})

    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    assert "{{ unknown.placeholder }}" in prompt["text"]
    assert "unknown render placeholder: unknown.placeholder" in prompt["validation_errors"]


def test_prompt_validate_and_reload_endpoints(client: TestClient) -> None:
    invalid = client.post(
        "/v1/prompts/clio.validate_me/validate",
        json={"profile": "default", "text": "Bad {{ not.allowed }}"},
    )

    assert invalid.status_code == 200, invalid.text
    assert invalid.json()["enabled"] is False
    assert "unknown render placeholder: not.allowed" in invalid.json()["validation_errors"]

    reload_resp = client.post("/v1/prompts/reload", json={})

    assert reload_resp.status_code == 200, reload_resp.text
    assert "clio.chat" in reload_resp.json()["reload"]["prompt_ids"]
    assert reload_resp.json()["reload"]["prompt_count"] >= 1


def test_put_prompt_saves_external_profile_and_resolution_uses_it(client: TestClient) -> None:
    resp = client.put(
        "/v1/prompts/clio.chat",
        json={
            "profile": "heavy",
            "title": "Heavy chat",
            "description": "More explicit behavior",
            "text": "Use detailed but grounded CLIO behavior.",
            "provider": "openai",
            "model": "gpt-5.1",
            "metadata": {"edited_by": "test"},
        },
    )

    assert resp.status_code == 200, resp.text
    saved = resp.json()["prompt"]
    assert saved["profiles"]["heavy"]["text"] == "Use detailed but grounded CLIO behavior."

    resolved = client.get("/v1/prompts/clio.chat?profile=heavy").json()["prompt"]
    assert resolved["text"] == "Use detailed but grounded CLIO behavior."
    assert resolved["title"] == "Heavy chat"
    assert resolved["scope"] == "global"
    assert resolved["provider"] == "openai"
    assert resolved["model"] == "gpt-5.1"
    assert resolved["metadata"]["edited_by"] == "test"


def test_put_prompt_rejects_invalid_profile(client: TestClient) -> None:
    resp = client.put(
        "/v1/prompts/clio.chat",
        json={"profile": "../bad", "text": "bad"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "bad_request"


def test_session_prompt_override_does_not_leak_to_other_sessions(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as c:
        sid_a = c.post("/v1/sessions", json={"title": "A"}).json()["id"]
        sid_b = c.post("/v1/sessions", json={"title": "B"}).json()["id"]
        saved = c.put(
            "/v1/prompts/clio.chat",
            json={
                "scope": "session",
                "session_id": sid_a,
                "profile": "default",
                "text": "Session A chat prompt.",
            },
        )
        assert saved.status_code == 200, saved.text

        prompt_a = c.get(
            "/v1/prompts/clio.chat",
            params={"session_id": sid_a},
        ).json()["prompt"]
        prompt_b = c.get(
            "/v1/prompts/clio.chat",
            params={"session_id": sid_b},
        ).json()["prompt"]

    assert prompt_a["text"] == "Session A chat prompt."
    assert prompt_a["scope"] == "session"
    assert "Session A chat prompt." not in prompt_b["text"]


def test_workspace_prompt_override_uses_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as c:
        wid = c.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        saved = c.put(
            "/v1/prompts/clio.chat",
            json={
                "scope": "workspace",
                "workspace_id": wid,
                "profile": "default",
                "text": "Workspace chat prompt.",
            },
        )
        assert saved.status_code == 200, saved.text
        prompt = c.get(
            "/v1/prompts/clio.chat",
            params={"workspace_id": wid},
        ).json()["prompt"]

    assert (workspace / ".clio" / "prompts" / "clio.chat--default.md").exists()
    assert prompt["text"] == "Workspace chat prompt."
    assert prompt["scope"] == "workspace"


def test_active_expert_pack_prompts_feed_agent_catalog_and_render_context(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "session-pack"
    (pack / "experts").mkdir(parents=True)
    (pack / "prompts").mkdir()
    pack.joinpath("clio-pack.yaml").write_text(
        """id: science-pack
version: 0.1.0
title: Science Pack
""",
        encoding="utf-8",
    )
    pack.joinpath("prompts", "science.root.md").write_text(
        """---
id: science.root
profile: heavy
provider: openai
model: gpt-5.1
---
Use the science pack root prompt.
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "science.md").write_text(
        """---
id: science
title: Science Root
parent_id: main
tier: 2
prompt_id: science.root
prompt_profile: heavy
---
Inline prompt should be composed.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "science"}).json()["id"]
        activated = c.post(
            f"/v1/sessions/{sid}/expert-pack",
            json={"path": str(pack)},
        )
        assert activated.status_code == 200, activated.text
        agent = c.get("/v1/agents/science", params={"session_id": sid}).json()
        rendered = c.post(
            "/v1/prompts/clio.main.planner/render",
            json={"session_id": sid},
        ).json()["prompt"]

    assert agent["system_prompt"] == "\n\n".join(
        (
            "Use the science pack root prompt.",
            "Agent-specific instructions from this definition:",
            "Inline prompt should be composed.",
        )
    )
    assert agent["default_provider"] == "openai"
    assert agent["default_model"] == "gpt-5.1"
    assert agent["metadata"]["prompt_resolution"]["scope"] == "session_pack"
    assert "- science: Science Root" in rendered["text"]
    assert "science-pack" in rendered["text"]


def test_user_agent_runtime_uses_resolved_prompt_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    seen: dict[str, Any] = {}

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(
        base_agent: Any,
        agent_def: Any,
        question: str,
        session_id: str,
    ) -> Any:
        del base_agent
        seen.update(
            {
                "system_prompt": agent_def.system_prompt,
                "provider": agent_def.default_provider,
                "model": agent_def.default_model,
                "metadata": agent_def.metadata,
                "question": question,
                "session_id": session_id,
            }
        )
        return type(
            "Pred",
            (),
            {
                "answer": "PROMPT_PROFILE_OK",
                "selected_expert": agent_def.id,
                "routing_rationale": "selected registered prompt-backed agent",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as c:
        saved = c.put(
            "/v1/prompts/clio.reviewer",
            json={
                "profile": "light",
                "title": "Reviewer light",
                "text": "Use the external reviewer prompt.",
                "provider": "openai",
                "model": "gpt-5-mini",
            },
        )
        assert saved.status_code == 200, saved.text
        created = c.post(
            "/v1/agents",
            json={
                "id": "reviewer",
                "title": "Reviewer",
                "system_prompt": "This inline prompt should be composed.",
                "metadata": {
                    "prompt_id": "clio.reviewer",
                    "prompt_profile": "light",
                },
            },
        )
        assert created.status_code == 201, created.text
        catalog_agent = c.get("/v1/agents/reviewer").json()
        listed_agents = {row["id"]: row for row in c.get("/v1/agents").json()["agents"]}
        sid = c.post(
            "/v1/sessions",
            json={"title": "prompt-backed agent", "agent": {"id": "reviewer"}},
        ).json()["id"]
        assistant = complete_turn(c, sid, "review this")

    assert catalog_agent["system_prompt"] == "\n\n".join(
        (
            "Use the external reviewer prompt.",
            "Agent-specific instructions from this definition:",
            "This inline prompt should be composed.",
        )
    )
    assert catalog_agent["default_provider"] == "openai"
    assert catalog_agent["default_model"] == "gpt-5-mini"
    assert catalog_agent["metadata"]["prompt_resolution"]["status"] == "resolved"
    assert listed_agents["reviewer"]["metadata"]["prompt_resolution"]["id"] == "clio.reviewer"
    assert seen["question"] == "review this"
    assert seen["system_prompt"] == "\n\n".join(
        (
            "Use the external reviewer prompt.",
            "Agent-specific instructions from this definition:",
            "This inline prompt should be composed.",
        )
    )
    assert seen["provider"] == "openai"
    assert seen["model"] == "gpt-5-mini"
    resolution = seen["metadata"]["prompt_resolution"]
    assert resolution["id"] == "clio.reviewer"
    assert resolution["profile"] == "light"
    assert resolution["scope"] == "global"
    assert resolution["status"] == "resolved"
    assert assistant["metadata"]["prompt_resolution"]["id"] == "clio.reviewer"
    assert assistant["metadata"]["prompt_resolution"]["profile"] == "light"
    # Text answer only — the routing decision is a routing.decision semantic
    # event (a0e1d9a9), never a message part.
    assert [part["type"] for part in assistant["parts"]] == ["text"]
    assert assistant["parts"][0]["text"] == "PROMPT_PROFILE_OK"


def test_planner_render_context_uses_agent_scoped_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The render context's ``commands.agent_invocable`` must be the AGENT-scoped
    subset (what ``planner_command_rows`` returns for that agent+session), not the
    un-scoped base command list.

    Regression for #770 C1: ``_prompt_render_context_for_request`` passed the
    module-level ``_resolve_runtime_dynamic_agent`` (arity ``(app, agent_id, ...)``)
    where ``planner_command_rows`` calls ``resolver(agent_id, session_id=...)`` ->
    ``TypeError`` swallowed by a bare ``except``, so the scoped enrichment silently
    reverted to the base list.
    """

    monkeypatch.chdir(tmp_path)
    command_dir = tmp_path / ".clio" / "commands"
    # Two agent-invocable commands on disk; the active agent only allows one.
    _write_agent_invocable_command(command_dir, "summarize", "Summarize a dataset")
    _write_agent_invocable_command(command_dir, "other", "Some other command")

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as c:
        created = c.post(
            "/v1/agents",
            json={
                "id": "caller",
                "title": "Caller",
                "system_prompt": "Call allowed commands.",
                "metadata": {"commands": ["/summarize"]},
            },
        )
        assert created.status_code in (200, 201), created.text
        sid = c.post(
            "/v1/sessions",
            json={"title": "t", "agent": {"id": "caller"}},
        ).json()["id"]

        rendered = c.post(
            "/v1/prompts/clio.main.planner/render",
            json={"session_id": sid},
        ).json()["prompt"]

    text = rendered["text"]
    # The agent-scoped subset keeps /summarize (allowed) and drops /other (not
    # allowed for this agent). Under the bug the base list leaked /other through.
    assert "/summarize" in text
    assert "/other" not in text


def test_planner_render_context_records_reason_on_enrichment_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure in the agent-invocable command enrichment must emit a structured
    reason (no silent ``except: pass``) and fall back to the base command list."""

    monkeypatch.chdir(tmp_path)
    _write_agent_invocable_command(
        tmp_path / ".clio" / "commands", "summarize", "Summarize a dataset"
    )

    def _boom(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("planner enrichment exploded")

    monkeypatch.setattr("clio_agent.gact.app._planner_command_rows", _boom)

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions",
            json={"title": "t", "agent": {"id": "main"}},
        ).json()["id"]

        with caplog.at_level(logging.WARNING, logger="clio_agent"):
            resp = c.post(
                "/v1/prompts/clio.main.planner/render",
                json={"session_id": sid},
            )

    assert resp.status_code == 200, resp.text
    # The failure is observable in the trace, not swallowed.
    reasons = [
        rec.getMessage()
        for rec in caplog.records
        if "PROMPT-CTX" in rec.getMessage() and "enrichment failed" in rec.getMessage()
    ]
    assert reasons, "expected a structured PROMPT-CTX enrichment-failure reason"
    # Match by content, not position: caplog.records ordering is not guaranteed to be
    # stable across runs (logger propagation / concurrent handlers can interleave), so
    # a positional ``reasons[0]`` was order-dependent and flaked (#902). Assert the
    # cause appears in *some* captured PROMPT-CTX reason instead.
    assert any("planner enrichment exploded" in reason for reason in reasons)
    # Enrichment failed -> render still succeeds on the base command list.
    assert "/summarize" in resp.json()["prompt"]["text"]


def test_provider_summary_serializes_dict_lm_config(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``provider.current`` renders the live ``app.state.lm_config`` as proper JSON
    without the degraded repr fallback.

    Regression (S6 live-gate wart): ``lm_config`` is a plain dict (set by
    ``PUT /v1/providers/lm``), but the render context called ``asdict()`` on it —
    ``asdict()`` only accepts DATACLASS instances, so it raised every single turn
    ("asdict() should be called on dataclass instances") and silently degraded to
    ``str(provider)`` (a Python repr, not JSON). The fix serializes a Mapping
    directly.

    Sabotage: revert the fix to ``json.dumps(asdict(provider), ...)`` and this test
    fails — the degrade reason fires and ``provider.current`` is a repr, not JSON.
    """

    from clio_agent.gact.agents.composition import _prompt_render_context

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        app.state.lm_config = {
            "provider": "lmstudio",
            "model": "qwen3",
            "api_base": "http://127.0.0.1:1234/v1",
            "temperature": 0.2,
            "max_tokens": 4096,
            "transport": "openai_chat",
        }
        with caplog.at_level(logging.WARNING, logger="clio_agent"):
            ctx = _prompt_render_context(app)

    summary = ctx["provider.current"]
    # Proper JSON of the live config (NOT a Python repr), round-trips to the dict.
    parsed = json.loads(summary)
    assert parsed == app.state.lm_config
    # No degrade reason fired (the asdict() footgun is gone).
    assert not [
        r.getMessage()
        for r in caplog.records
        if "provider summary serialize failed" in r.getMessage()
    ], "provider summary should serialize cleanly, not degrade to repr"


def test_provider_summary_typed_fallback_survives_bad_input(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The typed repr fallback is preserved for a GENUINELY unserializable provider
    (neither a dataclass nor a mapping) — the fix removes the spurious degrade on a
    plain dict, it does not remove the real defensive path."""

    from clio_agent.gact.agents.composition import _prompt_render_context

    class _Weird:
        def __repr__(self) -> str:
            return "<weird-provider>"

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        app.state.lm_config = _Weird()
        with caplog.at_level(logging.WARNING, logger="clio_agent"):
            ctx = _prompt_render_context(app)

    assert ctx["provider.current"] == "<weird-provider>"
    assert any("provider summary serialize failed" in r.getMessage() for r in caplog.records), (
        "a genuinely bad provider must still emit the structured degrade reason"
    )
