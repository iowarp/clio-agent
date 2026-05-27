from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.expert_packs import load_expert_packs, parse_expert_file


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.chdir(workspace)
    return workspace


def test_parse_expert_file_with_hierarchy_prompt_and_model(tmp_path: Path) -> None:
    path = tmp_path / "ndp.md"
    path.write_text(
        """---
id: ndp_catalog
title: NDP Catalog Expert
description: Dataset discovery specialist
parent_id: data
tier: 3
specialization: catalog
keywords:
- ndp
- earthscope
tools:
- ndp.search
- ndp.stage
prompt_id: clio.expert.ndp_catalog
prompt_profile: heavy
provider: openai
model: gpt-5.1
param_temperature: 0.2
---
Use bounded dataset discovery.
""",
        encoding="utf-8",
    )

    row = parse_expert_file(path, scope="workspace")

    assert row.id == "ndp_catalog"
    assert row.source == "expert_pack"
    assert row.parent_id == "data"
    assert row.tier == 3
    assert row.system_prompt == "Use bounded dataset discovery."
    assert row.prompt_id == "clio.expert.ndp_catalog"
    assert row.prompt_profile == "heavy"
    assert row.default_provider == "openai"
    assert row.default_model == "gpt-5.1"
    assert row.tools == ["ndp.search", "ndp.stage"]
    assert row.keywords == ["ndp", "earthscope"]
    assert row.parameters["temperature"] == "0.2"
    assert row.enabled is True
    assert row.validation_errors == []


def test_invalid_expert_file_is_disabled_with_errors(tmp_path: Path) -> None:
    path = tmp_path / "broken.md"
    path.write_text(
        """---
tier: 3
---
""",
        encoding="utf-8",
    )

    row = parse_expert_file(path, scope="workspace")

    assert row.enabled is False
    assert row.id == "broken"
    assert "missing required frontmatter field: id" in row.validation_errors
    assert "tier > 1 experts must declare parent_id" in row.validation_errors
    assert "expert must provide a prompt body or prompt_id" in row.validation_errors


def test_load_expert_packs_workspace_overrides_global(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    global_root = tmp_path / "home" / ".config" / "clio-agent" / "experts"
    global_root.mkdir(parents=True)
    global_root.joinpath("reviewer.md").write_text(
        """---
id: reviewer
title: Global Reviewer
parent_id: main
---
global prompt
""",
        encoding="utf-8",
    )
    workspace_root = isolated_env / ".clio" / "experts"
    workspace_root.mkdir(parents=True)
    workspace_root.joinpath("reviewer.md").write_text(
        """---
id: reviewer
title: Workspace Reviewer
parent_id: main
---
workspace prompt
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_expert_packs(cwd=isolated_env)}

    assert rows["reviewer"].title == "Workspace Reviewer"
    assert rows["reviewer"].system_prompt == "workspace prompt"
    assert rows["reviewer"].metadata["expert_scope"] == "workspace"


def test_agents_catalog_includes_expert_pack_and_parent_metadata(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("csv_quality.md").write_text(
        """---
id: csv_quality
title: CSV Quality Expert
description: Checks CSV quality
parent_id: analysis
tier: 3
keywords: csv, quality
tools: csv.inspect
prompt_id: clio.expert.csv_quality
profile: light
---
Check CSV schemas and quality.
""",
        encoding="utf-8",
    )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    body = client.get("/v1/agents").json()
    rows = {row["id"]: row for row in body["agents"]}

    assert rows["analysis"]["parent_id"] == "main"
    assert rows["sac_format"]["parent_id"] == "analysis"
    expert = rows["csv_quality"]
    assert expert["source"] == "expert_pack"
    assert expert["parent_id"] == "analysis"
    assert expert["tier"] == 3
    assert expert["prompt_id"] == "clio.expert.csv_quality"
    assert expert["prompt_profile"] == "light"
    assert expert["enabled"] is True
    assert expert["metadata"]["expert_scope"] == "workspace"


def test_agents_catalog_surfaces_disabled_expert_with_missing_parent(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("orphan.md").write_text(
        """---
id: orphan
title: Orphan Expert
parent_id: missing_parent
tier: 2
---
prompt
""",
        encoding="utf-8",
    )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    rows = {row["id"]: row for row in client.get("/v1/agents").json()["agents"]}

    assert rows["orphan"]["enabled"] is False
    assert "parent_id not found: missing_parent" in rows["orphan"]["validation_errors"]


def test_capabilities_advertise_expert_packs(isolated_env: Path, tmp_path: Path) -> None:
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))

    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_expert_packs"] is True


def test_expert_pack_agent_can_be_selected_and_executed(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []
    module = object()

    def fake_module(base_agent: Any, agent_def: Any) -> object:
        del base_agent
        assert agent_def.id == "csv_quality"
        assert agent_def.source == "expert_pack"
        assert agent_def.parent_id == "analysis"
        assert agent_def.prompt_profile == "light"
        return module

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk
        assert kwargs["agent_override"] is module
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent
        calls.append((agent_def.id, question, session_id))
        return type(
            "Pred",
            (),
            {
                "answer": "CSV_QUALITY_OK",
                "selected_expert": agent_def.id,
                "routing_rationale": "session selected expert-pack agent",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._build_prompt_user_agent_module", fake_module)
    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("csv_quality.md").write_text(
        """---
id: csv_quality
title: CSV Quality Expert
parent_id: analysis
tier: 3
profile: light
---
Check CSV schemas and quality.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "expert run", "agent": {"id": "csv_quality"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "inspect data.csv")

    assert calls == [("csv_quality", "inspect data.csv", sid)]
    assert assistant["stop_reason"] == "end_turn"
    assert assistant["parts"][0]["selected_agent"] == "csv_quality"
    assert assistant["parts"][1]["text"] == "CSV_QUALITY_OK"
    assert assistant["metadata"]["stream_fallback"]["reason"] == "dynamic_prompt_stream_unavailable"


def test_expert_pack_agent_executes_delegated_child_expert(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []

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

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent
        calls.append((agent_def.id, question, session_id))
        if agent_def.id == "root_review":
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_OK",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "expert_handoffs": [
                        {
                            "delegate_to": "schema_review",
                            "question": "Inspect the CSV schema",
                            "status": "requested",
                        }
                    ],
                },
            )()
        return type(
            "Pred",
            (),
            {
                "answer": "SCHEMA_OK",
                "selected_expert": agent_def.id,
                "routing_rationale": "delegated child expert",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
---
Coordinate data quality review.
""",
        encoding="utf-8",
    )
    root.joinpath("schema_review.md").write_text(
        """---
id: schema_review
title: Schema Review Expert
parent_id: root_review
tier: 3
---
Inspect schemas.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "delegated expert run", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "inspect data.csv")

    assert calls == [
        ("root_review", "inspect data.csv", sid),
        ("schema_review", "Inspect the CSV schema", sid),
    ]
    handoff = assistant["metadata"]["expert_handoffs"][0]
    assert handoff["agent_id"] == "schema_review"
    assert handoff["parent_id"] == "root_review"
    assert handoff["status"] == "completed"
    assert handoff["execution_mode"] == "prompt_agent"
    assert handoff["output_summary"] == "SCHEMA_OK"
    assert assistant["parts"][1]["type"] == "expert_handoff"
    assert assistant["parts"][1]["metadata"]["status"] == "completed"
    assert assistant["parts"][2]["text"] == "ROOT_OK"
