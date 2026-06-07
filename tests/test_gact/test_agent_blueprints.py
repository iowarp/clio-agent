from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.agent import ClioAgent
from clio_agent.gact.agent_blueprints import (
    DEFAULT_AGENT_BLUEPRINT_ID,
    DEFAULT_REGISTRY_COMMIT,
    DEFAULT_REGISTRY_REF,
    DEFAULT_REGISTRY_URL,
    discover_agent_blueprints,
    load_agent_blueprint_path,
    load_agent_blueprints,
    validate_agent_blueprint_path,
)
from clio_agent.gact.app import (
    _ACTIVE_BLUEPRINT_TOOL_ROWS,
    _ACTIVE_CHILD_TOOL_COMPLETIONS,
    _ACTIVE_GACT_SESSION_ID,
    _append_inferred_workflow_state_from_trajectory,
    _append_nested_workflow_state,
    _append_prediction_workflow_state,
    _append_session_workflow_state_context,
    _augment_ndp_search_result_with_runtime_state,
    _blueprint_fanout_config,
    _blueprint_module_kind,
    _blueprint_runtime_signature,
    _BlueprintTerminalWorkflowState,
    _bubbled_child_evidence_output_summary,
    _build_blueprint_dspy_module,
    _build_child_expert_tool,
    _build_fanout_tool,
    _builtin_agents,
    _coerce_fanout_child_ids,
    _compact_dynamic_delegation_output,
    _completed_row_contract_evidence,
    _continuation_contract_handoffs,
    _delegation_continuation_policy_contract,
    _dynamic_agent_tools,
    _dynamic_answer_has_pending_child_work,
    _dynamic_answer_is_delegation_placeholder,
    _dynamic_child_expert_tools,
    _extract_tools_called_from_trajectory,
    _failed_child_delegation_output_summary,
    _failed_child_delegation_workflow_state,
    _fallback_answer_from_delegation,
    _filter_child_handoffs_by_contract_order,
    _filter_workflow_state_for_blueprint_authority,
    _gact_app_context,
    _gact_turn_timeout_s,
    _infer_ndp_plot_state_from_tool_evidence,
    _infer_ndp_profile_state_from_tool_evidence,
    _infer_ndp_search_state_from_tool_evidence,
    _infer_ndp_station_catalog_state_from_tool_evidence,
    _infer_ndp_workflow_state_from_tool_evidence,
    _infer_ndp_workflow_state_from_tool_rows,
    _infer_ndp_workflow_state_from_trajectory,
    _latest_delegation_output_summary,
    _latest_final_child_output_summary,
    _make_ndp_workflow_tool_interceptor,
    _merge_tool_call_rows,
    _ndp_terminal_workflow_state_final_answer_fallback,
    _next_expert_marker_handoffs,
    _positive_ndp_workflow_state_final_answer_fallback,
    _prediction_structured_metadata,
    _prior_staged_ndp_resource_result,
    _reconcile_workflow_state,
    _recording_blueprint_tool,
    _recover_blueprint_react_tool_intent,
    _run_blueprint_dspy_agent,
    _runtime_dynamic_agent_children_context,
    _sanitize_scan_limited_model_evidence,
    _seed_child_tool_completions_from_resume_prompt,
    _should_execute_delegated_handoff,
    _station_resource_search_state_from_rows,
    _tool_agent_empty_answer_fallback,
    _tool_calls_from_handoff_rows,
    _tool_derived_contract_evidence_for_prediction,
    _tool_session_context,
    _user_agent_bool_param,
    _user_facing_dynamic_evidence_summary,
    _workflow_state_from_handoff_rows,
    _workflow_state_from_outputs,
    build_app,
)
from clio_agent.gact.types import AgentDef
from clio_agent.tools.execution import (
    _workspace_default_tool_arguments,
    tool_workspace_context,
)
from tests.test_gact.conftest import complete_turn


def _write_blueprint(root: Path, blueprint_id: str = "genomics") -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {blueprint_id}
version: 0.1.0
title: Genomics Agent
root_expert: root
defaults:
  prompt_profile: heavy
---
Genomics domain agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Genomics Root
tier: 1
prompt_id: genomics.root
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - memory_search_sessions
prompt_id: genomics.variant
---
Inspect variant evidence.
""",
        encoding="utf-8",
    )


def _write_data_root_blueprint(root: Path, blueprint_id: str = "remote-data") -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {blueprint_id}
version: 0.1.0
title: Remote Data Agent
default_expert: data
---
Remote marketplace agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "data.md").write_text(
        """---
id: data
title: Remote Data Orchestrator
tier: 1
prompt_profile: heavy
---
REMOTE BLUEPRINT ORCHESTRATOR MARKER.
""",
        encoding="utf-8",
    )


def _write_text_contract_blueprint(root: Path, *, allow_text_routing: bool = False) -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        """---
id: text-contract
version: 0.1.0
title: Text Contract Agent
root_expert: root
---
Test blueprint.
""",
        encoding="utf-8",
    )
    allow_line = "      allow_text_routing: true\n" if allow_text_routing else ""
    root.joinpath("experts", "root.md").write_text(
        f"""---
id: root
title: Root
tier: 1
children:
  - analysis
parameters:
  continuation_contracts:
    - id: prose_gate
      when_output_contains:
        - San Diego
        - P475.CI.LY_.20
{allow_line}      match: any
      next_expert: analysis
---
Coordinate work.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "analysis.md").write_text(
        """---
id: analysis
title: Analysis
tier: 2
parent: root
---
Analyze evidence.
""",
        encoding="utf-8",
    )


def _write_default_registry_blueprint(home: Path) -> Path:
    root = home / ".config" / "clio-agent" / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_blueprint(root, blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID)
    root.joinpath(".clio-install.md").write_text(
        "\n".join(
            [
                "# CLIO Agent Blueprint install metadata",
                "",
                f"source: {DEFAULT_REGISTRY_URL}",
                "source_kind: git",
                f"ref: {DEFAULT_REGISTRY_REF}",
                f"commit: {DEFAULT_REGISTRY_COMMIT}",
                f"pinned_commit: {DEFAULT_REGISTRY_COMMIT}",
                "scope: global",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return root


def test_default_registry_agent_blueprint_is_discoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_default_registry_blueprint(tmp_path)
    blueprints = {
        row.id: row
        for row in discover_agent_blueprints(home=tmp_path, cwd=tmp_path / "workspace")
    }
    agents = {
        row.id: row
        for row in load_agent_blueprints(
            home=tmp_path,
            cwd=tmp_path / "workspace",
            blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID,
        )
    }

    assert blueprints[DEFAULT_AGENT_BLUEPRINT_ID].scope == "global"
    assert blueprints[DEFAULT_AGENT_BLUEPRINT_ID].root_expert == "root"
    assert {"root", "variant"} <= set(agents)
    assert agents["variant"].metadata["agent_blueprint_id"] == DEFAULT_AGENT_BLUEPRINT_ID
    assert agents["variant"].metadata["agent_blueprint_scope"] == "global"
    assert "agent_blueprints/builtin" not in agents["variant"].metadata["definition_path"]
    assert blueprints[DEFAULT_AGENT_BLUEPRINT_ID].metadata["install"]["commit"] == DEFAULT_REGISTRY_COMMIT


def test_builtin_agents_are_loaded_from_default_registry_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_default_registry_blueprint(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    agents = {row.id: row for row in _builtin_agents()}

    assert {"root", "variant"} <= set(agents)
    assert agents["root"].source == "expert_pack"
    assert agents["root"].metadata["source_blueprint"] == "default_registry"
    assert "agent_blueprints/builtin" not in agents["root"].metadata["definition_path"]
    assert agents["variant"].metadata["install"]["commit"] == DEFAULT_REGISTRY_COMMIT


def test_agent_blueprint_rejects_free_text_continuation_contracts(tmp_path: Path) -> None:
    blueprint = tmp_path / "text-contract"
    _write_text_contract_blueprint(blueprint)

    rows = {row.id: row for row in load_agent_blueprint_path(blueprint)}
    validation = validate_agent_blueprint_path(blueprint)

    assert rows["root"].enabled is False
    assert any(
        "uses free-text routing predicates" in error
        for error in rows["root"].validation_errors
    )
    assert validation["enabled"] is False
    assert any(
        "root: continuation contract 'prose_gate' uses free-text routing predicates"
        in error
        for error in validation["validation_errors"]
    )


def test_agent_blueprint_allows_explicit_legacy_text_routing_opt_in(tmp_path: Path) -> None:
    blueprint = tmp_path / "text-contract"
    _write_text_contract_blueprint(blueprint, allow_text_routing=True)

    rows = {row.id: row for row in load_agent_blueprint_path(blueprint)}
    validation = validate_agent_blueprint_path(blueprint)

    assert rows["root"].enabled is True
    assert validation["enabled"] is True
    assert not any(
        "uses free-text routing predicates" in error
        for row in rows.values()
        for error in row.validation_errors
    )
    assert any(
        "allow_text_routing" in warning and "legacy migration routing" in warning
        for warning in validation["validation_warnings"]
    )


def test_runtime_text_continuation_policy_requires_legacy_opt_in() -> None:
    agent = AgentDef(
        id="legacy_parent",
        source="expert_pack",
        title="Legacy Parent",
        parameters={
            "continuation_contracts": [
                {
                    "id": "station_filename_gate",
                    "when_output_contains": ["P475.CI.LY_.20"],
                    "next_expert": "analysis",
                }
            ],
        },
    )

    contract = _delegation_continuation_policy_contract(
        agent,
        "The selected station resource was P475.CI.LY_.20.",
    )

    assert contract == {}


def test_runtime_text_continuation_policy_runs_only_with_explicit_legacy_opt_in() -> None:
    agent = AgentDef(
        id="legacy_parent",
        source="expert_pack",
        title="Legacy Parent",
        parameters={
            "allow_legacy_text_continuation": True,
            "continuation_contracts": [
                {
                    "id": "station_filename_gate",
                    "when_output_contains": ["P475.CI.LY_.20"],
                    "next_expert": "analysis",
                    "next_action": "analyze the selected station",
                }
            ],
        },
    )

    contract = _delegation_continuation_policy_contract(
        agent,
        "The selected station resource was P475.CI.LY_.20.",
    )

    assert contract["next_expert"] == "analysis"
    assert contract["next_action"] == "analyze the selected station"
    assert contract["source_policy"] == "station_filename_gate"


def test_workflow_state_normalizes_unicode_hyphens_in_path_fields() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": "/tmp/.clio/artifacts/ndp\u2011staging/MTA1.csv",
                            "source_url": "https://example.test/raw_csv/MTA1.csv",
                        },
                        "artifact": {
                            "status": "ready",
                            "path": "/tmp/.clio/artifacts/plots/MTA1\u2011plot.png",
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["local_path"] == "/tmp/.clio/artifacts/ndp-staging/MTA1.csv"
    assert state["artifact"]["path"] == "/tmp/.clio/artifacts/plots/MTA1-plot.png"


def test_agent_blueprint_respects_boolean_enabled_false(tmp_path: Path) -> None:
    blueprint = tmp_path / "disabled-agent"
    _write_blueprint(blueprint, blueprint_id="disabled-agent")
    blueprint.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Disabled Variant
parent_id: root
tier: 2
enabled: false
---
This expert is intentionally disabled.
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_agent_blueprint_path(blueprint)}

    assert rows["root"].enabled is True
    assert rows["variant"].enabled is False


def test_agent_blueprint_module_kind_and_structured_outputs_parse(tmp_path: Path) -> None:
    root = tmp_path / "react-blueprint"
    _write_blueprint(root, blueprint_id="react-blueprint")
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: React Root
tier: 1
module:
  kind: react
  max_iters: 3
signature:
  inputs: question
  outputs: answer
structured_outputs:
  evidence: true
  artifacts: true
fanout:
  max_workers: 2
tools:
  - memory_search_sessions
---
Coordinate with ReAct.
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_agent_blueprint_path(root)}
    assert rows["root"].module["kind"] == "react"
    assert rows["root"].module["max_iters"] == 3
    assert rows["root"].signature["inputs"] == "question"
    assert rows["root"].structured_outputs["evidence"] is True
    assert rows["root"].fanout["max_workers"] == 2


def test_agent_blueprint_loader_expands_pack_local_includes(tmp_path: Path) -> None:
    root = tmp_path / "included-blueprint"
    _write_blueprint(root, blueprint_id="included-blueprint")
    root.joinpath("AGENT.md").write_text(
        """---
id: included-blueprint
version: 0.1.0
title: Included Blueprint
root_expert: root
experts:
  - experts/root.md
includes:
  - modules/ndp-collector/experts
---
Agent with included module experts.
""",
        encoding="utf-8",
    )
    included = root / "modules" / "ndp-collector" / "experts"
    included.mkdir(parents=True)
    included.joinpath("ndp_catalog.md").write_text(
        """---
id: ndp_catalog
title: NDP Catalog
parent_id: variant
tier: 3
module_kind: react
tools:
  - ndp_search_datasets
---
Search NDP datasets.
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_agent_blueprint_path(root)}

    assert "ndp_catalog" in rows
    assert rows["ndp_catalog"].parent_id == "variant"
    assert rows["ndp_catalog"].metadata["definition_kind"] == "agent_blueprint"
    assert "modules/ndp-collector/experts/ndp_catalog.md" in rows["ndp_catalog"].metadata["definition_path"]


def test_blueprint_runtime_signature_preserves_fields_and_normalizes_structured_outputs() -> None:
    agent_def = AgentDef(
        id="semantic-root",
        source="expert_pack",
        title="Semantic Root",
        signature={
            "inputs": {
                "question": "User request",
                "dataset_summary": "Available dataset summary",
            },
            "outputs": {
                "answer": "User-facing answer",
                "artifact_plan": "Planned artifact work",
            },
        },
        structured_outputs={
            "evidence": "true",
            "artifacts": "false",
            "errors": True,
            "delegation": False,
        },
    )

    signature = _blueprint_runtime_signature(agent_def)

    assert list(signature.input_fields) == ["question", "dataset_summary"]
    assert list(signature.output_fields) == [
        "answer",
        "artifact_plan",
        "workflow_state",
        "evidence",
        "errors",
        "expert_handoffs",
    ]
    assert "artifacts" not in signature.output_fields
    assert "delegation" not in signature.output_fields


def test_blueprint_runtime_signature_preserves_declared_field_types() -> None:
    signature = _blueprint_runtime_signature(
        AgentDef(
            id="typed",
            source="expert_pack",
            title="Typed",
            signature={
                "inputs": {
                    "question": {"description": "User request", "type": "string"},
                    "limit": {"description": "Maximum rows", "type": "integer"},
                    "bbox": {"description": "GeoJSON-like bbox", "type": "array"},
                },
                "outputs": [
                    {"name": "answer", "description": "Final answer", "type": "str"},
                    {"name": "score", "description": "Quality score", "type": "float"},
                    {"name": "metadata", "description": "Structured metadata", "type": "dict"},
                    {"name": "needs_review", "description": "Review flag", "type": "bool"},
                ],
            },
            structured_outputs={
                "workflow_state": False,
                "evidence": False,
                "artifacts": False,
                "errors": False,
                "delegation": False,
                "expert_handoffs": False,
            },
        )
    )

    assert signature.__annotations__["question"] is str
    assert signature.__annotations__["limit"] is int
    assert signature.__annotations__["bbox"] is list
    assert signature.__annotations__["score"] is float
    assert signature.__annotations__["metadata"] is dict
    assert signature.__annotations__["needs_review"] is bool


def test_blueprint_runtime_signature_defaults_empty_declarations_to_question_and_answer() -> None:
    signature = _blueprint_runtime_signature(
        AgentDef(id="data", source="expert_pack", title="Data")
    )

    assert list(signature.input_fields) == ["system_prompt", "question"]
    assert list(signature.output_fields) == [
        "answer",
        "workflow_state",
        "evidence",
        "artifacts",
        "errors",
        "delegation",
        "expert_handoffs",
    ]


@pytest.mark.parametrize(
    ("raw_signature", "expected_inputs", "expected_outputs"),
    [
        ({}, ["system_prompt", "question"], ["answer"]),
        ({"inputs": {}, "outputs": {}}, ["system_prompt", "question"], ["answer"]),
        ({"inputs": [], "outputs": []}, ["system_prompt", "question"], ["answer"]),
        ({"outputs": {"summary": "Short summary"}}, ["system_prompt", "question"], ["summary"]),
        (
            {"inputs": ["question", "file_context"], "outputs": ["answer", "quality_flags"]},
            ["question", "file_context"],
            ["answer", "quality_flags"],
        ),
        (
            {
                "input": [{"name": "question", "description": "User goal"}],
                "output": [{"id": "answer", "desc": "Final answer"}],
            },
            ["question"],
            ["answer"],
        ),
    ],
)
def test_blueprint_runtime_signature_field_declaration_matrix(
    raw_signature: dict[str, Any],
    expected_inputs: list[str],
    expected_outputs: list[str],
) -> None:
    signature = _blueprint_runtime_signature(
        AgentDef(
            id="matrix",
            source="expert_pack",
            title="Matrix",
            signature=raw_signature,
            structured_outputs={
                "workflow_state": False,
                "evidence": False,
                "artifacts": False,
                "errors": False,
                "delegation": False,
                "expert_handoffs": False,
            },
        )
    )

    assert list(signature.input_fields) == expected_inputs
    assert list(signature.output_fields) == expected_outputs


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("disabled", False),
        ("yes", True),
    ],
)
def test_blueprint_structured_output_enablement_matrix(value: Any, enabled: bool) -> None:
    signature = _blueprint_runtime_signature(
        AgentDef(
            id="structured",
            source="expert_pack",
            title="Structured",
            structured_outputs={
                "workflow_state": False,
                "evidence": value,
                "artifacts": False,
                "errors": False,
                "delegation": False,
                "expert_handoffs": False,
            },
        )
    )

    assert ("evidence" in signature.output_fields) is enabled


def test_blueprint_module_kind_rejects_unsupported_values() -> None:
    with pytest.raises(ValueError, match="unsupported module.kind"):
        _blueprint_module_kind(
            AgentDef(
                id="bad",
                source="expert_pack",
                title="Bad",
                module={"kind": "native_python"},
            )
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("", []),
        ("analysis, visualization", ["analysis", "visualization"]),
        ('["analysis", "visualization"]', ["analysis", "visualization"]),
        (["analysis", "visualization"], ["analysis", "visualization"]),
        (("analysis", 7), ["analysis", "7"]),
    ],
)
def test_blueprint_fanout_child_id_coercion_matrix(value: Any, expected: list[str]) -> None:
    assert _coerce_fanout_child_ids(value) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, {"enabled": False, "max_workers": 1, "strategy": "declared_children"}),
        ({"enabled": True, "max_workers": 3}, {"enabled": True, "max_workers": 3, "strategy": "declared_children"}),
        ({"enabled": "false", "max_workers": 0}, {"enabled": False, "max_workers": 1, "strategy": "declared_children"}),
        ({"enabled": "yes", "workers": "2", "strategy": "map_reduce"}, {"enabled": True, "max_workers": 2, "strategy": "map_reduce"}),
    ],
)
def test_blueprint_fanout_config_matrix(raw: dict[str, Any], expected: dict[str, Any]) -> None:
    assert _blueprint_fanout_config(
        AgentDef(id="root", source="expert_pack", title="Root", fanout=raw)
    ) == expected


def test_gact_turn_timeout_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIO_GACT_TURN_TIMEOUT_S", raising=False)
    assert _gact_turn_timeout_s() == 900.0

    monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "0.2")
    assert _gact_turn_timeout_s() == 0.2

    monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "not-a-number")
    assert _gact_turn_timeout_s() == 900.0


def test_user_agent_bool_param_parses_depth_chain_opt_in() -> None:
    assert (
        _user_agent_bool_param(
            AgentDef(
                id="depth",
                source="expert_pack",
                title="Depth",
                parameters={"bubble_child_evidence_on_completion": "true"},
            ),
            "bubble_child_evidence_on_completion",
        )
        is True
    )
    assert (
        _user_agent_bool_param(
            AgentDef(
                id="width",
                source="expert_pack",
                title="Width",
                parameters={"bubble_child_evidence_on_completion": "false"},
            ),
            "bubble_child_evidence_on_completion",
            default=True,
        )
        is False
    )
    assert (
        _user_agent_bool_param(
            AgentDef(id="default", source="expert_pack", title="Default"),
            "bubble_child_evidence_on_completion",
        )
        is False
    )


def test_blueprint_continuation_contract_starts_declared_child_from_request() -> None:
    rows = _continuation_contract_handoffs(
        AgentDef(
            id="main",
            source="expert_pack",
            title="Main",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "start_waveform",
                        "when_request_contains": ["bounded seismic waveform", "NDP", "SAC", "PNG"],
                        "match": "all",
                        "next_expert": "data",
                        "next_action": "discover waveform data",
                        "flags": {"DO_NOT_ASK_USER": "true"},
                    }
                ]
            },
        ),
        source_text="Find bounded seismic waveform evidence through NDP, recover SAC, and create PNG.",
        answer_text="I need more details.",
        completed_outputs=[],
        declared_child_ids={"data", "analysis"},
        completed_child_ids=set(),
    )

    assert rows == [
        {
            "delegate_to": "data",
            "question": "discover waveform data",
            "status": "requested",
            "execute": True,
            "source": "blueprint_continuation_contract",
            "contract_id": "start_waveform",
            "DO_NOT_ASK_USER": "true",
        }
    ]


def test_blueprint_continuation_contract_requires_terms_and_declared_uncompleted_child() -> None:
    agent_def = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        parameters={
            "continuation_contracts": [
                {
                    "id": "to_analysis",
                    "when_output_contains": ["resource_too_large", "no staged local path"],
                    "match": "all",
                    "next_expert": "analysis",
                    "next_action": "run fallback",
                    "flags": {"allow_repeat": True},
                },
                {
                    "id": "to_external",
                    "when_output_contains": ["resource_too_large"],
                    "next_expert": "external_agent",
                },
            ]
        },
    )

    assert not _continuation_contract_handoffs(
        agent_def,
        source_text="request",
        answer_text="resource_too_large only",
        completed_outputs=[],
        declared_child_ids={"analysis"},
        completed_child_ids=set(),
    )
    rows_for_repeat = _continuation_contract_handoffs(
        agent_def,
        source_text="request",
        answer_text="resource_too_large; no staged local path",
        completed_outputs=["resource_too_large; no staged local path"],
        declared_child_ids={"analysis"},
        completed_child_ids={"analysis"},
    )
    assert rows_for_repeat
    assert rows_for_repeat[0]["allow_repeat"] is True

    rows = _continuation_contract_handoffs(
        agent_def,
        source_text="request",
        answer_text="untrusted parent draft",
        completed_outputs=["resource_too_large; no staged local path"],
        declared_child_ids={"analysis"},
        completed_child_ids=set(),
    )

    assert len(rows) == 1
    assert rows[0]["delegate_to"] == "analysis"
    assert rows[0]["source"] == "blueprint_continuation_contract"


def test_blueprint_continuation_contract_returns_first_ordered_transition_only() -> None:
    rows = _continuation_contract_handoffs(
        AgentDef(
            id="main",
            source="expert_pack",
            title="Main",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "to_analysis",
                        "when_output_contains": ["resource_too_large"],
                        "next_expert": "analysis",
                    },
                    {
                        "id": "to_visualization",
                        "when_output_contains": [".sac"],
                        "next_expert": "visualization",
                    },
                ]
            },
        ),
        source_text="request",
        answer_text="untrusted parent draft",
        completed_outputs=["resource_too_large and analysis.sac_format mentioned"],
        declared_child_ids={"analysis", "visualization"},
        completed_child_ids=set(),
    )

    assert [row["delegate_to"] for row in rows] == ["analysis"]


def test_blueprint_continuation_contract_passes_observed_sac_path_to_visualization() -> None:
    rows = _continuation_contract_handoffs(
        AgentDef(
            id="main",
            source="expert_pack",
            title="Main",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "to_visualization",
                        "when_output_contains": ["Trace statistics", ".sac"],
                        "match": "all",
                        "next_expert": "visualization",
                        "next_action": "plot_sac_traces",
                    },
                ]
            },
        ),
        source_text="request",
        answer_text="NDP still mentions OSDF/Pelican blockers.",
        completed_outputs=[
            "Trace statistics computed for "
            "/home/user/clio/tmp/earthscope_IU_ANMO_00_BHZ.sac; "
            "older NDP evidence mentions Pelican."
        ],
        declared_child_ids={"visualization"},
        completed_child_ids=set(),
    )

    assert len(rows) == 1
    assert rows[0]["delegate_to"] == "visualization"
    assert "Runtime-selected local SAC path: /home/user/clio/tmp/earthscope_IU_ANMO_00_BHZ.sac" in rows[0]["question"]
    assert "Call sac_plot_traces with this exact filepath" in rows[0]["question"]


def test_blueprint_continuation_contract_routes_on_typed_state_not_city_or_resource() -> None:
    completed = [
        json.dumps(
            {
                "agent_id": "data",
                "structured": {
                    "evidence": json.dumps(
                        {
                            "workflow_state": {
                                "geospatial": {
                                    "status": "resolved",
                                    "region_name": "Los Angeles, California",
                                    "bbox": [-119.0, 33.2, -117.5, 34.4],
                                },
                                "resource": {
                                    "status": "selected",
                                    "station_id": "LAX1",
                                    "dataset_id": "not-the-san-diego-fixture",
                                },
                            }
                        }
                    )
                },
                "output_summary": "Catalog evidence found for a changed geography.",
            }
        )
    ]
    rows = _continuation_contract_handoffs(
        AgentDef(
            id="main",
            source="expert_pack",
            title="Main",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "resource_to_analysis",
                        "when_state": {
                            "geospatial.status": "resolved",
                            "resource.status": "selected",
                        },
                        "match": "all",
                        "next_expert": "analysis",
                        "next_action": "analyze the selected station resource",
                    }
                ]
            },
        ),
        source_text="Explore recent EarthScope data around a changed city.",
        answer_text="untrusted parent prose",
        completed_outputs=completed,
        declared_child_ids={"analysis"},
        completed_child_ids=set(),
    )

    assert len(rows) == 1
    assert rows[0]["delegate_to"] == "analysis"
    assert rows[0]["source"] == "blueprint_typed_state_continuation_contract"
    assert "Los Angeles, California" in rows[0]["question"]
    assert "not-the-san-diego-fixture" in rows[0]["question"]


def test_blueprint_continuation_contract_does_not_route_unresolved_typed_state() -> None:
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "geospatial": {"status": "unsupported", "region_name": "Atlantis"},
                    "resource": {"status": "missing"},
                }
            }
        )
    ]

    assert not _continuation_contract_handoffs(
        AgentDef(
            id="main",
            source="expert_pack",
            title="Main",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "resource_to_analysis",
                        "when_state": {
                            "geospatial.status": "resolved",
                            "resource.status": "selected",
                        },
                        "match": "all",
                        "next_expert": "analysis",
                    }
                ]
            },
        ),
        source_text="Explore a no-data region.",
        answer_text="resolved selected",
        completed_outputs=completed,
        declared_child_ids={"analysis"},
        completed_child_ids=set(),
    )


def test_blueprint_continuation_contract_routes_metadata_only_to_synthesis_not_analysis() -> None:
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "resource_candidate": {"status": "metadata_only"},
                    "acquisition": {
                        "status": "metadata_only",
                        "analysis_ready": False,
                        "metadata_path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                    },
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="main",
            source="expert_pack",
            title="Main",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "data_to_analysis",
                        "when_child_completed": "data",
                        "when_state": {
                            "acquisition.status": "staged",
                            "acquisition.analysis_ready": True,
                        },
                        "match": "all",
                        "next_expert": "analysis",
                    },
                    {
                        "id": "data_blocked_to_synthesis",
                        "when_child_completed": "data",
                        "when_state": {
                            "acquisition.status": {
                                "in": ["metadata_only", "blocked", "missing"]
                            }
                        },
                        "match": "all",
                        "next_expert": "synthesis",
                    },
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text="metadata evidence found",
        completed_outputs=completed,
        declared_child_ids={"analysis", "synthesis"},
        completed_child_ids={"data"},
    )

    assert [row["delegate_to"] for row in rows] == ["synthesis"]
    assert rows[0]["source"] == "blueprint_typed_state_continuation_contract"
    assert "earthscope_converted_data.csv" in rows[0]["question"]


def test_blueprint_continuation_contract_routes_analysis_ready_acquisition_to_analysis(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "not_fixture_station.csv"
    staged_csv.parent.mkdir(parents=True)
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "resource_candidate": {
                        "status": "selected",
                        "dataset_id": "changed-dataset",
                        "resource_name": "not_fixture_station.csv",
                    },
                    "acquisition": {
                        "status": "staged",
                        "analysis_ready": True,
                        "local_path": str(staged_csv),
                    },
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="main",
            source="expert_pack",
            title="Main",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "data_to_analysis",
                        "when_child_completed": "data",
                        "when_state": {
                            "acquisition.status": "staged",
                            "acquisition.analysis_ready": True,
                        },
                        "match": "all",
                        "next_expert": "analysis",
                    },
                    {
                        "id": "data_blocked_to_synthesis",
                        "when_child_completed": "data",
                        "when_state": {
                            "acquisition.status": {
                                "in": ["metadata_only", "blocked", "missing"]
                            }
                        },
                        "match": "all",
                        "next_expert": "synthesis",
                    },
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text="staged evidence found",
        completed_outputs=completed,
        declared_child_ids={"analysis", "synthesis"},
        completed_child_ids={"data"},
    )

    assert [row["delegate_to"] for row in rows] == ["analysis"]
    assert "not_fixture_station.csv" in rows[0]["question"]


def test_blueprint_continuation_contract_rejects_missing_analysis_ready_path() -> None:
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "resource_candidate": {
                        "status": "selected",
                        "resource_name": "missing_station.csv",
                    },
                    "acquisition": {
                        "status": "staged",
                        "analysis_ready": True,
                        "local_path": "/tmp/clio-missing-staged/missing_station.csv",
                    },
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="main",
            source="expert_pack",
            title="Main",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "data_to_analysis",
                        "when_child_completed": "data",
                        "when_state": {
                            "acquisition.status": "staged",
                            "acquisition.analysis_ready": True,
                        },
                        "match": "all",
                        "next_expert": "analysis",
                    }
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text="staged evidence found",
        completed_outputs=completed,
        declared_child_ids={"analysis"},
        completed_child_ids={"data"},
    )

    assert rows == []


def test_workflow_state_merge_preserves_staged_acquisition_over_metadata_only(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "changed_station.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {"status": "metadata_only"},
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                            "metadata_path": "/workspace/earthscope_converted_data.csv",
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "changed_station.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {"status": "metadata_only"},
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                            "metadata_path": "/workspace/old_metadata.csv",
                        },
                    }
                }
            ),
        ]
    )

    assert state["resource_candidate"]["status"] == "selected"
    assert state["resource_candidate"]["resource_name"] == "changed_station.csv"
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"] == str(staged_csv)


def test_geospatial_workflow_state_authority_drops_catalog_claims() -> None:
    state = _filter_workflow_state_for_blueprint_authority(
        "geospatial",
        {
            "geospatial": {
                "status": "resolved",
                "center_lat": 34.05,
                "center_lon": -118.25,
                "radius_km": 75,
            },
            "region": {
                "center": {"lat": 34.05, "lon": -118.25},
                "radius_km": 75,
            },
            "catalog": {
                "status": "metadata_only",
                "stations": [{"id": "P056", "lat": 33.99, "lon": -118.3}],
            },
            "resource_candidate": {
                "status": "selected",
                "resource_name": "P056.PW.LY_.00.csv",
            },
            "acquisition": {
                "status": "staged",
                "analysis_ready": True,
                "local_path": "/tmp/P056.PW.LY_.00.csv",
            },
        },
    )

    assert set(state) == {"geospatial", "region"}
    assert state["geospatial"]["status"] == "resolved"
    assert "catalog" not in state
    assert "resource_candidate" not in state
    assert "acquisition" not in state


def test_event_catalog_authority_synthesizes_typed_blocker_from_metadata_only_catalog() -> None:
    state = _filter_workflow_state_for_blueprint_authority(
        "seismic_event_catalog",
        {
            "acquisition": {
                "status": "staged",
                "analysis_ready": True,
                "local_path": "/tmp/JPLM.PW.LY_.00.csv",
            },
            "catalog": {"status": "metadata_found"},
            "profile": {"status": "complete", "rows_scanned": 250000, "scan_limited": True},
            "station_catalog": {
                "status": "ranked_metadata_only",
                "stations": [{"station": "JPLM", "distance_km": 2.519}],
            },
        },
    )

    assert set(state) == {"event_context"}
    assert state["event_context"] == {
        "status": "blocked",
        "blocker": "no live event catalog tool available in this pack",
        "verified_event_count": None,
        "limitations": ["no_live_event_catalog_tool"],
        "next_action": (
            "add or call a live earthquake/event catalog tool for event counts, "
            "magnitudes, and dates"
        ),
    }


def test_event_catalog_authority_preserves_typed_event_context() -> None:
    state = _filter_workflow_state_for_blueprint_authority(
        "seismic_event_catalog",
        {
            "catalog": {"status": "metadata_found"},
            "event_context": {
                "status": "available",
                "verified_event_count": 3,
                "source": "event_catalog_tool",
            },
            "profile": {"status": "complete"},
        },
    )

    assert state == {
        "event_context": {
            "status": "available",
            "verified_event_count": 3,
            "source": "event_catalog_tool",
        }
    }


def test_event_catalog_authority_empty_state_is_typed_blocker() -> None:
    state = _filter_workflow_state_for_blueprint_authority(
        "seismic_event_catalog",
        {},
    )

    assert state["event_context"]["status"] == "blocked"
    assert state["event_context"]["verified_event_count"] is None
    assert state["event_context"]["limitations"] == ["no_live_event_catalog_tool"]


def test_workflow_state_reconcile_sanitizes_unverified_geospatial_provenance() -> None:
    state: dict[str, Any] = {
        "geospatial": {
            "status": "resolved",
            "center": {"lat": 34.2, "lon": -118.4},
            "provenance": "USGS basin outline + UNAVCO GNSS station footprints",
        },
        "region": {
            "center": {"lat": 34.2, "lon": -118.4},
            "provenance": "EarthScope NDP GNSS station catalogue",
        },
    }

    _reconcile_workflow_state(state)

    assert state["geospatial"]["provenance"] == "model_geographic_prior"
    assert state["region"]["provenance"] == "model_geographic_prior"
    assert "no geocoder/source tool was called" in state["geospatial"]["warnings"][0]


def test_workflow_state_reconcile_converts_no_tool_event_catalog_to_blocker() -> None:
    state: dict[str, Any] = {
        "event_catalog": {
            "status": "unavailable",
            "reason": "No seismic event data present; only GNSS time-series available",
        }
    }

    _reconcile_workflow_state(state)

    assert "event_catalog" not in state
    assert state["event_context"] == {
        "status": "blocked",
        "blocker": "no live event catalog tool available in this pack",
        "verified_event_count": None,
        "limitations": ["no_live_event_catalog_tool"],
        "next_action": (
            "add or call a live earthquake/event catalog tool for event counts, "
            "magnitudes, and dates"
        ),
    }


def test_workflow_state_reconcile_converts_missing_event_catalog_resource_to_blocker() -> None:
    state: dict[str, Any] = {
        "event_catalog": {
            "status": "metadata_found",
            "resource_status": "missing",
        }
    }

    _reconcile_workflow_state(state)

    assert "event_catalog" not in state
    assert state["event_context"]["status"] == "blocked"
    assert state["event_context"]["verified_event_count"] is None
    assert state["event_context"]["limitations"] == ["no_live_event_catalog_tool"]


def test_scan_limited_model_evidence_sanitizer_removes_unsupported_record_claims() -> None:
    output = """
Selected GNSS station - VDCY.
Suitability: SUITABLE (active, horizontal uncertainty ~= 0.035 m, vertical 0.089 m, 1 Hz sampling)
Rows scanned: 250000
The profile was scan_limited, so this was a two-week record with no large data gaps and per-epoch noise.
Low-rate GNSS displacements (<= 1 Hz) are enough here and uncertainty columns indicate sub-centimetre precision.
The scan was limited to the first 250 k rows (≈ 1.4 h of data).
Staged CSV (GNSS 1 Hz time-series): /tmp/MTA1.csv
This CSV contains GNSS station metadata and high-rate (1 Hz) 3-D position time-series for nearby stations.
Rows scanned: 250 000 (≈ 2.9 days at 1 Hz).
| Temporal cadence | ≈ 1 s (timestamps step ~1000 ms) | high-rate |
| Sampled time span | 5 070 s (≈ 1.4 h) - scan limited to first 250 k rows |
- Cadence: 1 s
**Sampling cadence**: 1 Hz (≈ 1000 ms intervals)
The CSV shows a 1 Hz cadence (1000 ms between samples).
The plot shows a representative sample and may not capture the entire time span or any data gaps present in the full record.
Note catalog limitations (single-frequency, possible gaps, 1 Hz public cadence).
Note catalog limitations (e.g., only 1 Hz public streams, possible gaps).
| **Sampling cadence** | ~1 Hz (Δt = 1000 ms) over the scanned interval; suitable for rapid deformation detection. |
| **Overall data quality** | High – suitable for displacement, velocity, or strain analysis. |
| Scan-limited profile | 250 k rows examined - no immediate issues. |
MTA1’s dataset meets all required format and quality criteria, making it suitable for immediate GNSS-based investigations.
No missing values were detected in a 5 k-row sample.
Estimated cadence: ~55 Hz.
Temporal density: high (≈55 Hz).
Noise level: low (σ ≈ 0.03 m E/N, 0.065 m U).
Overall suitability: high for local deformation or geodetic analysis.
Region definition derived from USGS seismic-hazard maps and PBO station coverage.
Time coverage: ≈ 5 days, continuous (no obvious gaps in sampled rows).
Quality flag qChannel consistent across the record.
Missing values: 0 % (all required fields present).
Station suitability (MTA1 - SCGN).
Conclusion: Station MTA1 provides optimal spatial coverage and meets quality criteria for GNSS-based deformation analysis.
The staged CSV and PNG artifact are ready for downstream modeling.
Assessment note: high‑quality, gap‑free data within 1 km of the region centre.
Coverage rating: moderate (sufficient for basin-scale analysis).
"""

    sanitized = _sanitize_scan_limited_model_evidence(output)

    assert "horizontal uncertainty ~= 0.035 m" in sanitized
    assert "vertical 0.089 m" in sanitized
    assert "1 Hz sampling" not in sanitized
    assert "two-week record" not in sanitized
    assert "no large data gaps" not in sanitized
    assert "<= 1 Hz" not in sanitized
    assert "sub-centimetre precision" not in sanitized
    assert "per-epoch noise" not in sanitized
    assert "1.4 h of data" not in sanitized
    assert "GNSS 1 Hz time-series" not in sanitized
    assert "high-rate (1 Hz) 3-D position time-series" not in sanitized
    assert "2.9 days at 1 Hz" not in sanitized
    assert "Temporal cadence" not in sanitized
    assert "Sampled time span" not in sanitized
    assert "Cadence: 1 s" not in sanitized
    assert "Sampling cadence" not in sanitized
    assert "CSV shows a 1 Hz cadence" not in sanitized
    assert "1 Hz public cadence" not in sanitized
    assert "1 Hz public streams" not in sanitized
    assert "over the scanned interval" not in sanitized
    assert "Overall data quality" not in sanitized
    assert "no immediate issues" not in sanitized
    assert "meets all required format and quality criteria" not in sanitized
    assert "No missing values were detected" not in sanitized
    assert "Estimated cadence" not in sanitized
    assert "Temporal density" not in sanitized
    assert "Noise level" not in sanitized
    assert "Overall suitability" not in sanitized
    assert "USGS seismic-hazard maps" not in sanitized
    assert "PBO station coverage" not in sanitized
    assert "Time coverage" not in sanitized
    assert "qChannel consistent" not in sanitized
    assert "Missing values: 0 %" not in sanitized
    assert "Station suitability" not in sanitized
    assert "optimal spatial coverage" not in sanitized
    assert "meets quality criteria" not in sanitized
    assert "ready for downstream modeling" not in sanitized
    assert "Assessment note" not in sanitized
    assert "high‑quality" not in sanitized
    assert "gap‑free" not in sanitized
    assert "Coverage rating" not in sanitized
    assert "sufficient for basin" not in sanitized
    assert "full record" not in sanitized
    assert "full-file cadence/duration/gap quality was not verified" in sanitized


def test_workflow_state_reconcile_sanitizes_scan_limited_quality_shortcuts() -> None:
    state: dict[str, Any] = {
        "profile": {
            "status": "complete",
            "rows_scanned": 250000,
            "rows_profiled": 5000,
            "numeric_summary_rows": 5000,
            "scan_limited": True,
            "profile_limited": True,
            "missing_values": {"time": 0, "east": 0},
            "missing_values_rows": 5000,
            "missing_values_scope": "profiled_rows",
        },
        "selected_station": {
            "station_id": "MTA1",
            "columns_ok": True,
            "missing_values": False,
            "suitability": "high",
            "data_quality": "excellent",
        },
        "assessment": {
            "suitability": "high",
            "coverage": "moderate",
            "coverage_rating": "moderate",
            "missing_values_percent": 0,
            "notes": "high-quality, gap-free data",
            "quality_consistent": True,
            "time_coverage_days": 5,
            "data_quality": {
                "cadence_hz_estimated": 55,
                "missing_values": False,
                "noise_level": "low",
                "uncertainty_m": {"east": 0.033},
            },
            "selected_station": {
                "station_id": "MTA1",
                "missing_values": False,
                "suitability": "high",
            }
        },
    }

    _reconcile_workflow_state(state)

    assert state["profile"]["missing_values_scope"] == "profiled_rows"
    assert state["profile"]["missing_values_rows"] == 5000
    assert "missing_values" not in state["selected_station"]
    assert "suitability" not in state["selected_station"]
    assert "data_quality" not in state["selected_station"]
    assert "missing_values" not in state["assessment"]["selected_station"]
    assert "suitability" not in state["assessment"]["selected_station"]
    assert "suitability" not in state["assessment"]
    assert "data_quality" not in state["assessment"]
    assert "coverage" not in state["assessment"]
    assert "coverage_rating" not in state["assessment"]
    assert "missing_values_percent" not in state["assessment"]
    assert "notes" not in state["assessment"]
    assert "quality_consistent" not in state["assessment"]
    assert "time_coverage_days" not in state["assessment"]


def test_positive_ndp_fallback_replaces_unsupported_scan_limited_final_claims() -> None:
    state: dict[str, Any] = {
        "acquisition": {
            "status": "staged",
            "analysis_ready": True,
            "local_path": "/tmp/MTA1.CI.LY_.30.csv",
            "source_url": "https://example.test/MTA1.CI.LY_.30.csv",
            "required_columns": ["time", "east", "north", "up"],
        },
        "resource_candidate": {
            "station_id": "MTA1",
            "station_distance_km": 0.713,
            "geographically_grounded": True,
        },
        "profile": {
            "status": "complete",
            "columns": ["time", "east", "north", "up", "sigEE"],
            "numeric_columns": ["time", "east", "north", "up", "sigEE"],
            "rows_scanned": 250000,
            "rows_profiled": 5000,
            "numeric_summary_rows": 5000,
            "scan_limited": True,
            "profile_limited": True,
            "missing_values": {"time": 0, "east": 0, "north": 0, "up": 0},
            "missing_values_rows": 5000,
            "missing_values_scope": "profiled_rows",
        },
        "artifact": {
            "path": "/tmp/MTA1.CI.LY_.30_plot.png",
            "columns": ["east", "north", "up"],
            "status": "ready",
        },
    }
    answer = (
        "Resource MTA1.CI.LY_.30.csv has 30-second sampling. "
        "Rows examined: 250000 at 30 s cadence. "
        "Missing values: 0% in all required columns. "
        "`qChannel` is a quality flag where 0 = good. "
        "Typical GNSS daily solutions have uncertainties of a few mm. "
        "The CSV contains a continuous time series. "
        "Overall data quality is high."
    )

    fallback = _positive_ndp_workflow_state_final_answer_fallback(answer, state)

    assert fallback
    assert "30-second sampling" not in fallback
    assert "30 s cadence" not in fallback
    assert "0 = good" not in fallback
    assert "Typical GNSS daily solutions" not in fallback
    assert "continuous time series" not in fallback
    assert "0% missing" not in fallback
    assert "high" not in fallback.casefold()
    assert "rows scanned: 250000" in fallback
    assert "rows profiled for numeric summary: 5000" in fallback
    assert "scope: profiled_rows" in fallback
    assert "Full-file cadence, duration, gaps" in fallback


def test_positive_ndp_fallback_uses_handoff_wrapped_workflow_state() -> None:
    rows = [
        {
            "agent_id": "synthesis",
            "status": "completed",
            "output_summary": json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": "/tmp/JPLM.PW.LY_.00.csv",
                            "source_url": "https://example.test/JPLM.PW.LY_.00.csv",
                            "required_columns": ["time", "east", "north", "up"],
                        },
                        "resource_candidate": {
                            "station_id": "JPLM",
                            "station_distance_km": 2.519,
                            "geographically_grounded": True,
                        },
                        "profile": {
                            "status": "complete",
                            "columns": ["time", "east", "north", "up", "sigEE"],
                            "numeric_columns": ["time", "east", "north", "up", "sigEE"],
                            "rows_scanned": 250000,
                            "rows_profiled": 5000,
                            "numeric_summary_rows": 5000,
                            "scan_limited": True,
                            "profile_limited": True,
                            "missing_values": {"time": 0, "east": 0, "north": 0, "up": 0},
                            "missing_values_rows": 5000,
                            "missing_values_scope": "profiled_rows",
                        },
                        "artifact": {
                            "path": "/tmp/JPLM.PW.LY_.00_plot.png",
                            "columns": ["east", "north", "up"],
                            "status": "ready",
                        },
                    }
                }
            ),
        }
    ]
    state = _workflow_state_from_handoff_rows(rows)

    fallback = _positive_ndp_workflow_state_final_answer_fallback(
        "The file contains about 250000 rows from a full scan, "
        "with no missing values in any column and qChannel 0 = good.",
        state,
    )

    assert fallback
    assert "JPLM" in fallback
    assert "full scan" not in fallback.casefold()
    assert "no missing values in any column" not in fallback.casefold()
    assert "0 = good" not in fallback
    assert "scope: profiled_rows" in fallback


def test_compact_dynamic_delegation_output_sanitizes_scan_limited_evidence() -> None:
    output = "\n".join(
        [
            "Profile evidence",
            "Rows scanned: 250000",
            "scan_limited: true",
            "Suitability: SUITABLE (active, horizontal uncertainty ~= 0.035 m, vertical 0.089 m, 1 Hz sampling)",
            "Estimated cadence: ~55 Hz.",
            "Noise level: low (sigma ~= 0.03 m).",
            "Overall suitability: high for local deformation analysis.",
            "Region definition derived from USGS seismic-hazard maps and PBO station coverage.",
            "Time coverage: 5 days, continuous (no obvious gaps in sampled rows).",
            "Quality flag qChannel consistent across the record.",
            "Conclusion: Station MTA1 provides optimal spatial coverage and meets quality criteria.",
            "The staged CSV and PNG artifact are ready for downstream modeling.",
            "Assessment note: high-quality, gap-free data within 1 km.",
            "Coverage rating: moderate (sufficient for basin-scale analysis).",
            *[f"filler line {index}" for index in range(220)],
        ]
    )

    compacted = _compact_dynamic_delegation_output(output, limit=800)

    assert "Rows scanned: 250000" in compacted
    assert "horizontal uncertainty ~= 0.035 m" in compacted
    assert "1 Hz sampling" not in compacted
    assert "Estimated cadence" not in compacted
    assert "Noise level" not in compacted
    assert "Overall suitability" not in compacted
    assert "USGS seismic-hazard maps" not in compacted
    assert "PBO station coverage" not in compacted
    assert "Time coverage" not in compacted
    assert "qChannel consistent" not in compacted
    assert "optimal spatial coverage" not in compacted
    assert "meets quality criteria" not in compacted
    assert "ready for downstream modeling" not in compacted
    assert "Assessment note" not in compacted
    assert "high-quality" not in compacted
    assert "gap-free" not in compacted
    assert "Coverage rating" not in compacted
    assert "sufficient for basin" not in compacted
    assert "full-file cadence/duration/gap quality was not verified" in compacted


def test_compact_dynamic_delegation_output_retains_reconciled_workflow_state(
    tmp_path: Path,
) -> None:
    staged_path = tmp_path / "BRAN.CI.LY_.30.csv"
    staged_path.write_text("time,east,north,up\n1,0,0,0\n", encoding="utf-8")
    output = "\n".join(
        [
            "Early child state.",
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "blocked",
                            "analysis_ready": False,
                            "local_path": str(staged_path),
                            "required_columns": ["time", "east", "north", "up"],
                            "source_url": "https://example.test/BRAN.CI.LY_.30.csv",
                        },
                        "resource_candidate": {
                            "status": "missing",
                            "resource_name": "BRAN.CI.LY_.30.csv",
                        },
                    }
                }
            ),
            "Later child state.",
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_path),
                            "required_columns": ["time", "east", "north", "up"],
                            "source_url": "https://example.test/BRAN.CI.LY_.30.csv",
                        },
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "BRAN.CI.LY_.30.csv",
                        },
                    }
                }
            ),
            *[f"filler line {index}" for index in range(220)],
        ]
    )

    compacted = _compact_dynamic_delegation_output(output, limit=800)

    assert '"status": "staged"' in compacted
    assert '"analysis_ready": true' in compacted
    assert '"status": "selected"' in compacted
    assert '"status": "blocked"' not in compacted
    assert '"status": "missing"' not in compacted


def test_compact_dynamic_delegation_output_prefers_scan_limited_typed_state(
    tmp_path: Path,
) -> None:
    staged_path = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_path.write_text("time,east,north,up\n1,0,0,0\n", encoding="utf-8")
    output = "\n".join(
        [
            "Assessment note: high-quality, gap-free data within 1 km.",
            "Coverage rating: moderate (sufficient for basin-scale analysis).",
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_path),
                            "required_columns": ["time", "east", "north", "up"],
                            "source_url": "https://example.test/MTA1.CI.LY_.30.csv",
                        },
                        "profile": {
                            "status": "complete",
                            "scan_limited": True,
                            "profile_limited": True,
                            "rows_scanned": 250000,
                            "rows_profiled": 5000,
                            "missing_values": {
                                "time": 0,
                                "east": 0,
                                "north": 0,
                                "up": 0,
                            },
                            "missing_values_rows": 5000,
                            "missing_values_scope": "profiled_rows",
                        },
                        "assessment": {
                            "coverage": "moderate",
                            "notes": "high-quality, gap-free data",
                        },
                    }
                }
            ),
            *[f"filler line {index}" for index in range(220)],
        ]
    )

    compacted = _compact_dynamic_delegation_output(output, limit=800)

    assert compacted.startswith("Retained typed workflow state:")
    assert '"scan_limited": true' in compacted
    assert '"rows_scanned": 250000' in compacted
    assert '"missing_values_scope": "profiled_rows"' in compacted
    assert "Assessment note" not in compacted
    assert "Coverage rating" not in compacted
    assert "high-quality" not in compacted
    assert '"coverage"' not in compacted
    assert '"notes"' not in compacted


def test_compact_dynamic_delegation_output_sanitizes_unverified_geospatial_prose() -> None:
    output = "\n".join(
        [
            "Region: circle centered at 34.2, -118.25.",
            "Region summary: radius 50 km (USGS Los Angeles Basin definition; high confidence).",
            "Circular region: centre 34.05 N / -118.25 W, radius 50 km (high confidence, model-derived prior).",
            "Provenance: USGS Los Angeles Basin geomorphology description; SCEC Southern California regional model extents.",
            "Method: derived from USGS basin boundary polygon, simplified to enclosing circle",
            '{"workflow_state": {"geospatial": {"provenance": "Derived from PBO GNSS stations and USGS basin definition"}}}',
            *[f"filler line {index}" for index in range(220)],
        ]
    )

    compacted = _compact_dynamic_delegation_output(output, limit=800)

    assert "USGS Los Angeles Basin definition" not in compacted
    assert "USGS Los Angeles Basin geomorphology" not in compacted
    assert "SCEC Southern California" not in compacted
    assert "USGS basin boundary polygon" not in compacted
    assert "PBO GNSS stations" not in compacted
    assert "high confidence" not in compacted
    assert "model_geographic_prior" in compacted
    assert "model-derived geography" in compacted
    assert "no source/geocoder tool evidence retained" in compacted


def test_compact_dynamic_delegation_output_sanitizes_event_context_no_events_claims() -> None:
    output = "\n".join(
        [
            "**Event-catalog capability status:**",
            "- **Catalog generation:** **Not yet performed** - no events have been detected or recorded.",
            "Only descriptive information is present; no seismic or deformation events have been catalogued.",
            "No detection algorithm applied - without applying a detector, no events can be cataloged.",
            "No event-detection has been performed, so the catalog contains zero events.",
            "No event extraction - catalog contains no events, no event timestamps, magnitudes, or locations.",
            json.dumps(
                {
                    "workflow_state": {
                        "event_context": {
                            "status": "blocked",
                            "blocker": "no live event catalog tool available in this pack",
                            "verified_event_count": None,
                            "limitations": ["no_live_event_catalog_tool"],
                        }
                    }
                }
            ),
            *[f"filler line {index}" for index in range(220)],
        ]
    )

    compacted = _compact_dynamic_delegation_output(output, limit=800)

    assert "no events have been detected" not in compacted
    assert "no seismic or deformation events" not in compacted
    assert "no events can be cataloged" not in compacted
    assert "zero events" not in compacted
    assert "catalog contains no events" not in compacted
    assert "no live event-catalog tool was available" in compacted
    assert "no live event-catalog evidence was available" in compacted
    assert '"status": "blocked"' in compacted
    assert '"no_live_event_catalog_tool"' in compacted


def test_workflow_state_merge_clears_stale_acquisition_blocker_after_staging(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "resource_discovery": {
                            "status": "search_required",
                            "reason": "station time-series resource still has to be discovered",
                        },
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                            "blocker": "staged resource is station metadata, not a GNSS time-series CSV",
                            "metadata_path": "/workspace/earthscope_converted_data.csv",
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "resource_url": "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "blocker": "staged resource is station metadata, not a GNSS time-series CSV",
                            "local_path": str(staged_csv),
                            "source_url": "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv",
                        },
                    }
                }
            ),
        ]
    )

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"] == str(staged_csv)
    assert "blocker" not in state["acquisition"]
    assert state["resource_discovery"]["status"] == "resource_found"


def test_workflow_state_merge_preserves_non_empty_tool_provenance(tmp_path: Path) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                            "dataset_name": "mta1-ci-ly-30",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "resource_url": "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv",
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "dataset_id": "",
                            "dataset_name": "",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "resource_url": "",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": "",
                        },
                    }
                }
            ),
        ]
    )

    assert state["resource_candidate"]["dataset_id"] == "1b0c1b93-f164-4025-bd7b-000252b5ca18"
    assert state["resource_candidate"]["resource_url"] == "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv"
    assert state["acquisition"]["source_url"] == "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv"


def test_regional_station_csv_requires_filtered_station_metadata_provenance(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "WWMT.CI.LY_.40.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "geospatial": {
                            "status": "resolved",
                            "region_name": "Bay Area, California",
                            "center_lat": 37.77,
                            "center_lon": -122.42,
                            "radius_km": 75,
                        },
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "WWMT.CI.LY_.40.csv",
                            "resource_url": "https://ds2.example.test/raw_csv/WWMT.CI.LY_.40.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": "https://ds2.example.test/raw_csv/WWMT.CI.LY_.40.csv",
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is False
    assert "filtered station metadata" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["station_id"] == "WWMT"
    assert state["resource_candidate"]["geographically_grounded"] is False


def test_regional_station_csv_requires_filtered_metadata_with_geography_state(
    tmp_path: Path,
) -> None:
    """Regression for r94: models may call the resolved region `geography`."""

    staged_csv = tmp_path / "LL01.PW.LY_.00.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "geography": {
                            "type": "Point",
                            "coordinates": [-118.25, 34.05],
                            "radius_km": 50,
                        },
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "LL01.PW.LY_.00.csv",
                            "resource_url": "https://ds2.example.test/raw_csv/LL01.PW.LY_.00.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": "https://ds2.example.test/raw_csv/LL01.PW.LY_.00.csv",
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["analysis_ready"] is False
    assert "filtered station metadata" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["station_id"] == "LL01"
    assert state["resource_candidate"]["geographically_grounded"] is False


def test_regional_station_csv_is_analysis_ready_when_station_metadata_matches(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "UCSF.CI.LY_.40.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "geospatial": {
                            "status": "resolved",
                            "region_name": "Bay Area, California",
                            "center_lat": 37.77,
                            "center_lon": -122.42,
                            "radius_km": 75,
                        },
                        "station_catalog": {
                            "status": "ranked_metadata_only",
                            "stations": [
                                {
                                    "station": "UCSF",
                                    "latitude": 37.7637,
                                    "longitude": -122.4587,
                                    "distance_km": 3.5,
                                }
                            ],
                        },
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "UCSF.CI.LY_.40.csv",
                            "resource_url": "https://ds2.example.test/raw_csv/UCSF.CI.LY_.40.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": "https://ds2.example.test/raw_csv/UCSF.CI.LY_.40.csv",
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert "blocker" not in state["acquisition"]
    assert state["resource_candidate"]["station_id"] == "UCSF"
    assert state["resource_candidate"]["geographically_grounded"] is True
    assert state["resource_candidate"]["station_distance_km"] == 3.5


def test_later_synthesis_state_cannot_downgrade_tool_grounded_station_csv(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "geospatial": {
                            "status": "resolved",
                            "center_lat": 34.05,
                            "center_lon": -118.25,
                            "radius_km": 50,
                        },
                        "station_catalog": {
                            "status": "ranked_metadata_only",
                            "stations": [
                                {
                                    "station": "MTA1",
                                    "latitude": 34.05522077,
                                    "longitude": -118.24550778,
                                    "distance_km": 0.713,
                                }
                            ],
                        },
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "resource_url": (
                                "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv"
                            ),
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": (
                                "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv"
                            ),
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "station_id": "MTA1",
                            "station_distance_km": 0.713,
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "geographically_grounded": False,
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": False,
                            "local_path": str(staged_csv),
                            "blocker": (
                                "staged station CSV lacks geographic provenance "
                                "from the filtered station metadata for the requested region"
                            ),
                        },
                    }
                }
            ),
        ]
    )

    assert state["acquisition"]["analysis_ready"] is True
    assert "blocker" not in state["acquisition"]
    assert state["resource_candidate"]["station_id"] == "MTA1"
    assert state["resource_candidate"]["geographically_grounded"] is True
    assert state["resource_candidate"]["station_distance_km"] == 0.713


def test_station_csv_matching_station_metadata_sets_grounded_candidate_without_geospatial(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "station_catalog": {
                            "status": "ranked_metadata_only",
                            "stations": [
                                {
                                    "station": "MTA1",
                                    "latitude": 34.046,
                                    "longitude": -118.25,
                                    "distance_km": 0.7,
                                }
                            ],
                        },
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["analysis_ready"] is True
    assert state["resource_candidate"]["station_id"] == "MTA1"
    assert state["resource_candidate"]["geographically_grounded"] is True
    assert state["resource_candidate"]["station_distance_km"] == 0.7


def test_ndp_trajectory_state_marks_station_csv_grounded_from_filter_and_stage(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _infer_ndp_workflow_state_from_trajectory(
        {
            "observation_0": {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "center": {"latitude": 34.05, "longitude": -118.25},
                "radius_km": 75,
                "within_radius_count": 1,
                "stations": [
                    {
                        "station": "MTA1",
                        "latitude": 34.046,
                        "longitude": -118.25,
                        "distance_km": 0.7,
                    }
                ],
                "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
            },
            "observation_1": {
                "staged": True,
                "path": str(staged_csv),
                "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "dataset_name": "mta1-ci-ly-30",
                "resource_name": "MTA1.CI.LY_.30.csv",
                "source_url": "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv",
            },
        }
    )

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["resource_candidate"]["station_id"] == "MTA1"
    assert state["resource_candidate"]["geographically_grounded"] is True
    assert state["resource_candidate"]["station_distance_km"] == 0.7


def test_ndp_trajectory_stage_args_preserve_station_resource_provenance(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _infer_ndp_workflow_state_from_trajectory(
        {
            "step_0_tool_name": "ndp_filter_earthscope_station_catalog",
            "step_0_observation": {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "center": {"latitude": 34.05, "longitude": -118.25},
                "radius_km": 75,
                "within_radius_count": 1,
                "stations": [
                    {
                        "station": "MTA1",
                        "latitude": 34.05522077,
                        "longitude": -118.24550778,
                        "distance_km": 0.7,
                    }
                ],
                "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
            },
            "step_1_tool_name": "ndp_stage_resource",
            "step_1_tool_args": {
                "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "identifier_type": "id",
                "resource_name": "MTA1.CI.LY_.30.csv",
                "server": "global",
            },
            "step_1_observation": {
                "staged": True,
                "path": str(staged_csv),
            },
        }
    )

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"] == str(staged_csv)
    assert state["resource_candidate"]["dataset_id"] == "1b0c1b93-f164-4025-bd7b-000252b5ca18"
    assert state["resource_candidate"]["resource_name"] == "MTA1.CI.LY_.30.csv"
    assert state["resource_candidate"]["station_id"] == "MTA1"
    assert state["resource_candidate"]["geographically_grounded"] is True
    assert state["resource_candidate"]["station_distance_km"] == 0.7


def test_delegated_prompt_includes_accumulated_session_workflow_state(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    app = SimpleNamespace(
        state=SimpleNamespace(
            tool_call_ledger={
                "session-123": [
                    {
                        "name": "ndp_filter_earthscope_station_catalog",
                        "args": {
                            "filepath": "/workspace/.clio/artifacts/ndp-staging/earthscope.csv",
                            "latitude": 34.05,
                            "longitude": -118.25,
                            "radius_km": 75,
                        },
                        "result": {
                            "ok": True,
                            "path": "/workspace/.clio/artifacts/ndp-staging/earthscope.csv",
                            "center": {"latitude": 34.05, "longitude": -118.25},
                            "radius_km": 75,
                            "within_radius_count": 1,
                            "stations": [
                                {
                                    "station": "MTA1",
                                    "latitude": 34.05522077,
                                    "longitude": -118.24550778,
                                    "network": "SCGN",
                                    "status": "ACTIVE",
                                    "distance_km": 0.713,
                                }
                            ],
                            "_meta": {
                                "tool": "filter_earthscope_station_catalog",
                                "status": "success",
                            },
                        },
                    },
                    {
                        "name": "ndp_stage_resource",
                        "args": {
                            "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "server": "global",
                        },
                        "result": {
                            "staged": True,
                            "path": str(staged_csv),
                            "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                            "dataset_name": "mta1-ci-ly-30",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "source_url": (
                                "https://ds2.datacollaboratory.org/Earthscope_api_dec2024/"
                                "raw_csv/MTA1.CI.LY_.30.csv"
                            ),
                        },
                    },
                ]
            }
        )
    )

    prompt = _append_session_workflow_state_context(
        app,
        "session-123",
        "synthesize the result",
    )

    assert "Accumulated typed workflow state" in prompt
    assert "MTA1" in prompt
    assert "0.713" in prompt
    assert "https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/MTA1.CI.LY_.30.csv" in prompt
    assert '"analysis_ready": true' in prompt


def test_regional_station_csv_is_not_analysis_ready_when_station_metadata_mismatches(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "WWMT.CI.LY_.40.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "geospatial": {
                            "status": "resolved",
                            "region_name": "Bay Area, California",
                            "center_lat": 37.77,
                            "center_lon": -122.42,
                            "radius_km": 75,
                        },
                        "station_catalog": {
                            "status": "ranked_metadata_only",
                            "stations": [
                                {
                                    "station": "UCSF",
                                    "latitude": 37.7637,
                                    "longitude": -122.4587,
                                    "distance_km": 3.5,
                                }
                            ],
                        },
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "WWMT.CI.LY_.40.csv",
                            "resource_url": "https://ds2.example.test/raw_csv/WWMT.CI.LY_.40.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": "https://ds2.example.test/raw_csv/WWMT.CI.LY_.40.csv",
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is False
    assert "does not match" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["station_id"] == "WWMT"
    assert state["resource_candidate"]["geographically_grounded"] is False


def test_station_csv_provenance_guard_accepts_alternate_geospatial_region_shape(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "WWMT.CI.LY_.40.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")

    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "type": "geospatial",
                        "region": {
                            "type": "circle",
                            "center": {"lat": 37.77, "lon": -122.42},
                            "radius_km": 75,
                        },
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "WWMT.CI.LY_.40.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["analysis_ready"] is False
    assert "filtered station metadata" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["station_id"] == "WWMT"
    assert state["resource_candidate"]["geographically_grounded"] is False


def test_blueprint_continuation_contract_routes_from_prior_structured_prompt_state(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "changed_station.csv"
    staged_csv.parent.mkdir(parents=True)
    staged_csv.write_text("time,east,north,up\n0,0,0,0\n")
    prior_state = {
        "workflow_state": {
            "resource_candidate": {
                "status": "selected",
                "resource_name": "changed_station.csv",
            },
            "acquisition": {
                "status": "staged",
                "analysis_ready": True,
                "local_path": str(staged_csv),
            },
        }
    }

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="analysis",
            source="expert_pack",
            title="Analysis",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "start_with_gnss_profile",
                        "when_state": {
                            "acquisition.status": "staged",
                            "acquisition.analysis_ready": True,
                            "profile.status": {"exists": False},
                        },
                        "match": "all",
                        "next_expert": "gnss_timeseries_analysis",
                        "next_action": "profile the staged CSV",
                    }
                ]
            },
        ),
        source_text="Prior structured blueprint state:\n" + json.dumps(prior_state),
        answer_text="",
        completed_outputs=[],
        declared_child_ids={"gnss_timeseries_analysis"},
        completed_child_ids=set(),
    )

    assert [row["delegate_to"] for row in rows] == ["gnss_timeseries_analysis"]
    assert "changed_station.csv" in rows[0]["question"]


def test_workflow_state_extraction_preserves_nested_child_structured_evidence() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "structured": {
                        "evidence": json.dumps(
                            {
                                "workflow_state": {
                                    "profile": {"status": "complete", "rows_scanned": 5000}
                                }
                            }
                        )
                    }
                }
            )
        ]
    )

    assert state["profile"]["status"] == "complete"
    assert state["profile"]["rows_scanned"] == 5000


def test_workflow_state_downgrades_analysis_ready_without_staged_local_path() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "ready",
                            "analysis_ready": True,
                            "source_url": "https://example.test/raw_csv/WXYZ.csv",
                        },
                        "resource_candidate": {
                            "status": "metadata_confirmed",
                            "resource_urls": ["https://example.test/raw_csv/WXYZ.csv"],
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["status"] == "candidate_found"
    assert state["acquisition"]["analysis_ready"] is False
    assert "staged local CSV path" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["resource_urls"] == ["https://example.test/raw_csv/WXYZ.csv"]


def test_workflow_state_reclassifies_data_available_without_staged_local_path() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "data_available",
                            "analysis_ready": True,
                            "resource_urls": [
                                "https://example.test/raw_csv/EFGH.CI.LY_.30.csv"
                            ],
                        },
                        "resource_candidate": {
                            "status": "ready",
                            "dataset_id": "changed-dataset",
                            "resource_name": "EFGH.CI.LY_.30.csv",
                            "resource_url": "https://example.test/raw_csv/EFGH.CI.LY_.30.csv",
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["status"] == "candidate_found"
    assert state["acquisition"]["analysis_ready"] is False
    assert "staged local CSV path" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["status"] == "ready"


def test_workflow_state_reclassifies_metadata_only_with_candidate_url() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                            "source_url": "https://example.test/raw_csv/ABCD.CI.LY_.30.csv",
                        },
                        "resource_candidate": {
                            "status": "available",
                            "dataset_id": "changed-dataset",
                            "resource_name": "ABCD.CI.LY_.30.csv",
                            "resource_url": "https://example.test/raw_csv/ABCD.CI.LY_.30.csv",
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["status"] == "candidate_found"
    assert state["acquisition"]["analysis_ready"] is False
    assert "no local CSV was staged" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["status"] == "available"


def test_blueprint_continuation_contract_routes_candidate_url_before_resolver_completion() -> None:
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "acquisition": {
                        "status": "metadata_only",
                        "analysis_ready": False,
                        "source_url": "https://example.test/raw_csv/ABCD.CI.LY_.30.csv",
                    },
                    "resource_candidate": {
                        "status": "available",
                        "dataset_id": "changed-dataset",
                        "resource_name": "ABCD.CI.LY_.30.csv",
                        "resource_url": "https://example.test/raw_csv/ABCD.CI.LY_.30.csv",
                    },
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="earthscope_station_catalog",
            source="expert_pack",
            title="Station Catalog",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "candidate_url_to_resource_stage",
                        "when_state": {
                            "resource_candidate.status": {
                                "in": [
                                    "available",
                                    "candidate_found",
                                    "metadata_confirmed",
                                    "ready",
                                    "selected",
                                ]
                            },
                        },
                        "match": "all",
                        "next_expert": "ndp_resource_resolver",
                        "next_action": "stage the current concrete station CSV candidate",
                    }
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text="candidate URL found",
        completed_outputs=completed,
        declared_child_ids={"ndp_resource_resolver"},
        completed_child_ids=set(),
    )

    assert [row["delegate_to"] for row in rows] == ["ndp_resource_resolver"]
    assert "ABCD.CI.LY_.30.csv" in rows[0]["question"]


def test_blueprint_continuation_contract_routes_selected_resource_without_acquisition_state() -> None:
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "resource_candidate": {
                        "status": "selected",
                        "dataset_id": "changed-dataset",
                        "resource_name": "WWMT.CI.LY_.40.csv",
                        "resource_url": "https://example.test/raw_csv/WWMT.CI.LY_.40.csv",
                    },
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="earthscope_station_catalog",
            source="expert_pack",
            title="Station Catalog",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "candidate_url_to_resource_stage",
                        "when_state": {
                            "resource_candidate.status": {
                                "in": [
                                    "available",
                                    "candidate_found",
                                    "metadata_confirmed",
                                    "ready",
                                    "selected",
                                ]
                            },
                        },
                        "match": "all",
                        "next_expert": "ndp_resource_resolver",
                        "next_action": "stage the current concrete station CSV candidate",
                    }
                ]
            },
        ),
        source_text="Explore a different region.",
        answer_text="candidate URL found",
        completed_outputs=completed,
        declared_child_ids={"ndp_resource_resolver"},
        completed_child_ids=set(),
    )

    assert [row["delegate_to"] for row in rows] == ["ndp_resource_resolver"]
    assert "WWMT.CI.LY_.40.csv" in rows[0]["question"]


def test_blueprint_continuation_contract_uses_inferred_tool_state_from_answer() -> None:
    answer = "\n".join(
        [
            "The tool-backed expert produced no final prose answer.",
            "",
            "CLIO inferred typed tool state from tool observations:",
            json.dumps(
                {
                    "workflow_state": {
                        "catalog": {"status": "metadata_found"},
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                        },
                    }
                }
            ),
        ]
    )

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="ndp_dataset_discovery",
            source="expert_pack",
            title="Discovery",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "discovery_to_station_catalog",
                        "when_state": {
                            "catalog.status": "metadata_found",
                            "station_catalog.status": {"exists": False},
                        },
                        "match": "all",
                        "next_expert": "earthscope_station_catalog",
                        "next_action": "rank nearby GNSS stations",
                    }
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text=answer,
        completed_outputs=[],
        declared_child_ids={"earthscope_station_catalog"},
        completed_child_ids=set(),
    )

    assert [row["delegate_to"] for row in rows] == ["earthscope_station_catalog"]
    assert "metadata_found" in rows[0]["question"]


def test_strict_depth_bubble_prefers_resumed_child_subtree_over_parent_draft() -> None:
    rows = [
        {
            "agent_id": "geospatial",
            "parent_id": "main",
            "status": "completed",
            "stage": "delegate.completed",
            "output_summary": '{"workflow_state":{"geospatial":{"status":"resolved"}}}',
            "children": [
                {
                    "agent_id": "ndp_dataset_discovery",
                    "parent_id": "geospatial",
                    "status": "completed",
                    "stage": "delegate.completed",
                    "output_summary": (
                        '{"workflow_state":{"acquisition":{"status":"metadata_only",'
                        '"analysis_ready":false,"blocker":"station metadata only"}}}'
                    ),
                },
                {
                    "agent_id": "geospatial",
                    "parent_id": "main",
                    "status": "completed",
                    "stage": "parent.resumed",
                    "output_summary": (
                        "No station-specific GNSS time-series CSV has been staged yet."
                    ),
                },
            ],
        },
        {
            "agent_id": "main",
            "status": "completed",
            "stage": "parent.resumed",
            "output_summary": '{"workflow_state":{"geospatial":{"status":"resolved"}}}',
        },
    ]

    assert (
        _bubbled_child_evidence_output_summary(
            rows[0]["children"],
            "geospatial",
            {"ndp_dataset_discovery"},
        )
        == "No station-specific GNSS time-series CSV has been staged yet."
    )


def test_blueprint_continuation_contract_does_not_repeat_resolver_for_filtered_stations() -> None:
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "station_catalog": {
                        "status": "ranked_metadata_only",
                        "stations": [{"station": "WXYZ", "distance_km": 4.2}],
                    },
                    "resource_discovery": {
                        "status": "search_required",
                        "station_resource_queries": [
                            {
                                "station": "WXYZ",
                                "preferred_calls": [
                                    {
                                        "tool": "ndp_search_datasets",
                                        "arguments": {
                                            "resource_name": "WXYZ",
                                            "resource_format": "CSV",
                                            "server": "global",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    "acquisition": {
                        "status": "metadata_only",
                        "analysis_ready": False,
                    },
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="earthscope_station_catalog",
            source="expert_pack",
            title="Station Catalog",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "station_catalog_to_resource",
                        "when_state": {
                            "station_catalog.status": {
                                "in": ["ranked", "ranked_metadata_only"]
                            }
                        },
                        "match": "all",
                        "next_expert": "ndp_resource_resolver",
                        "next_action": "stage a selected station-specific CSV",
                    }
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text="filtered stations found",
        completed_outputs=completed,
        declared_child_ids={"ndp_resource_resolver"},
        completed_child_ids={"ndp_resource_resolver"},
    )

    assert rows == []


def test_blueprint_continuation_contract_requires_ranked_stations_before_metadata_resolver() -> None:
    contracts = [
        {
            "id": "metadata_acquisition_to_resource_search",
            "when_state": {
                "station_catalog.status": {"in": ["ranked", "ranked_metadata_only"]},
                "acquisition.status": "metadata_only",
                "acquisition.analysis_ready": False,
            },
            "match": "all",
            "next_expert": "ndp_resource_resolver",
            "next_action": "use filtered ranked station metadata to search station CSVs",
        }
    ]
    agent = AgentDef(
        id="earthscope_station_catalog",
        source="expert_pack",
        title="Station Catalog",
        parameters={"continuation_contracts": contracts},
    )
    metadata_only_without_station_frontier = [
        json.dumps(
            {
                "workflow_state": {
                    "acquisition": {
                        "status": "metadata_only",
                        "analysis_ready": False,
                    },
                    "resource_discovery": {"status": "search_required"},
                }
            }
        )
    ]

    assert (
        _continuation_contract_handoffs(
            agent,
            source_text="Explore a changed region.",
            answer_text="metadata staged",
            completed_outputs=metadata_only_without_station_frontier,
            declared_child_ids={"ndp_resource_resolver"},
            completed_child_ids=set(),
        )
        == []
    )

    ranked_station_frontier = [
        json.dumps(
            {
                "workflow_state": {
                    "station_catalog": {
                        "status": "ranked_metadata_only",
                        "stations": [{"station": "WXYZ", "distance_km": 4.2}],
                    },
                    "acquisition": {
                        "status": "metadata_only",
                        "analysis_ready": False,
                    },
                    "resource_discovery": {
                        "status": "search_required",
                        "station_resource_queries": [
                            {
                                "station": "WXYZ",
                                "preferred_calls": [
                                    {
                                        "tool": "ndp_search_datasets",
                                        "arguments": {
                                            "resource_name": "WXYZ",
                                            "resource_format": "CSV",
                                            "server": "global",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        agent,
        source_text="Explore a changed region.",
        answer_text="metadata staged and filtered",
        completed_outputs=ranked_station_frontier,
        declared_child_ids={"ndp_resource_resolver"},
        completed_child_ids=set(),
    )

    assert len(rows) == 1
    assert rows[0]["delegate_to"] == "ndp_resource_resolver"
    assert rows[0]["source"] == "blueprint_typed_state_continuation_contract"
    assert '"station_catalog"' in rows[0]["question"]
    assert '"station_resource_queries"' in rows[0]["question"]


def test_blueprint_continuation_contract_routes_staged_station_csv_to_resolver_before_completion(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "ABCD.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2024-01-01,0,0,0\n")
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "acquisition": {
                        "status": "staged",
                        "analysis_ready": True,
                        "local_path": str(staged_csv),
                    },
                    "resource_candidate": {
                        "status": "selected",
                        "dataset_id": "changed-dataset",
                        "resource_name": "ABCD.CI.LY_.30.csv",
                    },
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="earthscope_station_catalog",
            source="expert_pack",
            title="Station Catalog",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "staged_acquisition_to_resource_resolver",
                        "when_state": {
                            "acquisition.status": "staged",
                            "acquisition.analysis_ready": True,
                        },
                        "match": "all",
                        "next_expert": "ndp_resource_resolver",
                        "next_action": "validate the staged station CSV acquisition state",
                    }
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text="station CSV staged",
        completed_outputs=completed,
        declared_child_ids={"ndp_resource_resolver"},
        completed_child_ids=set(),
    )

    assert [row["delegate_to"] for row in rows] == ["ndp_resource_resolver"]
    assert str(staged_csv) in rows[0]["question"]


def test_blueprint_continuation_contract_does_not_repeat_staged_station_csv_resolver(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "ABCD.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2024-01-01,0,0,0\n")
    completed = [
        json.dumps(
            {
                "workflow_state": {
                    "acquisition": {
                        "status": "staged",
                        "analysis_ready": True,
                        "local_path": str(staged_csv),
                    },
                    "profile": {"status": "complete"},
                    "artifact": {"status": "ready", "path": str(tmp_path / "plot.png")},
                }
            }
        )
    ]

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="earthscope_station_catalog",
            source="expert_pack",
            title="Station Catalog",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "staged_acquisition_to_resource_resolver",
                        "when_state": {
                            "acquisition.status": "staged",
                            "acquisition.analysis_ready": True,
                        },
                        "match": "all",
                        "next_expert": "ndp_resource_resolver",
                        "next_action": "validate the staged station CSV acquisition state",
                    }
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text="station CSV staged",
        completed_outputs=completed,
        declared_child_ids={"ndp_resource_resolver"},
        completed_child_ids={"ndp_resource_resolver"},
    )

    assert rows == []


def test_non_applicable_station_filter_preserves_analysis_ready_acquisition(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2024-01-01,0,0,0\n")

    state = _infer_ndp_station_catalog_state_from_tool_evidence(
        json.dumps(
            {
                "ok": True,
                "catalog_applicable": False,
                "resource_kind": "station_timeseries_csv",
                "analysis_ready": True,
                "path": str(staged_csv),
                "stations": [],
                "_meta": {
                    "tool": "filter_earthscope_station_catalog",
                    "status": "not_applicable",
                },
            }
        )
    )

    assert "station_catalog" not in state
    assert state["resource_candidate"]["resource_kind"] == "station_timeseries_csv"
    assert state["resource_candidate"]["catalog_applicable"] is False
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"] == str(staged_csv)


def test_handoff_rows_preserve_nested_durable_workflow_state(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2024-01-01,0,0,0\n")

    rows = [
        {
            "agent_id": "earthscope_station_catalog",
            "stage": "delegate.completed",
            "status": "completed",
            "workflow_state": {
                "station_catalog": {
                    "status": "ranked_metadata_only",
                    "stations": [{"station": "MTA1", "distance_km": 0.7}],
                },
            },
            "children": [
                {
                    "agent_id": "ndp_resource_resolver",
                    "stage": "delegate.completed",
                    "status": "completed",
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "dataset_id": "changed-dataset",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                        },
                    },
                }
            ],
        }
    ]

    state = _workflow_state_from_handoff_rows(rows)

    assert state["station_catalog"]["status"] == "ranked_metadata_only"
    assert state["resource_candidate"]["resource_name"] == "MTA1.CI.LY_.30.csv"
    assert state["resource_candidate"]["geographically_grounded"] is True
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True


def test_nested_handoff_state_drives_analysis_continuation_contract(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2024-01-01,0,0,0\n")
    parent_output = _append_nested_workflow_state(
        "Resolver completed station CSV acquisition.",
        [
            {
                "agent_id": "ndp_resource_resolver",
                "stage": "delegate.completed",
                "status": "completed",
                "workflow_state": {
                    "station_catalog": {
                        "status": "ranked_metadata_only",
                        "stations": [{"station": "MTA1", "distance_km": 0.7}],
                    },
                    "resource_candidate": {
                        "status": "selected",
                        "dataset_id": "changed-dataset",
                        "resource_name": "MTA1.CI.LY_.30.csv",
                    },
                    "acquisition": {
                        "status": "staged",
                        "analysis_ready": True,
                        "local_path": str(staged_csv),
                    },
                },
            }
        ],
    )

    rows = _continuation_contract_handoffs(
        AgentDef(
            id="ndp_dataset_discovery",
            source="expert_pack",
            title="Discovery",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "acquisition_to_analysis",
                        "when_state": {
                            "acquisition.status": "staged",
                            "acquisition.analysis_ready": True,
                            "resource_candidate.geographically_grounded": True,
                        },
                        "match": "all",
                        "next_expert": "gnss_timeseries_analysis",
                        "next_action": "profile the staged station CSV",
                    }
                ]
            },
        ),
        source_text="Explore a changed region.",
        answer_text="",
        completed_outputs=[parent_output],
        declared_child_ids={"gnss_timeseries_analysis"},
        completed_child_ids=set(),
    )

    assert [row["delegate_to"] for row in rows] == ["gnss_timeseries_analysis"]
    assert "Prior structured blueprint state" in rows[0]["question"]
    assert str(staged_csv) in rows[0]["question"]


def test_tool_workspace_context_defaults_ndp_staging_under_workspace(tmp_path: Path) -> None:
    with tool_workspace_context(tmp_path):
        args = _workspace_default_tool_arguments(
            "ndp_stage_resource",
            {"dataset_identifier": "dataset-1"},
        )

    assert args["output_dir"] == str(tmp_path / ".clio" / "artifacts" / "ndp-staging")


def test_tool_workspace_context_rewrites_disposable_tmp_ndp_staging(tmp_path: Path) -> None:
    with tool_workspace_context(tmp_path):
        args = _workspace_default_tool_arguments(
            "ndp_stage_resource",
            {"dataset_identifier": "dataset-1", "output_dir": "/tmp"},
        )

    assert args["output_dir"] == str(tmp_path / ".clio" / "artifacts" / "ndp-staging")


def test_tool_session_context_uses_active_session_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    wid = client.post(
        "/v1/workspaces",
        json={"name": "science", "root_path": str(workspace), "storage_root": str(workspace / ".clio")},
    ).json()["id"]
    sid = client.post("/v1/sessions", json={"title": "science", "workspace_id": wid}).json()["id"]

    with _gact_app_context(client.app), _tool_session_context(sid):
        args = _workspace_default_tool_arguments(
            "ndp_stage_resource",
            {"dataset_identifier": "dataset-1"},
        )

    assert args["output_dir"] == str(workspace / ".clio" / "artifacts" / "ndp-staging")


def test_generated_child_expert_tool_binds_active_session_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    wid = client.post(
        "/v1/workspaces",
        json={"name": "science", "root_path": str(workspace), "storage_root": str(workspace / ".clio")},
    ).json()["id"]
    sid = client.post("/v1/sessions", json={"title": "science", "workspace_id": wid}).json()["id"]
    parent = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        parameters={"enforce_child_contract_order": False},
    )
    child = AgentDef(
        id="ndp_resource_resolver",
        source="expert_pack",
        title="Resolver",
        parent_id="main",
    )

    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_active_agent_blueprint_rows",
        lambda app, session_id="": [parent, child],
    )
    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "runner")

    def fake_run_dynamic_agent_compat(
        runner: Any,
        base_agent: Any,
        agent_def: AgentDef,
        question: str,
        session_id: str,
        cancel_requested: Any,
    ) -> Any:
        del runner, base_agent, agent_def, question, session_id, cancel_requested
        args = _workspace_default_tool_arguments(
            "ndp_stage_resource",
            {"dataset_identifier": "dataset-1"},
        )
        return SimpleNamespace(answer=json.dumps({"workflow_state": {"workspace_args": args}}))

    monkeypatch.setattr(
        "clio_agent.gact.app._run_dynamic_agent_compat",
        fake_run_dynamic_agent_compat,
    )

    session_token = _ACTIVE_GACT_SESSION_ID.set(sid)
    completions_token = _ACTIVE_CHILD_TOOL_COMPLETIONS.set([])
    try:
        with _gact_app_context(client.app):
            child_tool = _build_child_expert_tool(SimpleNamespace(), parent, child)
            payload = json.loads(child_tool(question="stage a resource"))
    finally:
        _ACTIVE_CHILD_TOOL_COMPLETIONS.reset(completions_token)
        _ACTIVE_GACT_SESSION_ID.reset(session_token)

    state = _workflow_state_from_outputs([payload["output_summary"]])
    assert state["workspace_args"]["output_dir"] == str(
        workspace / ".clio" / "artifacts" / "ndp-staging"
    )


def test_skipped_delegated_handoff_does_not_execute_even_with_delegate_target() -> None:
    assert not _should_execute_delegated_handoff(
        {
            "delegate_to": "analysis",
            "status": "skipped",
            "skip_reason": "completed_sync_child_already_returned",
        }
    )


def test_ndp_metadata_resource_infers_metadata_only_workflow_state() -> None:
    state = _infer_ndp_workflow_state_from_tool_evidence(
        json.dumps(
            {
                "staged": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "dataset_id": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
                "dataset_name": "earthscope_stations",
                "resource_name": "earthscope_converted_data.csv",
                "source_url": "https://example.test/earthscope_converted_data.csv",
            }
        )
    )

    assert state["resource_candidate"]["status"] == "metadata_only"
    assert state["acquisition"]["status"] == "metadata_only"
    assert state["acquisition"]["analysis_ready"] is False
    assert state["acquisition"]["metadata_path"].endswith("earthscope_converted_data.csv")


def test_ndp_zero_result_constrained_search_is_not_conclusive_absence() -> None:
    state = _infer_ndp_search_state_from_tool_evidence(
        json.dumps(
            {
                "_meta": {"tool": "search_datasets"},
                "count": 0,
                "total_found": 0,
                "datasets": [],
                "search_coverage": {
                    "status": "covered",
                    "domain": "earthscope_gnss",
                    "search_terms": [
                        "EarthScope",
                        "GNSS",
                        "GPS",
                        "Southern California",
                        "Los Angeles",
                        "CSV",
                        "raw_csv",
                    ],
                    "resource_format": "csv",
                    "next_action": "",
                },
            }
        )
    )

    assert state["catalog"]["status"] == "search_incomplete"
    assert state["catalog"]["candidate_count"] == 0
    assert state["resource_discovery"]["status"] == "search_required"
    assert state["resource_discovery"]["search_terms"] == [
        "EarthScope",
        "GNSS",
        "GPS",
        "CSV",
        "raw_csv",
    ]


def test_ndp_station_csv_resource_infers_analysis_ready_workflow_state() -> None:
    state = _infer_ndp_workflow_state_from_tool_evidence(
        json.dumps(
            {
                "staged": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/WWMT.CI.LY_.40.csv",
                "dataset_id": "2611eeb1-4efd-4ab3-badd-abacc6b64d9f",
                "dataset_name": "wwmt-ci-ly-40",
                "resource_name": "WWMT.CI.LY_.40.csv",
                "source_url": "https://example.test/WWMT.CI.LY_.40.csv",
            }
        )
    )

    assert state["resource_candidate"]["status"] == "selected"
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"].endswith("WWMT.CI.LY_.40.csv")


def test_ndp_staged_csv_prose_infers_analysis_ready_workflow_state() -> None:
    state = _infer_ndp_workflow_state_from_tool_evidence(
        "| Selected CSV (staged) |\n"
        "| `/workspace/.clio/artifacts/ndp-staging/WWMT.CI.LY_.40.csv` |\n"
        "**Source URL**: `https://ds2.example.test/raw_csv/WWMT.CI.LY_.40.csv`\n"
        "**Size**: `50,084,343` bytes"
    )

    assert state["resource_candidate"]["status"] == "selected"
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"].endswith("WWMT.CI.LY_.40.csv")


def test_ndp_staged_csv_with_no_blocker_does_not_infer_failure() -> None:
    state = _infer_ndp_workflow_state_from_tool_evidence(
        "**Staged CSV Path:** `/workspace/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv`\n"
        "**Source URL:** `https://ds2.example.test/raw_csv/P475.CI.LY_.20.csv`\n"
        "**File Size:** *unknown*\n"
        "**Staging Blocker:** *none* - the resource is staged and ready for analysis."
    )

    assert state["resource_candidate"]["status"] == "selected"
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"].endswith("P475.CI.LY_.20.csv")


def test_ndp_plot_result_infers_ready_artifact_workflow_state() -> None:
    state = _infer_ndp_plot_state_from_tool_evidence(
        json.dumps(
            {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                "output_path": "/workspace/.clio/artifacts/MTA1_time_series.png",
                "output_size_bytes": 89696,
                "x_column": "time",
                "y_columns": ["east", "north", "up"],
                "rows_plotted": 2000,
                "_meta": {"tool": "plot_csv_timeseries", "status": "success"},
            }
        )
    )

    assert state["artifact"]["status"] == "ready"
    assert state["artifact"]["path"].endswith("MTA1_time_series.png")
    assert state["artifact"]["columns"] == ["east", "north", "up"]
    assert state["visualization"]["status"] == "complete"


def test_visualization_artifact_state_routes_to_synthesis() -> None:
    rows = _continuation_contract_handoffs(
        AgentDef(
            id="visualization",
            source="expert_pack",
            title="Visualization",
            parameters={
                "continuation_contracts": [
                    {
                        "id": "artifact_to_synthesis",
                        "when_state": {"artifact.status": "ready"},
                        "match": "all",
                        "next_expert": "synthesis",
                        "next_action": "synthesize artifact evidence",
                    }
                ]
            },
        ),
        source_text="Plot and summarize.",
        answer_text=(
            "CLIO inferred typed tool state from tool observations:\n"
            + json.dumps(
                {
                    "workflow_state": {
                        "artifact": {
                            "status": "ready",
                            "path": "/workspace/.clio/artifacts/MTA1_time_series.png",
                        }
                    }
                }
            )
        ),
        completed_outputs=[],
        declared_child_ids={"synthesis"},
        completed_child_ids=set(),
    )

    assert [row["delegate_to"] for row in rows] == ["synthesis"]
    assert "MTA1_time_series.png" in rows[0]["question"]


def test_ndp_trajectory_inference_prefers_station_csv_over_metadata() -> None:
    state = _infer_ndp_workflow_state_from_trajectory(
        {
            "observation_0": {
                "staged": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "dataset_id": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
                "dataset_name": "earthscope_stations",
                "resource_name": "earthscope_converted_data.csv",
                "source_url": "https://example.test/earthscope_converted_data.csv",
            },
            "observation_1": {
                "staged": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv",
                "dataset_id": "07e33e0c-4ac3-4a7b-a5b0-5a4610078236",
                "dataset_name": "p475-ci-ly-20",
                "resource_name": "P475.CI.LY_.20.csv",
                "source_url": "https://example.test/P475.CI.LY_.20.csv",
            },
        }
    )

    assert state["resource_candidate"]["status"] == "selected"
    assert state["resource_candidate"]["resource_name"] == "P475.CI.LY_.20.csv"
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"].endswith("P475.CI.LY_.20.csv")


def test_ndp_trajectory_state_appends_to_child_answer_for_contracts() -> None:
    answer = "Nearest EarthScope station for the requested region is P475."

    output = _append_inferred_workflow_state_from_trajectory(
        answer,
        {
            "trajectory": {
                "observation": {
                    "staged": True,
                    "path": "/workspace/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv",
                    "dataset_id": "07e33e0c-4ac3-4a7b-a5b0-5a4610078236",
                    "dataset_name": "p475-ci-ly-20",
                    "resource_name": "P475.CI.LY_.20.csv",
                    "source_url": "https://example.test/P475.CI.LY_.20.csv",
                }
            }
        },
    )

    state = _workflow_state_from_outputs([output])

    assert "CLIO inferred typed tool state from tool observations" in output
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"].endswith("P475.CI.LY_.20.csv")


def test_ndp_station_catalog_tool_infers_resource_search_required_state() -> None:
    state = _infer_ndp_station_catalog_state_from_tool_evidence(
        json.dumps(
            {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "center": {"latitude": 34.05, "longitude": -118.25},
                "radius_km": 75,
                "within_radius_count": 2,
                "stations": [
                    {
                        "station": "ABCD",
                        "latitude": 34.1,
                        "longitude": -118.3,
                        "network": "CI",
                        "status": "active",
                        "distance_km": 8.4,
                        "suggested_search_terms": [
                            "ABCD",
                            "ABCD EarthScope GNSS CSV",
                        ],
                        "resource_discovery": {
                            "status": "search_required",
                            "search_terms": [
                                "ABCD",
                                "ABCD.CI.LY",
                                "ABCD raw_csv",
                            ],
                        },
                    }
                ],
                "resource_discovery": {
                    "status": "search_required",
                    "station_resource_queries": [
                        {
                            "station": "ABCD",
                            "preferred_calls": [
                                {
                                    "tool": "ndp_search_datasets",
                                    "arguments": {
                                        "resource_name": "ABCD",
                                        "resource_format": "CSV",
                                        "server": "global",
                                        "limit": 20,
                                    },
                                }
                            ],
                        }
                    ],
                },
                "analysis_ready": False,
                "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
            }
        )
    )

    assert state["station_catalog"]["status"] == "ranked_metadata_only"
    assert state["station_catalog"]["candidate_count"] == 2
    assert state["station_catalog"]["analysis_ready_resource_count"] == 0
    assert state["resource_discovery"]["status"] == "search_required"
    assert "ABCD raw_csv" in state["resource_discovery"]["search_terms"]
    assert state["resource_discovery"]["station_resource_queries"][0]["station"] == "ABCD"


def test_ndp_search_tool_infers_incomplete_search_coverage_state() -> None:
    state = _infer_ndp_search_state_from_tool_evidence(
        json.dumps(
            {
                "datasets": [],
                "count": 0,
                "total_found": 0,
                "search_coverage": {
                    "domain": "earthscope_gnss",
                    "status": "incomplete",
                    "search_terms": ["GNSS", "station", "time-series"],
                    "next_action": "Search broad EarthScope station catalog terms.",
                },
                "_meta": {"tool": "search_datasets", "status": "success"},
            }
        )
    )

    assert state["catalog"]["status"] == "search_incomplete"
    assert state["resource_discovery"]["status"] == "search_required"
    assert "EarthScope" in state["resource_discovery"]["search_terms"]
    assert "GPS" in state["resource_discovery"]["search_terms"]


def test_ndp_search_tool_infers_earthscope_metadata_found_state() -> None:
    state = _infer_ndp_search_state_from_tool_evidence(
        json.dumps(
            {
                "datasets": [
                    {
                        "id": "dataset-1",
                        "name": "earthscope-stations",
                        "resource_summaries": [
                            {
                                "name": "earthscope_converted_data.csv",
                                "format": "CSV",
                                "url": "https://example.test/earthscope_converted_data.csv",
                            }
                        ],
                    }
                ],
                "count": 1,
                "total_found": 1,
                "search_coverage": {
                    "domain": "earthscope_gnss",
                    "status": "covered",
                    "search_terms": ["EarthScope", "GNSS", "station", "catalog", "CSV"],
                },
                "_meta": {"tool": "search_datasets", "status": "success"},
            }
        )
    )

    assert state["catalog"]["status"] == "metadata_found"
    assert state["resource_candidate"]["status"] == "metadata_only"
    assert state["acquisition"]["analysis_ready"] is False


def test_ndp_search_tool_prefers_station_csv_over_metadata_in_same_result() -> None:
    state = _infer_ndp_search_state_from_tool_evidence(
        json.dumps(
            {
                "datasets": [
                    {
                        "id": "metadata-dataset",
                        "name": "earthscope-stations",
                        "resource_summaries": [
                            {
                                "name": "earthscope_converted_data.csv",
                                "format": "CSV",
                                "url": "https://example.test/earthscope_converted_data.csv",
                            }
                        ],
                    },
                    {
                        "id": "station-dataset",
                        "name": "abcd-ci-ly-30",
                        "resource_summaries": [
                            {
                                "name": "ABCD.CI.LY_.30.csv",
                                "format": "CSV",
                                "url": "https://example.test/raw_csv/ABCD.CI.LY_.30.csv",
                            }
                        ],
                    },
                ],
                "count": 2,
                "total_found": 2,
                "search_coverage": {
                    "domain": "earthscope_gnss",
                    "status": "covered",
                    "resource_name": "ABCD",
                    "resource_format": "CSV",
                    "station_resource_search": True,
                },
                "_meta": {"tool": "search_datasets", "status": "success"},
            }
        )
    )

    assert state["catalog"]["status"] == "candidates_found"
    assert state["resource_candidate"]["status"] == "selected"
    assert state["resource_candidate"]["dataset_id"] == "station-dataset"
    assert state["resource_candidate"]["resource_name"] == "ABCD.CI.LY_.30.csv"


def test_ndp_station_catalog_trajectory_state_appends_search_required_for_parent_contracts() -> None:
    output = _append_inferred_workflow_state_from_trajectory(
        "Ranked nearby stations from metadata.",
        {
            "observation_0": {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "center": {"latitude": 41.88, "longitude": -87.63},
                "radius_km": 100,
                "within_radius_count": 1,
                "stations": [
                    {
                        "station": "WXYZ",
                        "latitude": 41.9,
                        "longitude": -87.7,
                        "distance_km": 6.1,
                        "resource_discovery": {
                            "status": "search_required",
                            "search_terms": ["WXYZ", "WXYZ EarthScope GNSS CSV"],
                        },
                    }
                ],
                "resource_discovery": {
                    "status": "search_required",
                    "station_resource_queries": [
                        {
                            "station": "WXYZ",
                            "preferred_calls": [
                                {
                                    "tool": "ndp_search_datasets",
                                    "arguments": {
                                        "resource_name": "WXYZ",
                                        "resource_format": "CSV",
                                        "server": "global",
                                        "limit": 20,
                                    },
                                }
                            ],
                        }
                    ],
                },
                "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
            }
        },
    )

    state = _workflow_state_from_outputs([output])

    assert "CLIO inferred typed tool state from tool observations" in output
    assert state["station_catalog"]["status"] == "ranked_metadata_only"
    assert state["resource_discovery"]["status"] == "search_required"
    assert "WXYZ EarthScope GNSS CSV" in state["resource_discovery"]["search_terms"]
    assert "station_resource_queries" in state["resource_discovery"]


def test_ndp_station_resource_search_trajectory_stays_required_until_ranked_stations_are_covered() -> None:
    output = _append_inferred_workflow_state_from_trajectory(
        "Ranked nearby stations and searched the first station.",
        {
            "steps": [
                {
                    "tool_name": "ndp_filter_earthscope_station_catalog",
                    "observation": {
                        "ok": True,
                        "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                        "center": {"latitude": 37.77, "longitude": -122.42},
                        "radius_km": 75,
                        "within_radius_count": 3,
                        "stations": [
                            {"station": "UCSF", "distance_km": 1.2},
                            {"station": "SBRB", "distance_km": 10.2},
                            {"station": "MHDL", "distance_km": 22.4},
                        ],
                        "resource_discovery": {"status": "search_required"},
                        "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
                    },
                },
                {
                    "tool_name": "ndp_search_datasets",
                    "tool_args": {"resource_name": "UCSF", "resource_format": "CSV", "server": "global"},
                    "observation": {
                        "datasets": [],
                        "count": 0,
                        "total_found": 0,
                        "search_coverage": {
                            "domain": "earthscope_gnss",
                            "status": "covered",
                            "resource_name": "UCSF",
                            "resource_format": "CSV",
                            "station_resource_search": True,
                        },
                        "_meta": {"tool": "search_datasets", "status": "success"},
                    },
                },
            ]
        },
    )

    state = _workflow_state_from_outputs([output])

    assert state["resource_discovery"]["status"] == "search_required"
    assert state["resource_discovery"]["searched_station_ids"] == ["UCSF"]
    assert state["resource_discovery"]["remaining_station_ids"] == ["SBRB", "MHDL"]
    assert state["acquisition"]["analysis_ready"] is False


def test_ndp_station_resource_search_trajectory_exhausts_after_ranked_station_coverage() -> None:
    output = _append_inferred_workflow_state_from_trajectory(
        "Ranked nearby stations and searched all ranked station resources.",
        {
            "steps": [
                {
                    "tool_name": "ndp_filter_earthscope_station_catalog",
                    "observation": {
                        "ok": True,
                        "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                        "center": {"latitude": 37.77, "longitude": -122.42},
                        "radius_km": 75,
                        "within_radius_count": 3,
                        "stations": [
                            {"station": "UCSF", "distance_km": 1.2},
                            {"station": "SBRB", "distance_km": 10.2},
                            {"station": "MHDL", "distance_km": 22.4},
                        ],
                        "resource_discovery": {"status": "search_required"},
                        "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
                    },
                },
                *[
                    {
                        "tool_name": "ndp_search_datasets",
                        "tool_args": {"resource_name": station, "resource_format": "CSV", "server": "global"},
                        "observation": {
                            "datasets": [],
                            "count": 0,
                            "total_found": 0,
                            "search_coverage": {
                                "domain": "earthscope_gnss",
                                "status": "covered",
                                "resource_name": station,
                                "resource_format": "CSV",
                                "station_resource_search": True,
                            },
                            "_meta": {"tool": "search_datasets", "status": "success"},
                        },
                    }
                    for station in ("UCSF", "SBRB", "MHDL")
                ],
            ]
        },
    )

    state = _workflow_state_from_outputs([output])

    assert state["resource_discovery"]["status"] == "search_exhausted"
    assert state["resource_discovery"]["searched_station_ids"] == ["UCSF", "SBRB", "MHDL"]
    assert state["resource_discovery"]["search_attempt_count"] == 3
    assert state["acquisition"]["status"] == "metadata_only"
    assert state["acquisition"]["analysis_ready"] is False


def test_empty_tool_agent_fallback_summarizes_metadata_only_search_exhaustion() -> None:
    answer = _tool_agent_empty_answer_fallback(
        {
            "steps": [
                {
                    "tool_name": "ndp_filter_earthscope_station_catalog",
                    "observation": {
                        "ok": True,
                        "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                        "center": {"latitude": 37.77, "longitude": -122.42},
                        "radius_km": 75,
                        "within_radius_count": 3,
                        "stations": [
                            {"station": "UCSF", "distance_km": 1.2},
                            {"station": "SBRB", "distance_km": 10.2},
                            {"station": "MHDL", "distance_km": 22.4},
                        ],
                        "resource_discovery": {"status": "search_required"},
                        "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
                    },
                },
                *[
                    {
                        "tool_name": "ndp_search_datasets",
                        "tool_args": {
                            "resource_name": station,
                            "resource_format": "CSV",
                            "server": "global",
                        },
                        "observation": {
                            "datasets": [],
                            "count": 0,
                            "total_found": 0,
                            "search_coverage": {
                                "domain": "earthscope_gnss",
                                "status": "covered",
                                "resource_name": station,
                                "resource_format": "CSV",
                                "station_resource_search": True,
                            },
                            "_meta": {"tool": "search_datasets", "status": "success"},
                        },
                    }
                    for station in ("UCSF", "SBRB", "MHDL")
                ],
            ]
        },
    )

    assert "Staged EarthScope station metadata" in answer
    assert "Metadata CSV: `/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`" in answer
    assert "37.77 N, 122.42 W" in answer
    assert "Station-specific NDP searches were attempted for: UCSF, SBRB, MHDL." in answer
    assert "Resource discovery status: search exhausted" in answer
    assert "Retained tool observations" not in answer


def test_station_resource_search_state_exhausts_from_durable_tool_rows() -> None:
    selected = {
        "station_catalog": {
            "status": "ranked_metadata_only",
            "stations": [
                {"station": "UCSF", "distance_km": 3.444},
                {"station": "SBRB", "distance_km": 9.325},
                {"station": "SBRU", "distance_km": 9.325},
                {"station": "MHDL", "distance_km": 10.36},
                {"station": "EBMD", "distance_km": 12.971},
            ],
        },
        "resource_discovery": {"status": "search_required"},
        "acquisition": {"status": "metadata_only", "analysis_ready": False},
    }
    rows = [
        {
            "name": "ndp_search_datasets",
            "args": {"resource_name": station, "resource_format": "CSV", "server": "global"},
            "result": {
                "datasets": [],
                "count": 0,
                "total_found": 0,
                "search_coverage": {
                    "domain": "earthscope_gnss",
                    "status": "covered",
                    "resource_name": station,
                    "resource_format": "CSV",
                    "station_resource_search": True,
                },
                "_meta": {"tool": "search_datasets", "status": "success"},
            },
        }
        for station in ("UCSF", "SBRB", "SBRU", "MHDL", "EBMD")
    ]

    state = _station_resource_search_state_from_rows(rows, selected)

    assert state["resource_discovery"]["status"] == "search_exhausted"
    assert state["resource_discovery"]["searched_station_ids"] == [
        "UCSF",
        "SBRB",
        "SBRU",
        "MHDL",
        "EBMD",
    ]
    assert state["acquisition"]["status"] == "metadata_only"
    assert state["acquisition"]["analysis_ready"] is False


def test_station_resource_search_state_dedupes_repeated_station_attempts() -> None:
    selected = {
        "station_catalog": {
            "status": "ranked_metadata_only",
            "stations": [
                {"station": "UCSF", "distance_km": 3.444},
                {"station": "SBRB", "distance_km": 9.325},
                {"station": "SBRU", "distance_km": 9.325},
            ],
        },
        "resource_discovery": {"status": "search_required"},
        "acquisition": {"status": "metadata_only", "analysis_ready": False},
    }
    rows = [
        {
            "name": "ndp_search_datasets",
            "args": {"resource_name": station, "resource_format": "CSV", "server": "global"},
            "result": {
                "datasets": [],
                "count": 0,
                "total_found": 0,
                "search_coverage": {
                    "domain": "earthscope_gnss",
                    "status": "covered",
                    "resource_name": station,
                    "resource_format": "CSV",
                    "station_resource_search": True,
                },
                "_meta": {"tool": "search_datasets", "status": "success"},
            },
        }
        for station in ("UCSF", "SBRB", "UCSF", "SBRU", "SBRB")
    ]

    state = _station_resource_search_state_from_rows(rows, selected)

    assert state["resource_discovery"]["status"] == "search_exhausted"
    assert state["resource_discovery"]["searched_station_ids"] == ["UCSF", "SBRB", "SBRU"]
    assert state["resource_discovery"]["search_attempt_count"] == 3
    assert state["resource_discovery"]["duplicate_search_attempt_count"] == 2
    assert state["resource_discovery"]["trace_quality"] == "repeated_station_resource_search"


def test_prior_staged_ndp_resource_result_skips_same_station_csv() -> None:
    rows = [
        {
            "name": "ndp_stage_resource",
            "args": {
                "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "resource_name": "MTA1.CI.LY_.30.csv",
            },
            "ok": True,
            "result": {
                "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                "source_url": "https://example.test/raw_csv/MTA1.CI.LY_.30.csv",
                "selected_resource_url": "https://example.test/raw_csv/MTA1.CI.LY_.30.csv",
                "size_bytes": 50424246,
                "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "dataset_name": "mta1-ci-ly-30",
                "resource_name": "MTA1.CI.LY_.30.csv",
                "_meta": {"tool": "stage_resource", "status": "success"},
            },
        }
    ]

    result = _prior_staged_ndp_resource_result(
        {
            "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
            "resource_name": "MTA1.CI.LY_.30.csv",
            "resource_index": 0,
        },
        rows,
    )

    assert result is not None
    assert result["path"] == "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv"
    assert result["size_bytes"] == 50424246
    assert result["_meta"] == {
        "tool": "stage_resource",
        "status": "skipped",
        "reason": "duplicate_station_resource_stage",
        "cache_hit": True,
    }
    assert result["clio_runtime"]["workflow_state"]["acquisition"]["analysis_ready"] is True


def test_prior_staged_ndp_resource_result_does_not_skip_different_station_csv() -> None:
    rows = [
        {
            "name": "ndp_stage_resource",
            "args": {
                "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "resource_name": "MTA1.CI.LY_.30.csv",
            },
            "ok": True,
            "result": {
                "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                "source_url": "https://example.test/raw_csv/MTA1.CI.LY_.30.csv",
                "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "resource_name": "MTA1.CI.LY_.30.csv",
                "_meta": {"tool": "stage_resource", "status": "success"},
            },
        }
    ]

    result = _prior_staged_ndp_resource_result(
        {
            "dataset_identifier": "other-dataset",
            "resource_name": "PKRD.CI.LY_.30.csv",
            "resource_index": 0,
        },
        rows,
    )

    assert result is None


def test_prior_staged_ndp_resource_result_does_not_skip_metadata_only_catalog() -> None:
    rows = [
        {
            "name": "ndp_stage_resource",
            "args": {
                "dataset_identifier": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
                "resource_name": "earthscope_converted_data.csv",
            },
            "ok": True,
            "result": {
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "source_url": "https://example.test/earthscope_converted_data.csv",
                "dataset_id": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
                "resource_name": "earthscope_converted_data.csv",
                "_meta": {"tool": "stage_resource", "status": "success"},
            },
        }
    ]

    result = _prior_staged_ndp_resource_result(
        {
            "dataset_identifier": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
            "resource_name": "earthscope_converted_data.csv",
            "resource_index": 0,
        },
        rows,
    )

    assert result is None


def test_recording_blueprint_tool_skips_duplicate_station_csv_stage() -> None:
    def fail_stage(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"duplicate station staging should be short-circuited: {kwargs}")

    tool = dspy.Tool(
        func=fail_stage,
        name="ndp_stage_resource",
        desc="Stage NDP resource",
        args={"dataset_identifier": {"type": "string"}},
    )
    rows: list[dict[str, Any]] = [
        {
            "name": "ndp_stage_resource",
            "args": {
                "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "resource_name": "MTA1.CI.LY_.30.csv",
            },
            "ok": True,
            "result": {
                "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                "source_url": "https://example.test/raw_csv/MTA1.CI.LY_.30.csv",
                "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "resource_name": "MTA1.CI.LY_.30.csv",
                "_meta": {"tool": "stage_resource", "status": "success"},
            },
        }
    ]
    token = _ACTIVE_BLUEPRINT_TOOL_ROWS.set(rows)
    try:
        wrapped = _recording_blueprint_tool(tool)
        result = wrapped(
            dataset_identifier="1b0c1b93-f164-4025-bd7b-000252b5ca18",
            resource_name="MTA1.CI.LY_.30.csv",
            resource_index=0,
        )
    finally:
        _ACTIVE_BLUEPRINT_TOOL_ROWS.reset(token)

    assert result["_meta"]["reason"] == "duplicate_station_resource_stage"
    assert rows[-1]["skipped"] is True
    assert rows[-1]["name"] == "ndp_stage_resource"


def test_ndp_search_runtime_feedback_lists_remaining_ranked_stations() -> None:
    rows = [
        {
            "name": "ndp_filter_earthscope_station_catalog",
            "args": {
                "filepath": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "latitude": 37.77,
                "longitude": -122.42,
                "radius_km": 75,
            },
            "result": {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "center": {"latitude": 37.77, "longitude": -122.42},
                "radius_km": 75,
                "within_radius_count": 3,
                "stations": [
                    {"station": "UCSF", "distance_km": 3.444},
                    {"station": "SBRB", "distance_km": 9.325},
                    {"station": "SBRU", "distance_km": 9.325},
                ],
                "resource_discovery": {"status": "search_required"},
                "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
            },
        },
        {
            "name": "ndp_search_datasets",
            "args": {"resource_name": "UCSF", "resource_format": "CSV", "server": "global"},
            "result": {
                "datasets": [],
                "count": 0,
                "total_found": 0,
                "search_coverage": {
                    "domain": "earthscope_gnss",
                    "status": "covered",
                    "resource_name": "UCSF",
                    "resource_format": "CSV",
                    "station_code": "UCSF",
                    "station_resource_search": True,
                },
                "_meta": {"tool": "search_datasets", "status": "success"},
            },
        },
    ]

    state = _infer_ndp_workflow_state_from_tool_rows(rows)
    augmented = _augment_ndp_search_result_with_runtime_state(rows[-1]["result"], rows)

    assert state["resource_discovery"]["status"] == "search_required"
    assert augmented["clio_runtime"]["terminal"] is False
    assert augmented["clio_runtime"]["workflow_state"]["resource_discovery"][
        "remaining_station_ids"
    ] == ["SBRB", "SBRU"]
    assert "SBRB, SBRU" in augmented["clio_runtime"]["next_action"]


def test_ndp_search_runtime_feedback_marks_station_search_exhausted() -> None:
    rows: list[dict[str, Any]] = [
        {
            "name": "ndp_filter_earthscope_station_catalog",
            "args": {
                "filepath": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "latitude": 37.77,
                "longitude": -122.42,
                "radius_km": 75,
            },
            "result": {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "center": {"latitude": 37.77, "longitude": -122.42},
                "radius_km": 75,
                "within_radius_count": 3,
                "stations": [
                    {"station": "UCSF", "distance_km": 3.444},
                    {"station": "SBRB", "distance_km": 9.325},
                    {"station": "SBRU", "distance_km": 9.325},
                ],
                "resource_discovery": {"status": "search_required"},
                "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
            },
        }
    ]
    rows.extend(
        {
            "name": "ndp_search_datasets",
            "args": {"resource_name": station, "resource_format": "CSV", "server": "global"},
            "result": {
                "datasets": [],
                "count": 0,
                "total_found": 0,
                "search_coverage": {
                    "domain": "earthscope_gnss",
                    "status": "covered",
                    "resource_name": station,
                    "resource_format": "CSV",
                    "station_code": station,
                    "station_resource_search": True,
                },
                "_meta": {"tool": "search_datasets", "status": "success"},
            },
        }
        for station in ("UCSF", "SBRB", "SBRU")
    )

    state = _infer_ndp_workflow_state_from_tool_rows(rows)
    augmented = _augment_ndp_search_result_with_runtime_state(rows[-1]["result"], rows)

    assert state["resource_discovery"]["status"] == "search_exhausted"
    assert augmented["clio_runtime"]["terminal"] is True
    assert augmented["clio_runtime"]["workflow_state"]["acquisition"]["status"] == "metadata_only"
    assert augmented["clio_runtime"]["workflow_state"]["acquisition"]["analysis_ready"] is False
    assert "Stop calling ndp_search_datasets" in augmented["clio_runtime"]["next_action"]


def test_ndp_terminal_state_final_answer_fallback_replaces_stale_continuation() -> None:
    state = {
        "station_catalog": {
            "candidate_count": 2,
            "radius_km": 75,
            "center": {"latitude": 37.77, "longitude": -122.42},
            "stations": [
                {"station": "UCSF", "distance_km": 3.444},
                {"station": "SBRB", "distance_km": 9.325},
            ],
        },
        "acquisition": {
            "status": "metadata_only",
            "analysis_ready": False,
            "metadata_path": "/workspace/.clio/artifacts/ndp-staging/earthscope.csv",
            "blocker": "station-specific searches did not return a concrete GNSS time-series CSV",
        },
        "resource_discovery": {
            "status": "search_exhausted",
            "searched_station_ids": ["UCSF", "SBRB"],
        },
    }

    fallback = _ndp_terminal_workflow_state_final_answer_fallback(
        "Next action: search UCSF and SBRB with ndp_search_datasets, then run visualization.",
        state,
    )

    assert "Resource discovery status: search exhausted" in fallback
    assert "Station-specific NDP searches were attempted for: UCSF, SBRB." in fallback
    assert "No GNSS profiling or visualization was run" in fallback


def test_ndp_terminal_state_final_answer_fallback_keeps_terminal_brief() -> None:
    state = {
        "acquisition": {
            "status": "metadata_only",
            "analysis_ready": False,
            "metadata_path": "/workspace/.clio/artifacts/ndp-staging/earthscope.csv",
        },
        "resource_discovery": {
            "status": "search_exhausted",
            "searched_station_ids": ["UCSF"],
        },
    }

    fallback = _ndp_terminal_workflow_state_final_answer_fallback(
        "Search exhausted for station-specific NDP resources. No analysis-ready CSV was staged.",
        state,
    )

    assert fallback == ""


def test_positive_ndp_state_final_answer_fallback_replaces_artifact_only_brief() -> None:
    state = {
        "station_catalog": {
            "candidate_count": 113,
            "radius_km": 75.0,
            "center": {"latitude": 34.05, "longitude": -118.25},
            "stations": [
                {
                    "station": "MTA1",
                    "distance_km": 0.713,
                    "network": "SCGN",
                    "status": "ACTIVE",
                }
            ],
            "status": "ranked_metadata_only",
        },
        "resource_candidate": {
            "status": "selected",
            "resource_name": "MTA1.CI.LY_.30.csv",
            "resource_url": "https://example.test/EarthScope/MTA1.CI.LY_.30.csv",
            "geographically_grounded": True,
            "station_distance_km": 0.713,
        },
        "acquisition": {
            "status": "staged",
            "analysis_ready": True,
            "local_path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
            "source_url": "https://example.test/EarthScope/MTA1.CI.LY_.30.csv",
        },
        "profile": {
            "status": "complete",
            "rows_scanned": 1000,
            "columns": ["time", "east", "north", "up", "sigEE", "sigNN", "sigUU"],
            "numeric_columns": ["east", "north", "up", "sigEE", "sigNN", "sigUU"],
        },
        "artifact": {
            "status": "ready",
            "path": "/workspace/.clio/artifacts/ndp-visualizations/MTA1_time_series.png",
            "columns": ["east", "north", "up"],
        },
        "visualization": {
            "status": "complete",
            "path": "/workspace/.clio/artifacts/ndp-visualizations/MTA1_time_series.png",
            "source_path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
        },
    }

    fallback = _positive_ndp_workflow_state_final_answer_fallback(
        "Staged CSV file: `/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`.\n"
        "Generated plot: `/workspace/.clio/artifacts/ndp-visualizations/MTA1_time_series.png`.",
        state,
    )

    assert "EarthScope GNSS acquisition, profiling, and visualization completed." in fallback
    assert "requested region centered at 34.05, -118.25 with a 75.0 km radius" in fallback
    assert "selected station `MTA1`" in fallback
    assert "0.713 km from the requested center" in fallback
    assert "NDP source URL: https://example.test/EarthScope/MTA1.CI.LY_.30.csv" in fallback
    assert "rows scanned: 1000" in fallback
    assert "`sigEE`" in fallback
    assert "earthquake/event catalog" not in fallback


def test_positive_ndp_state_final_answer_fallback_keeps_complete_brief() -> None:
    state = {
        "resource_candidate": {
            "status": "selected",
            "resource_name": "MTA1.CI.LY_.30.csv",
            "resource_url": "https://example.test/EarthScope/MTA1.CI.LY_.30.csv",
        },
        "acquisition": {
            "status": "staged",
            "analysis_ready": True,
            "local_path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
            "source_url": "https://example.test/EarthScope/MTA1.CI.LY_.30.csv",
        },
    }

    fallback = _positive_ndp_workflow_state_final_answer_fallback(
        "Station MTA1 is 0.713 km from the region center. "
        "The NDP source URL is https://example.test/EarthScope/MTA1.CI.LY_.30.csv. "
        "Profile rows scanned: 1000 with uncertainty columns sigEE, sigNN, sigUU. "
        "CSV `/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`; "
        "PNG `/workspace/.clio/artifacts/ndp-visualizations/MTA1_time_series.png`. "
        "Limitation: no event catalog was included.",
        state,
    )

    assert fallback == ""


def test_ndp_tool_rows_infer_profile_plot_and_station_catalog_from_live_rows() -> None:
    rows = [
        {
            "name": "ndp_filter_earthscope_station_catalog",
            "ok": True,
            "args": {
                "latitude": 34.05,
                "longitude": -118.25,
                "radius_km": 75.0,
            },
            "result": {
                "preview": json.dumps(
                    {
                        "ok": True,
                        "center": {"latitude": 34.05, "longitude": -118.25},
                        "radius_km": 75.0,
                        "within_radius_count": 1,
                        "stations": [
                            {
                                "station": "MTA1",
                                "latitude": 34.05522077,
                                "longitude": -118.24550778,
                                "network": "SCGN",
                                "status": "ACTIVE",
                                "distance_km": 0.713,
                            }
                        ],
                        "_meta": {"tool": "filter_earthscope_station_catalog"},
                    }
                ),
            },
        },
        {
            "name": "ndp_stage_resource",
            "ok": True,
            "args": {
                "dataset_identifier": "https://example.test/EarthScope/MTA1.CI.LY_.30.csv",
                "resource_name": "MTA1.CI.LY_.30.csv",
            },
            "result": {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                "source_url": "https://example.test/EarthScope/MTA1.CI.LY_.30.csv",
            },
        },
        {
            "name": "ndp_profile_csv_resource",
            "ok": True,
            "result": json.dumps(
                {
                    "ok": True,
                    "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                    "columns": [
                        "time",
                        "east",
                        "north",
                        "up",
                        "sigEE",
                        "sigNN",
                        "sigUU",
                    ],
                    "rows_scanned": 250000,
                    "scan_limited": True,
                    "numeric_summary": {
                        "east": {"count": 5000},
                        "north": {"count": 5000},
                        "up": {"count": 5000},
                        "sigEE": {"count": 5000},
                    },
                    "_meta": {"tool": "profile_csv_resource"},
                }
            ),
        },
        {
            "name": "ndp_plot_csv_timeseries",
            "ok": True,
            "result": json.dumps(
                {
                    "ok": True,
                    "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                    "output_path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30_plot.png",
                    "output_size_bytes": 87820,
                    "y_columns": ["east", "north", "up"],
                    "rows_plotted": 2000,
                    "_meta": {"tool": "plot_csv_timeseries"},
                }
            ),
        },
    ]

    state = _infer_ndp_workflow_state_from_tool_rows(rows)
    fallback = _positive_ndp_workflow_state_final_answer_fallback(
        "CSV staged and plot generated.",
        state,
    )

    assert state["station_catalog"]["stations"][0]["distance_km"] == 0.713
    assert state["profile"]["rows_scanned"] == 250000
    assert state["artifact"]["path"].endswith("_plot.png")
    assert "0.713 km from the requested center" in fallback
    assert "rows scanned: 250000" in fallback
    assert "Visualization: `/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30_plot.png`" in fallback


def test_ndp_station_resource_search_ignores_off_region_broad_csv_candidate() -> None:
    output = _append_inferred_workflow_state_from_trajectory(
        "A broad EarthScope CSV search returned an off-region station resource.",
        {
            "steps": [
                {
                    "tool_name": "ndp_filter_earthscope_station_catalog",
                    "observation": {
                        "ok": True,
                        "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                        "center": {"latitude": 37.77, "longitude": -122.42},
                        "radius_km": 75,
                        "within_radius_count": 3,
                        "stations": [
                            {"station": "UCSF", "distance_km": 3.4},
                            {"station": "SBRB", "distance_km": 9.3},
                            {"station": "SBRU", "distance_km": 9.3},
                        ],
                        "resource_discovery": {"status": "search_required"},
                        "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
                    },
                },
                *[
                    {
                        "tool_name": "ndp_search_datasets",
                        "tool_args": {"resource_name": station, "resource_format": "CSV", "server": "global"},
                        "observation": {
                            "datasets": [],
                            "count": 0,
                            "total_found": 0,
                            "search_coverage": {
                                "domain": "earthscope_gnss",
                                "status": "covered",
                                "resource_name": station,
                                "resource_format": "CSV",
                                "station_resource_search": True,
                            },
                            "_meta": {"tool": "search_datasets", "status": "success"},
                        },
                    }
                    for station in ("UCSF", "SBRB", "SBRU")
                ],
                {
                    "tool_name": "ndp_search_datasets",
                    "tool_args": {
                        "search_terms": ["EarthScope", "GNSS", "CSV", "raw_csv"],
                        "resource_format": "CSV",
                        "server": "global",
                    },
                    "observation": {
                        "datasets": [
                            {
                                "id": "wwmt-dataset",
                                "name": "wwmt-ci-ly",
                                "resource_summaries": [
                                    {
                                        "name": "WWMT.CI.LY_.40.csv",
                                        "format": "CSV",
                                        "url": "https://example.test/raw_csv/WWMT.CI.LY_.40.csv",
                                    }
                                ],
                            }
                        ],
                        "count": 1,
                        "total_found": 1,
                        "search_coverage": {
                            "domain": "earthscope_gnss",
                            "status": "covered",
                            "resource_format": "CSV",
                            "station_resource_search": False,
                            "search_terms": ["EarthScope", "GNSS", "CSV", "raw_csv"],
                        },
                        "_meta": {"tool": "search_datasets", "status": "success"},
                    },
                },
            ]
        },
    )

    state = _workflow_state_from_outputs([output])

    assert state["resource_discovery"]["status"] == "search_exhausted"
    assert state["resource_discovery"]["searched_station_ids"] == ["UCSF", "SBRB", "SBRU"]
    assert state["resource_discovery"]["searches"][-1]["status"] == "off_region_candidate_ignored"
    assert state["resource_discovery"]["searches"][-1]["off_region_candidate_station_id"] == "WWMT"
    assert state["acquisition"]["analysis_ready"] is False


def test_ndp_station_resource_search_exhaustion_does_not_override_later_staged_station_csv() -> None:
    output = _append_inferred_workflow_state_from_trajectory(
        "A station CSV was eventually staged after earlier empty station searches.",
        {
            "steps": [
                {
                    "tool_name": "ndp_filter_earthscope_station_catalog",
                    "observation": {
                        "ok": True,
                        "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                        "center": {"latitude": 37.77, "longitude": -122.42},
                        "radius_km": 75,
                        "within_radius_count": 2,
                        "stations": [
                            {"station": "UCSF", "distance_km": 1.2},
                            {"station": "SBRB", "distance_km": 10.2},
                        ],
                        "resource_discovery": {"status": "search_required"},
                        "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
                    },
                },
                {
                    "tool_name": "ndp_search_datasets",
                    "tool_args": {"resource_name": "UCSF", "resource_format": "CSV", "server": "global"},
                    "observation": {
                        "datasets": [],
                        "count": 0,
                        "total_found": 0,
                        "search_coverage": {
                            "domain": "earthscope_gnss",
                            "status": "covered",
                            "resource_name": "UCSF",
                            "resource_format": "CSV",
                            "station_resource_search": True,
                        },
                        "_meta": {"tool": "search_datasets", "status": "success"},
                    },
                },
                {
                    "tool_name": "ndp_stage_resource",
                    "tool_args": {
                        "dataset_identifier": "station-dataset",
                        "resource_name": "SBRB.CI.LY_.20.csv",
                    },
                    "observation": {
                        "ok": True,
                        "path": "/workspace/.clio/artifacts/ndp-staging/SBRB.CI.LY_.20.csv",
                        "resource_name": "SBRB.CI.LY_.20.csv",
                        "source_url": "https://example.test/raw_csv/SBRB.CI.LY_.20.csv",
                    },
                },
            ]
        },
    )

    state = _workflow_state_from_outputs([output])

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"].endswith("SBRB.CI.LY_.20.csv")


def test_truncated_station_catalog_preview_downgrades_off_region_staged_station_csv() -> None:
    station_catalog_preview = json.dumps(
        {
            "ok": True,
            "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
            "center": {"latitude": 37.77, "longitude": -122.42},
            "radius_km": 75,
            "within_radius_count": 2,
            "stations": [
                {"station": "UCSF", "distance_km": 3.4},
                {"station": "EBMD", "distance_km": 13.0},
            ],
            "resource_discovery": {"status": "search_required"},
            "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
        }
    )
    output = _append_inferred_workflow_state_from_trajectory(
        "A broad search staged a CSV after the filtered stations were available.",
        {
            "steps": [
                {
                    "tool_name": "ndp_filter_earthscope_station_catalog",
                    "observation": {
                        "preview": json.dumps(
                            {
                                "original_chars": len(station_catalog_preview),
                                "preview": station_catalog_preview,
                            }
                        ),
                        "truncated": True,
                    },
                },
                {
                    "tool_name": "ndp_stage_resource",
                    "tool_args": {
                        "dataset_identifier": "off-region-dataset",
                        "resource_name": "WWMT.CI.LY_.40.csv",
                    },
                    "observation": {
                        "ok": True,
                        "path": "/workspace/.clio/artifacts/ndp-staging/WWMT.CI.LY_.40.csv",
                        "resource_name": "WWMT.CI.LY_.40.csv",
                        "source_url": "https://example.test/raw_csv/WWMT.CI.LY_.40.csv",
                    },
                },
            ]
        },
    )

    state = _workflow_state_from_outputs([output])

    assert state["station_catalog"]["stations"][0]["station"] == "UCSF"
    assert state["resource_candidate"]["station_id"] == "WWMT"
    assert state["resource_candidate"]["geographically_grounded"] is False
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is False
    assert "does not match the filtered station metadata" in state["acquisition"]["blocker"]


def test_contract_order_blocks_analysis_handoff_for_off_region_staged_station_csv() -> None:
    """Regression for live r21: broad WWMT staging must not authorize analysis."""

    pred = SimpleNamespace(
        trajectory=None,
        tools_called=[
            {
                "name": "ndp_filter_earthscope_station_catalog",
                "args": {
                    "filepath": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                    "latitude": 37.77,
                    "longitude": -122.42,
                    "radius_km": 75,
                },
                "result": {
                    "ok": True,
                    "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                    "center": {"latitude": 37.77, "longitude": -122.42},
                    "radius_km": 75,
                    "within_radius_count": 3,
                    "stations": [
                        {"station": "UCSF", "distance_km": 3.4},
                        {"station": "SBRB", "distance_km": 9.3},
                        {"station": "SBRU", "distance_km": 9.3},
                    ],
                    "resource_discovery": {"status": "search_required"},
                    "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
                },
            },
            {
                "name": "ndp_search_datasets",
                "args": {"resource_name": "UCSF", "resource_format": "CSV", "server": "global"},
                "result": {
                    "datasets": [],
                    "count": 0,
                    "total_found": 0,
                    "search_coverage": {
                        "domain": "earthscope_gnss",
                        "status": "covered",
                        "resource_name": "UCSF",
                        "resource_format": "CSV",
                        "station_resource_search": True,
                    },
                    "_meta": {"tool": "search_datasets", "status": "success"},
                },
            },
            {
                "name": "ndp_search_datasets",
                "args": {
                    "search_terms": ["EarthScope", "GNSS", "GPS", "CSV", "raw_csv", "San Francisco"],
                    "resource_format": "CSV",
                    "server": "global",
                },
                "result": {
                    "datasets": [
                        {
                            "id": "wwmt-dataset",
                            "name": "wwmt-ci-ly-40",
                            "resource_summaries": [
                                {
                                    "name": "WWMT.CI.LY_.40.csv",
                                    "format": "CSV",
                                    "url": "https://example.test/raw_csv/WWMT.CI.LY_.40.csv",
                                }
                            ],
                        }
                    ],
                    "count": 1,
                    "total_found": 1,
                    "search_coverage": {
                        "domain": "earthscope_gnss",
                        "status": "covered",
                        "resource_format": "CSV",
                        "station_resource_search": False,
                        "search_terms": ["EarthScope", "GNSS", "GPS", "CSV", "raw_csv", "San Francisco"],
                    },
                    "_meta": {"tool": "search_datasets", "status": "success"},
                },
            },
            {
                "name": "ndp_stage_resource",
                "args": {
                    "dataset_identifier": "wwmt-dataset",
                    "resource_name": "WWMT.CI.LY_.40.csv",
                },
                "result": {
                    "ok": True,
                    "path": "/workspace/.clio/artifacts/ndp-staging/WWMT.CI.LY_.40.csv",
                    "dataset_id": "wwmt-dataset",
                    "dataset_name": "wwmt-ci-ly-40",
                    "resource_name": "WWMT.CI.LY_.40.csv",
                    "source_url": "https://example.test/raw_csv/WWMT.CI.LY_.40.csv",
                    "_meta": {"tool": "stage_resource", "status": "success"},
                },
            },
        ],
    )
    tool_outputs = _tool_derived_contract_evidence_for_prediction(pred)
    state = _workflow_state_from_outputs(tool_outputs)

    assert state["acquisition"]["analysis_ready"] is False

    parent = AgentDef(
        id="ndp_resource_resolver",
        source="expert_pack",
        title="NDP Resource Resolver",
        parameters={
            "enforce_child_contract_order": True,
            "continuation_contracts": [
                {
                    "id": "acquisition_to_gnss_profile",
                    "when_state": {
                        "acquisition.status": "staged",
                        "acquisition.analysis_ready": True,
                    },
                    "match": "all",
                    "next_expert": "gnss_timeseries_analysis",
                    "next_action": "profile the exact staged station CSV",
                }
            ],
        },
    )

    rows = _filter_child_handoffs_by_contract_order(
        parent,
        [{"agent_id": "gnss_timeseries_analysis", "status": "requested"}],
        completed_outputs=[],
        current_tool_outputs=tool_outputs,
        completed_child_ids=set(),
        declared_child_ids={"gnss_timeseries_analysis"},
    )

    assert rows[0]["status"] == "skipped"
    assert rows[0]["skip_reason"] == "child_contract_order_violation"
    assert rows[0]["allowed_next_children"] == []


def test_metadata_only_acquisition_clears_stale_station_candidate_identity() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "dataset_id": "wwmt-dataset",
                            "dataset_name": "wwmt-ci-ly-40",
                            "resource_name": "WWMT.CI.LY_.40.csv",
                            "resource_url": "https://example.test/raw_csv/WWMT.CI.LY_.40.csv",
                            "station_id": "WWMT",
                            "geographically_grounded": False,
                        },
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                            "metadata_path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                            "source_url": "https://example.test/earthscope_converted_data.csv",
                        },
                    }
                }
            )
        ]
    )

    assert state["resource_candidate"] == {
        "status": "metadata_only",
        "resource_name": "earthscope_converted_data.csv",
        "resource_url": "https://example.test/earthscope_converted_data.csv",
    }
    assert state["acquisition"]["analysis_ready"] is False


def test_ndp_profile_tool_evidence_infers_complete_profile_state() -> None:
    state = _infer_ndp_profile_state_from_tool_evidence(
        json.dumps(
            {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv",
                "size_bytes": 51608375,
                "columns": ["time", "east", "north", "up", "sigEE", "sigNN"],
                "rows_scanned": 5000,
                "scan_limited": True,
                "numeric_summary": {"east": {"min": -1.0}, "north": {"max": 2.0}},
                "_meta": {"tool": "profile_csv_resource", "status": "success"},
            }
        )
    )

    assert state["profile"]["status"] == "complete"
    assert state["profile"]["local_path"].endswith("P475.CI.LY_.20.csv")
    assert state["profile"]["rows_scanned"] == 5000
    assert state["profile"]["scan_limited"] is True
    assert state["profile"]["numeric_columns"] == ["east", "north"]


def test_ndp_profile_trajectory_state_appends_for_parent_contracts() -> None:
    output = _append_inferred_workflow_state_from_trajectory(
        "Profiled the staged GNSS CSV.",
        {
            "observation_0": {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv",
                "columns": ["time", "east", "north", "up"],
                "rows_scanned": 5000,
                "scan_limited": False,
                "numeric_summary": {"east": {}, "north": {}, "up": {}},
                "_meta": {"tool": "profile_csv_resource", "status": "success"},
            }
        },
    )

    state = _workflow_state_from_outputs([output])

    assert state["profile"]["status"] == "complete"
    assert state["profile"]["local_path"].endswith("P475.CI.LY_.20.csv")


def test_nested_child_workflow_state_is_appended_to_parent_output() -> None:
    parent_output = _append_nested_workflow_state(
        "Analysis parent summarized its children.",
        [
            {
                "agent_id": "gnss_timeseries_analysis",
                "stage": "delegate.completed",
                "status": "completed",
                "output_summary": json.dumps(
                    {
                        "workflow_state": {
                            "profile": {
                                "status": "complete",
                                "local_path": "/workspace/P475.CI.LY_.20.csv",
                            }
                        }
                    }
                ),
                "tools_called": [
                    {
                        "name": "ndp_stage_resource",
                        "args": {
                            "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                        },
                        "result": {
                            "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                        },
                        "ok": True,
                    }
                ],
            }
        ],
    )

    state = _workflow_state_from_outputs([parent_output])

    assert "CLIO merged nested typed workflow state" in parent_output
    assert state["profile"]["status"] == "complete"
    assert state["profile"]["local_path"].endswith("P475.CI.LY_.20.csv")
    assert state["resource_candidate"]["dataset_id"] == "1b0c1b93-f164-4025-bd7b-000252b5ca18"
    assert state["resource_candidate"]["resource_name"] == "MTA1.CI.LY_.30.csv"


def test_nested_tool_row_json_result_overrides_stale_candidate_state(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "PKRD.CI.LY_.20.csv"
    staged_csv.write_text("time,east,north,up\n2024-01-01,0,0,0\n")

    parent_output = _append_nested_workflow_state(
        json.dumps(
            {
                "workflow_state": {
                    "resource_candidate": {"status": "selected"},
                    "acquisition": {
                        "status": "candidate_found",
                        "analysis_ready": False,
                        "local_path": str(staged_csv),
                        "blocker": "analysis-ready acquisition requires a staged local CSV path",
                    },
                }
            }
        ),
        [
            {
                "agent_id": "ndp_resource_resolver",
                "stage": "delegate.completed",
                "status": "completed",
                "tools_called": [
                    {
                        "name": "ndp_stage_resource",
                        "args": {
                            "dataset_identifier": "5dcd10ce-d77f-4bf1-8363-ce597892b120",
                            "resource_name": "PKRD.CI.LY_.20.csv",
                        },
                        "result": json.dumps(
                            {
                                "staged": True,
                                "path": str(staged_csv),
                                "dataset_id": "5dcd10ce-d77f-4bf1-8363-ce597892b120",
                                "resource_name": "PKRD.CI.LY_.20.csv",
                                "_meta": {"tool": "stage_resource", "status": "success"},
                            }
                        ),
                        "ok": True,
                    }
                ],
            }
        ],
    )

    state = _workflow_state_from_outputs([parent_output])

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert "blocker" not in state["acquisition"]
    assert state["resource_candidate"]["resource_name"] == "PKRD.CI.LY_.20.csv"


def test_nested_station_filter_preview_state_grounds_staged_station_csv(
    tmp_path: Path,
) -> None:
    metadata_csv = tmp_path / "earthscope_converted_data.csv"
    metadata_csv.write_text("Station,Latitude,Longitude\nMTA1,34.05522077,-118.24550778\n")
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2024-01-01,0,0,0\n")

    filter_result = {
        "ok": True,
        "path": str(metadata_csv),
        "center": {"latitude": 34.05, "longitude": -118.25},
        "radius_km": 75,
        "within_radius_count": 1,
        "stations": [
            {
                "station": "MTA1",
                "latitude": 34.05522077,
                "longitude": -118.24550778,
                "network": "SCGN",
                "status": "ACTIVE",
                "distance_km": 0.713,
                "suggested_search_terms": ["MTA1", "MTA1.CI.LY"],
            }
        ],
        "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
    }
    parent_output = _append_nested_workflow_state(
        "Resolver staged a station CSV after filtering the station catalog.",
        [
            {
                "agent_id": "ndp_resource_resolver",
                "stage": "delegate.completed",
                "status": "completed",
                "tools_called": [
                    {
                        "name": "ndp_filter_earthscope_station_catalog",
                        "args": {"filepath": str(metadata_csv)},
                        "result": {"preview": json.dumps(filter_result)},
                        "ok": True,
                    },
                    {
                        "name": "ndp_stage_resource",
                        "args": {
                            "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                        },
                        "result": json.dumps(
                            {
                                "staged": True,
                                "path": str(staged_csv),
                                "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                                "resource_name": "MTA1.CI.LY_.30.csv",
                                "_meta": {"tool": "stage_resource", "status": "success"},
                            }
                        ),
                        "ok": True,
                    },
                ],
            }
        ],
    )

    state = _workflow_state_from_outputs([parent_output])

    assert state["station_catalog"]["status"] == "ranked_metadata_only"
    assert state["resource_candidate"]["resource_name"] == "MTA1.CI.LY_.30.csv"
    assert state["resource_candidate"]["geographically_grounded"] is True
    assert state["resource_candidate"]["station_distance_km"] == 0.713
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True


def test_compacted_delegation_output_retains_parseable_workflow_state() -> None:
    workflow_state = {
        "workflow_state": {
            "acquisition": {
                "analysis_ready": True,
                "local_path": "/workspace/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv",
                "required_columns": ["time", "east", "north", "up"],
                "source_url": "https://ds2.example.test/raw_csv/P475.CI.LY_.20.csv",
                "status": "staged",
            },
            "resource_candidate": {
                "resource_name": "P475.CI.LY_.20.csv",
                "resource_url": "https://ds2.example.test/raw_csv/P475.CI.LY_.20.csv",
                "status": "selected",
            },
        }
    }
    output = (
        "Selected GNSS station P475 for the requested region.\n"
        + ("long model prose that should not decide routing\n" * 120)
        + "\nCLIO inferred typed tool state:\n"
        + json.dumps(workflow_state)
    )

    compacted = _compact_dynamic_delegation_output(output, limit=900)
    state = _workflow_state_from_outputs([compacted])

    assert "Retained typed workflow state" in compacted
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"].endswith("P475.CI.LY_.20.csv")


def test_user_facing_child_evidence_summary_removes_retained_scaffolding() -> None:
    compacted = (
        "**Region & Station**\n"
        "- Station MTA1 is 0.71 km from the requested center.\n"
        "- No blocker remains; the staged resource is a valid GNSS CSV.\n\n"
        "**Ev\n\n"
        "[...delegation output truncated; exact evidence retained below...]\n\n"
        "[exact retained evidence index]\n"
        "Paths:\n"
        "- /tmp/MTA1.CI.LY_.30.csv\n\n"
        "Retained typed workflow state:\n"
        '{"workflow_state": {"acquisition": {"status": "staged"}}}'
    )

    visible = _user_facing_dynamic_evidence_summary(compacted)

    assert "[exact retained evidence index]" not in visible
    assert "Retained typed workflow state" not in visible
    assert "**Ev" not in visible
    assert "Station MTA1" in visible
    assert "valid GNSS CSV" in visible


def test_user_facing_summary_removes_clio_typed_state_blocks() -> None:
    output = (
        "**Synthesis**\n\n"
        "- Staged CSV: `/tmp/MTA1.CI.LY_.30.csv`\n"
        "- Generated plot: `/tmp/MTA1.CI.LY_.30_timeseries.png`\n\n"
        "The acquisition is analysis-ready and the plot was generated.\n\n"
        "CLIO typed workflow state:\n"
        '{"workflow_state":{"acquisition":{"status":"staged"}}}\n\n'
        "CLIO inferred typed tool state from tool observations:\n"
        '{"workflow_state":{"artifact":{"status":"ready"}}}'
    )

    visible = _user_facing_dynamic_evidence_summary(output)

    assert "Staged CSV" in visible
    assert "Generated plot" in visible
    assert "CLIO typed workflow state" not in visible
    assert "CLIO inferred typed tool state" not in visible
    assert "workflow_state" not in visible


def test_forwarded_child_prose_is_pending_work_not_final_answer() -> None:
    answer = (
        "The request has been forwarded to the analysis child expert. "
        "No further action is required from the current expert until the child returns."
    )

    assert _dynamic_answer_has_pending_child_work(answer) is True
    assert _dynamic_answer_is_delegation_placeholder(answer) is True


def test_earlier_response_prose_is_pending_work_not_final_answer() -> None:
    answer = "The earlier response already fulfills the full request. No further processing is needed."

    assert _dynamic_answer_has_pending_child_work(answer) is True
    assert _dynamic_answer_is_delegation_placeholder(answer) is True


def test_will_request_child_prose_is_pending_work_not_final_answer() -> None:
    answer = (
        "We will request the data child expert to obtain the required GNSS "
        "station time-series and stage them as CSV resources for later analysis."
    )

    assert _dynamic_answer_has_pending_child_work(answer) is True
    assert _dynamic_answer_is_delegation_placeholder(answer) is True


def test_pending_parent_output_can_fall_back_to_latest_nested_child_evidence() -> None:
    rows = [
        {
            "agent_id": "ndp_dataset_discovery",
            "stage": "delegate.completed",
            "status": "completed",
            "output_summary": json.dumps(
                {
                    "workflow_state": {
                        "catalog": {"status": "metadata_found"},
                        "resource_discovery": {
                            "status": "search_exhausted",
                            "searches": ["WXYZ EarthScope GNSS CSV"],
                        },
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                        },
                    }
                }
            ),
        }
    ]
    parent_output = (
        "Dataset discovery pending - child expert will confirm presence or absence "
        "of GNSS stations/time-series."
    )

    assert _dynamic_answer_has_pending_child_work(parent_output) is True
    assert "search_exhausted" in _latest_delegation_output_summary(rows)


def test_latest_final_child_output_prefers_synthesis_summary() -> None:
    output = _latest_final_child_output_summary(
        [
            {
                "agent_id": "visualization",
                "stage": "delegate.completed",
                "status": "completed",
                "output_summary": "PNG artifact: /workspace/plot.png",
            },
            {
                "agent_id": "synthesis",
                "stage": "delegate.completed",
                "status": "completed",
                "output_summary": "Final synthesized answer with cited data and caveats.",
            },
        ]
    )

    assert output == "Final synthesized answer with cited data and caveats."


def test_ndp_staging_failure_infers_blocked_acquisition_workflow_state() -> None:
    state = _infer_ndp_workflow_state_from_tool_evidence(
        "- **Staged CSV path:** *none (staging failed)*\n"
        "- **Selected source URL:** https://ds2.example.test/raw_csv/SAND.CI.LY_.00.csv\n"
        "- **Staging blocker:** NDP service unavailable - 502 Proxy Error"
    )

    assert state["resource_candidate"]["status"] == "missing"
    assert state["acquisition"]["status"] == "blocked"
    assert state["acquisition"]["analysis_ready"] is False
    assert "could not be staged" in state["acquisition"]["blocker"]


def test_ndp_acquisition_blocked_prose_infers_blocked_workflow_state() -> None:
    state = _infer_ndp_workflow_state_from_tool_evidence(
        "Acquisition blocked: No verifiable EarthScope GNSS station CSV found "
        "within 75 km of 34.05 N, -118.25 W. No CSV staged."
    )

    assert state["resource_candidate"]["status"] == "missing"
    assert state["acquisition"]["status"] == "blocked"
    assert state["acquisition"]["analysis_ready"] is False


def test_blueprint_next_expert_marker_converts_latest_declared_uncompleted_child() -> None:
    rows = _next_expert_marker_handoffs(
        source_text="request",
        completed_outputs=[
            "NEXT_EXPERT: analysis\nNEXT_ACTION: run_sac_fallback\nresource_too_large",
            "NEXT_EXPERT: visualization\nNEXT_ACTION: plot_sac_traces /tmp/wave.sac\n/tmp/wave.sac",
        ],
        declared_child_ids={"analysis", "visualization"},
        completed_child_ids={"analysis"},
    )

    assert len(rows) == 1
    assert rows[0]["delegate_to"] == "visualization"
    assert rows[0]["source"] == "blueprint_next_expert_marker"
    assert "plot_sac_traces /tmp/wave.sac" in rows[0]["question"]


def test_blueprint_next_expert_marker_appends_observed_sac_path_when_action_is_generic() -> None:
    rows = _next_expert_marker_handoffs(
        source_text="request",
        completed_outputs=[
            "Trace statistics\n"
            "LOCAL_SAC_PATH: /tmp/clio-seismic/earthscope_IU_ANMO.sac\n"
            "NEXT_EXPERT: visualization\n"
            "NEXT_ACTION: plot_sac_traces"
        ],
        declared_child_ids={"visualization"},
        completed_child_ids=set(),
    )

    assert len(rows) == 1
    assert rows[0]["delegate_to"] == "visualization"
    assert "Runtime-selected local SAC path: /tmp/clio-seismic/earthscope_IU_ANMO.sac" in rows[0]["question"]


def test_blueprint_next_expert_marker_ignores_unknown_or_completed_targets() -> None:
    assert not _next_expert_marker_handoffs(
        source_text="request",
        completed_outputs=["NEXT_EXPERT: shell\nNEXT_ACTION: do unsafe thing"],
        declared_child_ids={"analysis"},
        completed_child_ids=set(),
    )
    assert not _next_expert_marker_handoffs(
        source_text="request",
        completed_outputs=["NEXT_EXPERT: analysis\nNEXT_ACTION: repeat"],
        declared_child_ids={"analysis"},
        completed_child_ids={"analysis"},
    )


def test_blueprint_compiler_selects_declared_dspy_module_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any, list[Any]]] = []

    class FakePredict:
        def __init__(self, signature: Any) -> None:
            calls.append(("predict", signature, []))

    class FakeChainOfThought:
        def __init__(self, signature: Any) -> None:
            calls.append(("chain_of_thought", signature, []))

    class FakeReAct:
        def __init__(self, signature: Any, *, tools: list[Any], max_iters: int) -> None:
            self.max_iters = max_iters
            calls.append(("react", signature, tools))

    scoped_tool = dspy.Tool(
        func=lambda question: f"scoped:{question}",
        name="scoped_tool",
        desc="Scoped test tool.",
        args={"question": {"type": "string"}},
    )
    child_tool = dspy.Tool(
        func=lambda question: f"child:{question}",
        name="delegate_to_child",
        desc="Child expert tool.",
        args={"question": {"type": "string"}},
    )

    monkeypatch.setattr(dspy, "Predict", FakePredict)
    monkeypatch.setattr(dspy, "ChainOfThought", FakeChainOfThought)
    monkeypatch.setattr(dspy, "ReAct", FakeReAct)
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(provider="openai", model="gpt-5-mini"),
    )
    monkeypatch.setattr("clio_agent.gact.app._dynamic_agent_tools", lambda base_agent, agent_def: [scoped_tool])
    monkeypatch.setattr("clio_agent.gact.app._dynamic_child_expert_tools", lambda base_agent, agent_def: [child_tool])

    base_agent = SimpleNamespace()
    predict = _build_blueprint_dspy_module(
        base_agent,
        AgentDef(id="predictor", source="expert_pack", title="Predictor", module={"kind": "predict"}),
    )
    cot = _build_blueprint_dspy_module(
        base_agent,
        AgentDef(id="reasoner", source="expert_pack", title="Reasoner", module={"kind": "chain_of_thought"}),
    )
    react = _build_blueprint_dspy_module(
        base_agent,
        AgentDef(
            id="reactor",
            source="expert_pack",
            title="Reactor",
            module={"kind": "react"},
            parameters={"max_iters": 7},
            tools=["scoped_tool"],
        ),
    )

    assert predict.kind == "predict"
    assert cot.kind == "chain_of_thought"
    assert react.kind == "react"
    assert calls[0][0] == "predict"
    assert calls[1][0] == "chain_of_thought"
    assert calls[2][0] == "react"
    assert [tool.name for tool in calls[2][2]] == ["scoped_tool", "delegate_to_child"]
    assert react.program.max_iters == 7


def test_blueprint_react_recovers_malformed_final_tool_intent() -> None:
    def fake_stage_resource(**kwargs: Any) -> dict[str, Any]:
        return {
            "staged": True,
            "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
            "dataset_id": kwargs["dataset_identifier"],
            "resource_name": kwargs["resource_name"],
            "_meta": {"tool": "stage_resource", "status": "success"},
        }

    tool = dspy.Tool(
        func=fake_stage_resource,
        name="ndp_stage_resource",
        desc="Stage an NDP resource.",
        args={},
    )
    exc = RuntimeError(
        "Adapter JSONAdapter failed to parse the LM response.\n\n"
        'LM Response: {"tool_name":"ndp_stage_resource","tool_args":'
        '{"dataset_identifier":"changed-dataset","resource_name":"MTA1.CI.LY_.30.csv"}}'
    )

    recovered = _recover_blueprint_react_tool_intent(tools=[tool], exc=exc)

    assert recovered is not None
    assert recovered.tools_called[0]["name"] == "ndp_stage_resource"
    assert recovered.tools_called[0]["ok"] is True
    assert "Recovered a malformed ReAct tool intent" in recovered.answer
    state = _workflow_state_from_outputs([recovered.answer])
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["resource_candidate"]["resource_name"] == "MTA1.CI.LY_.30.csv"


def test_blueprint_runner_uses_dspy_module_call_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeBlueprintModule:
        def __call__(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(answer="called")

        def forward(self, **kwargs: Any) -> Any:
            raise AssertionError("blueprint runner must use the DSPy module call path")

    monkeypatch.setattr(
        "clio_agent.gact.app._build_blueprint_dspy_module",
        lambda base_agent, agent_def: FakeBlueprintModule(),
    )

    result = _run_blueprint_dspy_agent(
        SimpleNamespace(),
        AgentDef(id="data", source="expert_pack", title="Data"),
        "prove call path",
        "sess_test",
    )

    assert result.answer == "called"
    assert calls == [
        {
            "question": "prove call path",
            "session_id": "sess_test",
            "cancel_requested": None,
        }
    ]


def test_blueprint_module_allows_handoff_only_root_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProgram:
        def __call__(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                answer="",
                expert_handoffs='[{"agent_id":"reference","parent_id":"main","task":"inspect fasta"}]',
            )

    class FakePredict:
        def __init__(self, signature: Any) -> None:
            self.signature = signature

        def __call__(self, **kwargs: Any) -> Any:
            return FakeProgram()(**kwargs)

    monkeypatch.setattr(dspy, "Predict", FakePredict)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(provider="argonne", model="gpt-oss-120b"),
    )

    module = _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(id="main", source="expert_pack", title="Main", module={"kind": "predict"}),
    )

    result = module(question="delegate", session_id="session-123")

    assert result.answer == ""
    assert result.selected_expert == "main"
    assert result.route_source == "agent_blueprint"
    assert result.expert_handoffs == [
        {"agent_id": "reference", "parent_id": "main", "task": "inspect fasta"}
    ]


def test_blueprint_module_empty_answer_with_children_enters_repair_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProgram:
        def __call__(self, **kwargs: Any) -> Any:
            return SimpleNamespace(answer="", expert_handoffs="")

    class FakePredict:
        def __init__(self, signature: Any) -> None:
            self.signature = signature

        def __call__(self, **kwargs: Any) -> Any:
            return FakeProgram()(**kwargs)

    monkeypatch.setattr(dspy, "Predict", FakePredict)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(provider="argonne", model="gpt-oss-120b"),
    )
    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_dynamic_agent_children_context",
        lambda app, agent_def, session_id="": "Declared child experts available:\n- reference",
    )

    module = _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(id="main", source="expert_pack", title="Main", module={"kind": "predict"}),
    )

    token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    try:
        with _gact_app_context(SimpleNamespace()):
            result = module(question="inspect", session_id="session-123")
    finally:
        _ACTIVE_GACT_SESSION_ID.reset(token)

    assert result.answer == ""
    assert result.selected_expert == "main"
    assert result.route_source == "agent_blueprint"
    assert result.expert_handoffs == []
    assert "declared-child handoff repair" in result.routing_rationale


def test_blueprint_react_empty_answer_preserves_tool_trajectory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReact:
        def __init__(self, signature: Any, *, tools: list[Any], max_iters: int) -> None:
            self.signature = signature
            self.tools = tools
            self.max_iters = max_iters

        def __call__(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                answer="",
                expert_handoffs="",
                trajectory={
                    "observations": [
                        {
                            "tool_name": "hdf5_list_datasets",
                            "result": {"datasets": ["safe_float"], "checksum": "abc123"},
                        }
                    ]
                },
            )

    monkeypatch.setattr(dspy, "ReAct", FakeReact)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(provider="argonne", model="gpt-oss-120b"),
    )
    monkeypatch.setattr("clio_agent.gact.app._dynamic_agent_tools", lambda base_agent, agent_def: [])
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_child_expert_tools",
        lambda base_agent, agent_def: [],
    )

    module = _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(id="source_inspect", source="expert_pack", title="Source", module={"kind": "react"}),
    )

    result = module(question="inspect", session_id="session-123")

    assert "hdf5_list_datasets" in result.answer
    assert "safe_float" in result.answer
    assert result.tools_called == [
        {
            "name": "hdf5_list_datasets",
            "result": {"datasets": ["safe_float"], "checksum": "abc123"},
            "ok": True,
            "telemetry_source": "agent_trajectory",
        }
    ]
    assert result.route_source == "agent_blueprint"


def test_blueprint_react_terminal_workflow_state_returns_final_prediction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not issubclass(_BlueprintTerminalWorkflowState, Exception)

    terminal_result = {
        "datasets": [],
        "count": 0,
        "_meta": {
            "tool": "search_datasets",
            "status": "skipped",
            "reason": "resource_discovery_search_exhausted",
        },
        "clio_runtime": {
            "terminal": True,
            "workflow_state": {
                "acquisition": {
                    "status": "metadata_only",
                    "analysis_ready": False,
                    "blocker": "No concrete station CSV resources were found.",
                },
                "resource_discovery": {"status": "search_exhausted"},
            },
        },
    }

    class FakeReact:
        def __init__(self, signature: Any, *, tools: list[Any], max_iters: int) -> None:
            self.signature = signature
            self.tools = tools
            self.max_iters = max_iters

        def __call__(self, **kwargs: Any) -> Any:
            rows = _ACTIVE_BLUEPRINT_TOOL_ROWS.get()
            if rows is not None:
                rows.append(
                    {
                        "name": "ndp_search_datasets",
                        "args": {"resource_name": "UCSF", "resource_format": "CSV"},
                        "ok": True,
                        "result": terminal_result,
                        "telemetry_source": "blueprint_react_tool_wrapper",
                        "skipped": True,
                    }
                )
            raise _BlueprintTerminalWorkflowState(terminal_result)

    monkeypatch.setattr(dspy, "ReAct", FakeReact)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(provider="argonne", model="gpt-oss-120b"),
    )
    monkeypatch.setattr("clio_agent.gact.app._dynamic_agent_tools", lambda base_agent, agent_def: [])
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_child_expert_tools",
        lambda base_agent, agent_def: [],
    )

    module = _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(
            id="ndp_resource_resolver",
            source="expert_pack",
            title="Resolver",
            module={"kind": "react"},
        ),
    )

    result = module(question="resolve station CSVs", session_id="session-123")
    workflow_state = _workflow_state_from_outputs([result.workflow_state, result.answer])

    assert result.route_source == "agent_blueprint"
    assert workflow_state["resource_discovery"]["status"] == "search_exhausted"
    assert workflow_state["acquisition"]["analysis_ready"] is False
    assert "metadata-only" in result.answer
    assert len(result.tools_called) == 1
    terminal_tool_call = result.tools_called[0]
    assert terminal_tool_call["name"] == "ndp_search_datasets"
    assert terminal_tool_call["args"] == {"resource_name": "UCSF", "resource_format": "CSV"}
    assert terminal_tool_call["ok"] is True
    assert terminal_tool_call["telemetry_source"] == "blueprint_react_tool_wrapper"
    assert terminal_tool_call["result"]["_meta"]["reason"] == "resource_discovery_search_exhausted"
    assert terminal_tool_call["result"]["_meta"]["status"] == "skipped"
    assert terminal_tool_call["result"]["clio_runtime"]["terminal"] is True


def test_extract_tools_called_from_indexed_react_trajectory() -> None:
    rows = _extract_tools_called_from_trajectory(
        {
            "step_0_tool_name": "ndp_get_dataset_details",
            "step_0_tool_args": {
                "dataset_identifier": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
                "server": "global",
            },
            "step_0_observation": {
                "resources": [
                    {
                        "name": "earthscope_converted_data.csv",
                        "url": "https://example.test/earthscope_converted_data.csv",
                    }
                ]
            },
        }
    )

    assert rows == [
        {
            "name": "ndp_get_dataset_details",
            "args": {
                "dataset_identifier": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
                "server": "global",
            },
            "result": {
                "resources": [
                    {
                        "name": "earthscope_converted_data.csv",
                        "url": "https://example.test/earthscope_converted_data.csv",
                    }
                ]
            },
            "ok": True,
            "telemetry_source": "agent_trajectory",
        }
    ]


def test_merge_tool_call_rows_deduplicates_matching_call_id_with_result_evidence() -> None:
    rows = _merge_tool_call_rows(
        [
            {
                "call_id": "call_same",
                "name": "ndp_search_datasets",
                "args": {"search_terms": ["UCSF"]},
                "ok": True,
                "result": {"datasets": []},
                "telemetry_source": "live_observer",
            }
        ],
        [
            {
                "call_id": "call_same",
                "name": "ndp_search_datasets",
                "args": {"search_terms": ["UCSF"]},
                "ok": True,
                "result": {"datasets": []},
                "telemetry_source": "child_handoff",
            }
        ],
    )

    assert len(rows) == 1
    assert rows[0]["call_id"] == "call_same"


def test_failed_child_delegation_state_preserves_tool_evidence_as_blocker() -> None:
    state = _failed_child_delegation_workflow_state(
        prompt=(
            "Prior structured blueprint state:\n"
            '{"acquisition":{"status":"metadata_only","analysis_ready":false},'
            '"resource_discovery":{"status":"search_required"}}'
        ),
        child_agent_id="earthscope_station_catalog",
        parent_agent_id="ndp_dataset_discovery",
        error="AuthenticationError",
        message="token inactive",
        tools_called=[
            {
                "name": "ndp_stage_resource",
                "args": {
                    "dataset_identifier": "811f0bcc",
                    "resource_name": "earthscope_converted_data.csv",
                },
                "ok": True,
                "result": {
                    "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                    "dataset_id": "811f0bcc",
                    "dataset_name": "earthscope_stations",
                    "resource_name": "earthscope_converted_data.csv",
                    "source_url": "https://example.test/earthscope_converted_data.csv",
                },
            }
        ],
    )

    assert state["delegation"]["status"] == "failed"
    assert state["delegation"]["failed_child"] == "earthscope_station_catalog"
    assert state["acquisition"]["status"] == "blocked"
    assert state["acquisition"]["analysis_ready"] is False
    assert "AuthenticationError" in state["acquisition"]["blocker"]
    assert state["resource_discovery"]["status"] == "child_failed"
    assert state["resource_candidate"]["status"] == "metadata_only"


def test_failed_child_delegation_output_summary_is_parent_parseable() -> None:
    state = {
        "delegation": {
            "status": "failed",
            "failed_child": "earthscope_station_catalog",
        },
        "acquisition": {"status": "blocked", "analysis_ready": False},
    }

    summary = _failed_child_delegation_output_summary(
        child_agent_id="earthscope_station_catalog",
        parent_agent_id="ndp_dataset_discovery",
        error="AuthenticationError",
        message="token inactive",
        workflow_state=state,
    )

    assert "Child expert 'earthscope_station_catalog' failed" in summary
    assert _workflow_state_from_outputs([summary])["acquisition"]["status"] == "blocked"


def test_nested_handoff_tool_calls_preserve_child_result_evidence() -> None:
    rows = _tool_calls_from_handoff_rows(
        [
            {
                "agent_id": "main",
                "children": [
                    {
                        "agent_id": "ndp_resource_resolver",
                        "tools_called": [
                            {
                                "name": "ndp_stage_resource",
                                "args": {
                                    "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                                    "resource_name": "MTA1.CI.LY_.30.csv",
                                },
                                "result": {
                                    "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                                    "resource_name": "MTA1.CI.LY_.30.csv",
                                },
                                "ok": True,
                                "telemetry_source": "agent_trajectory",
                            }
                        ],
                    }
                ],
            }
        ]
    )

    assert rows == [
        {
            "name": "ndp_stage_resource",
            "args": {
                "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "resource_name": "MTA1.CI.LY_.30.csv",
            },
            "ok": True,
            "telemetry_source": "agent_trajectory",
            "result": {
                "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                "resource_name": "MTA1.CI.LY_.30.csv",
            },
        }
    ]


def test_generated_child_expert_tool_runs_declared_child_and_returns_compact_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    parent = AgentDef(id="root", source="expert_pack", title="Root")
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")
    calls: list[dict[str, Any]] = []

    def fake_run_dynamic_agent_compat(runner, base_agent, agent_def, question, session_id, cancel_requested):
        calls.append(
            {
                "runner": runner,
                "agent_id": agent_def.id,
                "question": question,
                "session_id": session_id,
                "cancel_requested": cancel_requested,
            }
        )
        return SimpleNamespace(
            answer="This is a long child analysis answer with enough detail to summarize.",
            evidence='[{"claim":"supported"}]',
            artifacts="",
            errors="",
            delegation='{"return_to":"root"}',
        )

    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "child-runner")
    monkeypatch.setattr("clio_agent.gact.app._run_dynamic_agent_compat", fake_run_dynamic_agent_compat)

    token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    try:
        with _gact_app_context(app):
            tool = _build_child_expert_tool(SimpleNamespace(), parent, child)
            payload = json.loads(tool(question="inspect the evidence"))
    finally:
        _ACTIVE_GACT_SESSION_ID.reset(token)

    assert tool.name == "delegate_to_analysis"
    assert calls == [
        {
            "runner": "child-runner",
            "agent_id": "analysis",
            "question": "inspect the evidence",
            "session_id": "session-123",
            "cancel_requested": None,
        }
    ]
    assert payload["agent_id"] == "analysis"
    assert payload["parent_id"] == "root"
    assert payload["status"] == "completed"
    assert payload["return_payload"] == "compact_result"
    assert payload["structured"] == {
        "evidence": '[{"claim":"supported"}]',
        "delegation": '{"return_to":"root"}',
    }
    assert "child analysis answer" in payload["output_summary"]


def test_recording_blueprint_tool_captures_context_local_tool_result() -> None:
    def sample_tool(station: str) -> dict[str, Any]:
        return {"station": station, "ok": True}

    tool = dspy.Tool(
        func=sample_tool,
        name="sample_station_tool",
        desc="Sample station tool",
        args={"station": {"type": "string"}},
    )
    rows: list[dict[str, Any]] = []
    token = _ACTIVE_BLUEPRINT_TOOL_ROWS.set(rows)
    try:
        wrapped = _recording_blueprint_tool(tool)
        result = wrapped(station="UCSF")
    finally:
        _ACTIVE_BLUEPRINT_TOOL_ROWS.reset(token)

    assert result == {"station": "UCSF", "ok": True}
    assert rows == [
        {
            "name": "sample_station_tool",
            "args": {"station": "UCSF"},
            "ok": True,
            "duration_ms": pytest.approx(rows[0]["duration_ms"]),
            "result": {"station": "UCSF", "ok": True},
            "telemetry_source": "blueprint_react_tool_wrapper",
        }
    ]


def test_recording_blueprint_tool_skips_ndp_search_after_station_exhaustion() -> None:
    def fail_search(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"ndp search should have been short-circuited: {kwargs}")

    tool = dspy.Tool(
        func=fail_search,
        name="ndp_search_datasets",
        desc="Search NDP datasets",
        args={"resource_name": {"type": "string"}},
    )
    rows: list[dict[str, Any]] = [
        {
            "name": "ndp_filter_earthscope_station_catalog",
            "args": {
                "filepath": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "latitude": 37.77,
                "longitude": -122.42,
                "radius_km": 75,
            },
            "result": {
                "ok": True,
                "path": "/workspace/.clio/artifacts/ndp-staging/earthscope_converted_data.csv",
                "center": {"latitude": 37.77, "longitude": -122.42},
                "radius_km": 75,
                "within_radius_count": 3,
                "stations": [
                    {"station": "UCSF", "distance_km": 3.444},
                    {"station": "SBRB", "distance_km": 9.325},
                    {"station": "SBRU", "distance_km": 9.325},
                ],
                "resource_discovery": {"status": "search_required"},
                "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
            },
        },
        *[
            {
                "name": "ndp_search_datasets",
                "args": {"resource_name": station, "resource_format": "CSV", "server": "global"},
                "result": {
                    "datasets": [],
                    "count": 0,
                    "total_found": 0,
                    "search_coverage": {
                        "domain": "earthscope_gnss",
                        "status": "covered",
                        "resource_name": station,
                        "resource_format": "CSV",
                        "station_code": station,
                        "station_resource_search": True,
                    },
                    "_meta": {"tool": "search_datasets", "status": "success"},
                },
            }
            for station in ("UCSF", "SBRB", "SBRU")
        ],
    ]
    token = _ACTIVE_BLUEPRINT_TOOL_ROWS.set(rows)
    try:
        wrapped = _recording_blueprint_tool(tool)
        with pytest.raises(_BlueprintTerminalWorkflowState) as raised:
            wrapped(resource_name="UCSF", resource_format="CSV", server="global")
    finally:
        _ACTIVE_BLUEPRINT_TOOL_ROWS.reset(token)
    result = raised.value.result

    assert result["_meta"] == {
        "tool": "search_datasets",
        "status": "skipped",
        "reason": "resource_discovery_search_exhausted",
    }
    assert result["clio_runtime"]["terminal"] is True
    assert result["clio_runtime"]["workflow_state"]["resource_discovery"]["status"] == "search_exhausted"
    assert rows[-1]["skipped"] is True
    assert rows[-1]["result"]["clio_runtime"]["terminal"] is True


def test_recording_blueprint_tool_skips_ndp_search_from_inherited_workflow_state() -> None:
    def fail_search(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"ndp search should have been short-circuited: {kwargs}")

    tool = dspy.Tool(
        func=fail_search,
        name="ndp_search_datasets",
        desc="Search NDP datasets",
        args={"resource_name": {"type": "string"}},
    )
    rows: list[dict[str, Any]] = [
        {
            "name": "clio_prior_workflow_state",
            "args": {},
            "ok": True,
            "result": {},
            "workflow_state": {
                "acquisition": {
                    "status": "metadata_only",
                    "analysis_ready": False,
                    "metadata_path": (
                        "/workspace/.clio/artifacts/ndp-staging/"
                        "earthscope_converted_data.csv"
                    ),
                },
                "station_catalog": {
                    "status": "ranked_metadata_only",
                    "stations": [
                        {"station": "UCSF", "distance_km": 3.444},
                        {"station": "SBRB", "distance_km": 9.325},
                        {"station": "SBRU", "distance_km": 9.325},
                    ],
                },
                "resource_discovery": {"status": "search_required"},
            },
            "telemetry_source": "blueprint_react_context_seed",
        },
        *[
            {
                "name": "ndp_search_datasets",
                "args": {"resource_name": station, "resource_format": "CSV", "server": "global"},
                "result": {
                    "datasets": [],
                    "count": 0,
                    "total_found": 0,
                    "search_coverage": {
                        "domain": "earthscope_gnss",
                        "status": "covered",
                        "resource_name": station,
                        "resource_format": "CSV",
                        "station_code": station,
                        "station_resource_search": True,
                    },
                    "_meta": {"tool": "search_datasets", "status": "success"},
                },
            }
            for station in ("UCSF", "SBRB", "SBRU")
        ],
    ]
    token = _ACTIVE_BLUEPRINT_TOOL_ROWS.set(rows)
    try:
        wrapped = _recording_blueprint_tool(tool)
        with pytest.raises(_BlueprintTerminalWorkflowState) as raised:
            wrapped(resource_name="MHDL", resource_format="CSV", server="global")
    finally:
        _ACTIVE_BLUEPRINT_TOOL_ROWS.reset(token)
    result = raised.value.result

    assert result["_meta"]["status"] == "skipped"
    assert result["_meta"]["reason"] == "resource_discovery_search_exhausted"
    assert result["clio_runtime"]["terminal"] is True
    assert result["clio_runtime"]["workflow_state"]["resource_discovery"]["status"] == "search_exhausted"
    assert rows[-1]["skipped"] is True


def test_recording_blueprint_tool_skips_duplicate_station_search_before_exhaustion() -> None:
    def fail_search(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"duplicate ndp search should be short-circuited: {kwargs}")

    tool = dspy.Tool(
        func=fail_search,
        name="ndp_search_datasets",
        desc="Search NDP datasets",
        args={"resource_name": {"type": "string"}},
    )
    rows: list[dict[str, Any]] = [
        {
            "name": "clio_prior_workflow_state",
            "args": {},
            "ok": True,
            "result": {},
            "workflow_state": {
                "station_catalog": {
                    "status": "ranked_metadata_only",
                    "stations": [
                        {"station": "UCSF", "distance_km": 3.444},
                        {"station": "SBRB", "distance_km": 9.325},
                        {"station": "SBRU", "distance_km": 9.325},
                    ],
                },
                "resource_discovery": {"status": "search_required"},
            },
            "telemetry_source": "blueprint_react_context_seed",
        },
        {
            "name": "ndp_search_datasets",
            "args": {"resource_name": "UCSF", "resource_format": "CSV", "server": "global"},
            "result": {
                "datasets": [],
                "count": 0,
                "total_found": 0,
                "search_coverage": {
                    "domain": "earthscope_gnss",
                    "status": "covered",
                    "resource_name": "UCSF",
                    "resource_format": "CSV",
                    "station_code": "UCSF",
                    "station_resource_search": True,
                },
                "_meta": {"tool": "search_datasets", "status": "success"},
            },
        },
    ]
    token = _ACTIVE_BLUEPRINT_TOOL_ROWS.set(rows)
    try:
        wrapped = _recording_blueprint_tool(tool)
        result = wrapped(resource_name="UCSF", resource_format="CSV", server="global")
    finally:
        _ACTIVE_BLUEPRINT_TOOL_ROWS.reset(token)

    assert result["_meta"] == {
        "tool": "search_datasets",
        "status": "skipped",
        "reason": "duplicate_station_resource_search",
    }
    assert result["clio_runtime"]["terminal"] is False
    assert "SBRB, SBRU" in result["clio_runtime"]["next_action"]
    assert rows[-1]["skipped"] is True


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_station_catalog_prompt_keeps_resolver_acquisition_boundary(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "earthscope_station_catalog.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    tool_block = prompt.split("---", 2)[1]

    assert "station metadata ranking, not station time-series acquisition" in prompt
    assert "It has no NDP search or staging tools" in prompt
    assert "  - ndp_filter_earthscope_station_catalog" in tool_block
    assert "  - ndp_search_datasets" not in tool_block
    assert "  - ndp_stage_resource" not in tool_block
    assert "Do not call `ndp_stage_resource` for a station-specific time-series CSV" in prompt
    assert "do not call `ndp_search_datasets` to search station-specific resources by station ID" in prompt
    assert "The `ndp_resource_resolver` expert owns station-specific resource search" in prompt
    assert "resource_discovery.station_resource_queries" in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_analysis_prompt_forbids_rows_scanned_cadence_inference(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "gnss_timeseries_analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "Never convert `rows_scanned` into duration, cadence, or a sampling rate" in normalized_prompt
    assert "`rows_scanned`, `rows_examined`, and file size are profiler coverage signals" in normalized_prompt
    assert "Treat `numeric_summary_rows` or" in prompt
    assert "Do not infer a \"30-day record\" from `.30`" in normalized_prompt
    assert "visible sample rows suggest that local spacing" in normalized_prompt
    assert "do not generalize" in prompt
    assert "file-wide `Hz`, days-long duration, or sampling-rate claim" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_synthesis_prompt_filters_scan_limited_cadence_claims(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "synthesis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "Never convert `rows_scanned`, `rows_examined`, `rows_profiled`" in prompt
    assert "scan-limited profile row count is coverage evidence only" in prompt
    assert "Treat numeric summaries as covering" in prompt
    assert "Do not write `Hz`, \"hours\", \"days\", \"duration\", \"complete\"" in normalized_prompt
    assert "Do not infer a \"30-day record\" from `.30`" in normalized_prompt
    assert "unqualified \"high suitability\"" in normalized_prompt
    assert "omit that inference and keep only the grounded facts" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_station_network_prompt_preserves_uncertainty_units(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "station_network_analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "Values such as `0.033 m` are centimeter-scale" in prompt
    assert "not sub-centimeter" in prompt
    assert "Do not call uncertainty \"sub-cm\" unless the" in prompt
    assert "If the evidence is scan-limited" in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_station_network_prompt_forbids_scan_limited_record_claims(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "station_network_analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "Do not infer cadence, duration, complete coverage, or gap-free behavior" in prompt
    assert "`rows_scanned`, `rows_examined`, `rows_profiled`" in prompt
    assert "resource names, or adjacent sample rows" in prompt
    assert '"30-day record", "30 s cadence"' in prompt
    assert '"30-day record", "30 s cadence", "two-week record"' in normalized_prompt
    assert '"full record", "continuous", "no large data gaps"' in normalized_prompt
    assert "full-file cadence/duration/gap quality was not verified" in normalized_prompt
    assert "Prefer wording such as \"preliminary station/resource" in normalized_prompt
    assert "Treat `qChannel` as an opaque numeric flag" in prompt
    assert "`missing_values_scope=profiled_rows`" in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_analysis_prompt_filters_child_scan_limited_record_claims(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "audit child summaries for unsupported" in prompt
    assert '"30-day' in prompt
    assert '"30 s cadence", "two-week record"' in prompt
    assert '"full record", "continuous", "no large data gaps"' in normalized_prompt
    assert '"high' in prompt and '"excellent coverage"' in prompt
    assert "`rows_scanned`/`rows_examined`" in prompt
    assert "`rows_profiled`/`numeric_summary_rows`" in prompt
    assert "omit that phrase from the returned" in normalized_prompt
    assert "full-file cadence/duration/gap quality was not verified" in normalized_prompt
    assert "Missing-value claims must cite `missing_values`" in prompt
    assert "`missing_values_scope=profiled_rows`" in prompt
    assert "Do not turn `qChannel` min/max/mean into" in prompt
    assert "Numeric uncertainty means alone are descriptive statistics" in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_analysis_keeps_event_context_optional(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "station_network_to_event_context" not in prompt
    assert "next_expert: seismic_event_catalog" not in prompt
    assert "Optional capability:" in prompt
    assert "Request `seismic_event_catalog` only when the user explicitly asks" in prompt
    assert "does not by itself require this child" in normalized_prompt
    assert "do not report event-catalog limitations as a mandatory result" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_event_catalog_prompt_returns_typed_blocker_not_no_events(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "seismic_event_catalog.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "return only an explicit capability gap" in normalized_prompt
    assert "EVENT_CATALOG_BLOCKER: no live event catalog tool available in this pack" in prompt
    assert (
        "Absence of a live event-catalog tool is not evidence that no events occurred"
        in normalized_prompt
    )
    assert "`event_catalog_capability.status=partial`" in prompt
    assert '"event_context"' in prompt
    assert '"status": "blocked"' in prompt
    assert '"verified_event_count": null' in prompt
    assert '"no_live_event_catalog_tool"' in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_geospatial_prompt_does_not_invent_named_source_provenance(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "geospatial.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert '`provenance="model_geographic_prior"`' in prompt
    assert "Do not cite USGS, EarthScope, UNAVCO, station catalogs" in prompt
    assert "unless a tool result or user input actually provided that source" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_resolver_prompt_uses_typed_station_resource_frontier(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "ndp_resource_resolver.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "`resource_discovery.station_resource_queries[*].preferred_calls`" in prompt
    assert '`resource_name="<station id>"`' in prompt
    assert (
        "Do not search station IDs in `search_terms` for this resolver step"
        in normalized_prompt
    )
    assert "grouped calls" in prompt
    assert "do not count as station-resource coverage" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_data_prompt_requires_staged_metadata_before_station_filter(
    blueprint_id: str,
) -> None:
    root = Path(__file__).resolve().parents[2] / "external" / "clio-agent-marketplace"
    data_prompt = (
        root / blueprint_id / "experts" / "data.md"
    ).read_text(encoding="utf-8")
    discovery_prompt = (
        root / blueprint_id / "experts" / "ndp_dataset_discovery.md"
    ).read_text(encoding="utf-8")
    station_prompt = (
        root / blueprint_id / "experts" / "earthscope_station_catalog.md"
    ).read_text(encoding="utf-8")
    normalized_data = " ".join(data_prompt.split())
    normalized_discovery = " ".join(discovery_prompt.split())
    normalized_station = " ".join(station_prompt.split())

    assert "discovery_metadata_requires_staging" in data_prompt
    assert "acquisition.metadata_path" in data_prompt
    assert "exists: true" in data_prompt
    assert "a guessed filename such as `earthscope_stations.csv` is not a staged path" in normalized_data
    assert "the next tool call must be `ndp_stage_resource`" in normalized_discovery
    assert "before station ranking can proceed" in normalized_discovery
    assert "Do not call `ndp_filter_earthscope_station_catalog` with a guessed relative filename" in normalized_station
    assert "exact local path returned by `ndp_stage_resource`" in normalized_station


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_final_prompts_guard_scan_limited_profile_scope(
    blueprint_id: str,
) -> None:
    root = Path(__file__).resolve().parents[2] / "external" / "clio-agent-marketplace"
    main_prompt = (root / blueprint_id / "experts" / "main.md").read_text(encoding="utf-8")
    synthesis_prompt = (
        root / blueprint_id / "experts" / "synthesis.md"
    ).read_text(encoding="utf-8")
    analysis_prompt = (
        root / blueprint_id / "experts" / "gnss_timeseries_analysis.md"
    ).read_text(encoding="utf-8")
    visualization_prompt = (
        root / blueprint_id / "experts" / "visualization.md"
    ).read_text(encoding="utf-8")
    combined = " ".join(
        "\n".join([main_prompt, synthesis_prompt, analysis_prompt, visualization_prompt]).split()
    )

    assert "rows_profiled`/`numeric_summary_rows" in combined
    assert "numeric_summary.time.min/max" in combined
    assert "only for `numeric_summary_rows`" in combined
    assert "Do not estimate total file row count from byte size" in combined
    assert '"30-s cadence"' in combined
    assert '"no missing values"' in combined
    assert '"low noise"' in combined
    assert "Plot success only proves" in combined
    assert "full-file cadence/duration/gap quality was not verified" in combined
    assert "missing_values_scope=profiled_rows" in combined
    assert "Do not interpret `qChannel` numeric values as decoded quality" in combined
    assert "Treat `qChannel` numeric summaries as opaque flag values" in combined
    assert "Uncertainty means are descriptive statistics" in combined
    assert "provenance=model_geographic_prior" in combined
    assert "do not cite USGS, UNAVCO" in combined
    assert "Named geographic provenance is allowed only when a tool result" in combined


def test_recording_blueprint_tool_skips_broad_csv_search_after_station_catalog() -> None:
    def fail_search(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"broad ndp search should be short-circuited: {kwargs}")

    tool = dspy.Tool(
        func=fail_search,
        name="ndp_search_datasets",
        desc="Search NDP datasets",
        args={"search_terms": {"type": "array"}},
    )
    rows: list[dict[str, Any]] = [
        {
            "name": "clio_prior_workflow_state",
            "args": {},
            "ok": True,
            "result": {},
            "workflow_state": {
                "station_catalog": {
                    "status": "ranked_metadata_only",
                    "stations": [
                        {"station": "UCSF", "distance_km": 3.444},
                        {"station": "SBRB", "distance_km": 9.325},
                    ],
                },
                "resource_discovery": {
                    "status": "search_required",
                    "searched_station_ids": ["UCSF"],
                },
            },
            "telemetry_source": "blueprint_react_context_seed",
        },
    ]
    token = _ACTIVE_BLUEPRINT_TOOL_ROWS.set(rows)
    try:
        wrapped = _recording_blueprint_tool(tool)
        result = wrapped(
            search_terms=["EarthScope", "GNSS", "CSV"],
            resource_format="CSV",
            server="global",
        )
    finally:
        _ACTIVE_BLUEPRINT_TOOL_ROWS.reset(token)

    assert result["_meta"] == {
        "tool": "search_datasets",
        "status": "skipped",
        "reason": "broad_station_resource_search_after_station_catalog",
    }
    assert result["clio_runtime"]["terminal"] is False
    assert "SBRB" in result["clio_runtime"]["next_action"]
    assert rows[-1]["skipped"] is True


def test_ndp_workflow_tool_interceptor_skips_duplicate_from_live_ledger() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=SimpleNamespace(get=lambda sid: SimpleNamespace(id=sid)),
            tool_call_ledger={
                "session-123": [
                    {
                        "name": "ndp_filter_earthscope_station_catalog",
                        "args": {
                            "filepath": "/workspace/.clio/artifacts/ndp-staging/earthscope.csv",
                            "latitude": 37.77,
                            "longitude": -122.42,
                            "radius_km": 75,
                        },
                        "result": {
                            "ok": True,
                            "path": "/workspace/.clio/artifacts/ndp-staging/earthscope.csv",
                            "center": {"latitude": 37.77, "longitude": -122.42},
                            "radius_km": 75,
                            "within_radius_count": 2,
                            "stations": [
                                {"station": "UCSF", "distance_km": 3.444},
                                {"station": "SBRB", "distance_km": 9.325},
                            ],
                            "resource_discovery": {"status": "search_required"},
                            "_meta": {
                                "tool": "filter_earthscope_station_catalog",
                                "status": "success",
                            },
                        },
                    },
                    {
                        "name": "ndp_search_datasets",
                        "args": {
                            "resource_name": "UCSF",
                            "resource_format": "CSV",
                            "server": "global",
                        },
                        "result": {
                            "datasets": [],
                            "count": 0,
                            "total_found": 0,
                            "search_coverage": {
                                "domain": "earthscope_gnss",
                                "status": "covered",
                                "resource_name": "UCSF",
                                "resource_format": "CSV",
                                "station_code": "UCSF",
                                "station_resource_search": True,
                            },
                            "_meta": {"tool": "search_datasets", "status": "success"},
                        },
                    },
                ]
            },
        )
    )
    interceptor = _make_ndp_workflow_tool_interceptor(app)

    with _tool_session_context("session-123"):
        result = interceptor(
            "ndp_search_datasets",
            {"resource_name": "UCSF", "resource_format": "CSV", "server": "global"},
        )

    assert result is not None
    assert result["_meta"] == {
        "tool": "search_datasets",
        "status": "skipped",
        "reason": "duplicate_station_resource_search",
    }
    assert result["clio_runtime"]["terminal"] is False
    assert "SBRB" in result["clio_runtime"]["next_action"]


def test_ndp_workflow_tool_interceptor_skips_duplicate_stage_from_live_ledger() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=SimpleNamespace(get=lambda sid: SimpleNamespace(id=sid)),
            tool_call_ledger={
                "session-123": [
                    {
                        "name": "ndp_stage_resource",
                        "args": {
                            "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                        },
                        "ok": True,
                        "result": {
                            "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                            "source_url": "https://example.test/raw_csv/MTA1.CI.LY_.30.csv",
                            "selected_resource_url": "https://example.test/raw_csv/MTA1.CI.LY_.30.csv",
                            "size_bytes": 50424246,
                            "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                            "dataset_name": "mta1-ci-ly-30",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "_meta": {"tool": "stage_resource", "status": "success"},
                        },
                    },
                ]
            },
        )
    )
    interceptor = _make_ndp_workflow_tool_interceptor(app)

    with _tool_session_context("session-123"):
        result = interceptor(
            "ndp_stage_resource",
            {
                "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "resource_name": "MTA1.CI.LY_.30.csv",
                "resource_index": 0,
            },
        )

    assert result is not None
    assert result["_meta"] == {
        "tool": "stage_resource",
        "status": "skipped",
        "reason": "duplicate_station_resource_stage",
        "cache_hit": True,
    }
    assert result["path"] == "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv"
    assert result["clio_runtime"]["workflow_state"]["acquisition"]["analysis_ready"] is True


def test_ndp_workflow_tool_interceptor_raises_terminal_in_active_blueprint_context() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=SimpleNamespace(get=lambda sid: SimpleNamespace(id=sid)),
            tool_call_ledger={
                "session-123": [
                    {
                        "name": "ndp_filter_earthscope_station_catalog",
                        "args": {
                            "filepath": "/workspace/.clio/artifacts/ndp-staging/earthscope.csv",
                            "latitude": 37.77,
                            "longitude": -122.42,
                            "radius_km": 75,
                        },
                        "result": {
                            "ok": True,
                            "path": "/workspace/.clio/artifacts/ndp-staging/earthscope.csv",
                            "center": {"latitude": 37.77, "longitude": -122.42},
                            "radius_km": 75,
                            "within_radius_count": 2,
                            "stations": [
                                {"station": "UCSF", "distance_km": 3.444},
                                {"station": "SBRB", "distance_km": 9.325},
                            ],
                            "resource_discovery": {"status": "search_required"},
                            "_meta": {
                                "tool": "filter_earthscope_station_catalog",
                                "status": "success",
                            },
                        },
                    },
                    *[
                        {
                            "name": "ndp_search_datasets",
                            "args": {
                                "resource_name": station,
                                "resource_format": "CSV",
                                "server": "global",
                            },
                            "result": {
                                "datasets": [],
                                "count": 0,
                                "total_found": 0,
                                "search_coverage": {
                                    "domain": "earthscope_gnss",
                                    "status": "covered",
                                    "resource_name": station,
                                    "resource_format": "CSV",
                                    "station_code": station,
                                    "station_resource_search": True,
                                },
                                "_meta": {"tool": "search_datasets", "status": "success"},
                            },
                        }
                        for station in ("UCSF", "SBRB")
                    ],
                ]
            },
        )
    )
    interceptor = _make_ndp_workflow_tool_interceptor(app)

    with _tool_session_context("session-123"):
        result = interceptor(
            "ndp_search_datasets",
            {"resource_name": "UCSF", "resource_format": "CSV", "server": "global"},
        )

    assert result is not None
    assert result["_meta"]["reason"] == "resource_discovery_search_exhausted"
    assert result["clio_runtime"]["terminal"] is True

    active_rows: list[dict[str, Any]] = []
    token = _ACTIVE_BLUEPRINT_TOOL_ROWS.set(active_rows)
    try:
        with _tool_session_context("session-123"):
            with pytest.raises(_BlueprintTerminalWorkflowState) as raised:
                interceptor(
                    "ndp_search_datasets",
                    {"resource_name": "UCSF", "resource_format": "CSV", "server": "global"},
                )
    finally:
        _ACTIVE_BLUEPRINT_TOOL_ROWS.reset(token)

    assert raised.value.result["_meta"]["reason"] == "resource_discovery_search_exhausted"
    assert active_rows[-1]["telemetry_source"] == "blueprint_react_tool_interceptor"
    assert active_rows[-1]["result"]["clio_runtime"]["terminal"] is True


def test_generated_child_expert_tool_merges_session_tool_ledger_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(tool_call_ledger={"session-123": []})
    )
    parent = AgentDef(id="root", source="expert_pack", title="Root")
    child = AgentDef(id="catalog", source="expert_pack", title="Catalog", parent_id="root")

    def fake_run_dynamic_agent_compat(runner, base_agent, agent_def, question, session_id, cancel_requested):
        app.state.tool_call_ledger[session_id].extend(
            [
                {
                    "name": "ndp_stage_resource",
                    "args": {
                        "dataset_identifier": "earthscope-stations",
                        "resource_name": "earthscope_converted_data.csv",
                    },
                    "ok": True,
                    "result": {
                        "path": "/tmp/earthscope_converted_data.csv",
                        "dataset_id": "earthscope-stations",
                        "dataset_name": "earthscope_stations",
                        "resource_name": "earthscope_converted_data.csv",
                        "source_url": "https://example.test/earthscope_converted_data.csv",
                    },
                },
                {
                    "name": "ndp_filter_earthscope_station_catalog",
                    "args": {
                        "filepath": "/tmp/earthscope_converted_data.csv",
                        "latitude": 37.77,
                        "longitude": -122.42,
                        "radius_km": 75,
                    },
                    "ok": True,
                    "result": {
                        "_meta": {"tool": "filter_earthscope_station_catalog"},
                        "path": "/tmp/earthscope_converted_data.csv",
                        "center": {"latitude": 37.77, "longitude": -122.42},
                        "radius_km": 75,
                        "within_radius_count": 1,
                        "stations": [
                            {
                                "station": "UCSF",
                                "latitude": 37.76296967,
                                "longitude": -122.45815583,
                                "distance_km": 3.444,
                            }
                        ],
                    },
                },
            ]
        )
        return SimpleNamespace(answer="metadata catalog filtered")

    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "child-runner")
    monkeypatch.setattr("clio_agent.gact.app._run_dynamic_agent_compat", fake_run_dynamic_agent_compat)

    token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    try:
        with _gact_app_context(app):
            tool = _build_child_expert_tool(SimpleNamespace(), parent, child)
            payload = json.loads(tool(question="filter station catalog"))
    finally:
        _ACTIVE_GACT_SESSION_ID.reset(token)

    assert [row["name"] for row in payload["tools_called"]] == [
        "ndp_stage_resource",
        "ndp_filter_earthscope_station_catalog",
    ]
    state = payload["workflow_state"]
    assert state["acquisition"]["status"] == "metadata_only"
    assert state["acquisition"]["analysis_ready"] is False
    assert state["station_catalog"]["status"] == "ranked_metadata_only"
    assert state["resource_discovery"]["status"] == "search_required"
    assert "CLIO durable typed workflow state" in payload["output_summary"]


def test_generated_child_expert_tool_enforces_contract_order_from_typed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    parent = AgentDef(
        id="data",
        source="expert_pack",
        title="Data",
        parameters={
            "enforce_child_contract_order": True,
            "continuation_contracts": [
                {
                    "id": "start_with_discovery",
                    "next_expert": "ndp_dataset_discovery",
                    "next_action": "discover resources",
                },
                {
                    "id": "discovery_to_station",
                    "when_state": {"catalog.status": "candidates_found"},
                    "match": "all",
                    "next_expert": "earthscope_station_catalog",
                    "next_action": "rank stations",
                },
            ],
        },
    )
    discovery = AgentDef(
        id="ndp_dataset_discovery",
        source="expert_pack",
        title="Discovery",
        parent_id="data",
    )
    station = AgentDef(
        id="earthscope_station_catalog",
        source="expert_pack",
        title="Station Catalog",
        parent_id="data",
    )
    rows = [parent, discovery, station]

    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_active_agent_blueprint_rows",
        lambda app, session_id="": rows,
    )
    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "runner")

    def fake_run_dynamic_agent_compat(
        runner: Any,
        base_agent: Any,
        agent_def: AgentDef,
        question: str,
        session_id: str,
        cancel_requested: Any,
    ) -> Any:
        if agent_def.id == "ndp_dataset_discovery":
            return SimpleNamespace(
                answer='{"workflow_state":{"catalog":{"status":"candidates_found"}}}'
            )
        return SimpleNamespace(answer='{"workflow_state":{"station_catalog":{"status":"ranked"}}}')

    monkeypatch.setattr(
        "clio_agent.gact.app._run_dynamic_agent_compat",
        fake_run_dynamic_agent_compat,
    )

    session_token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    completions_token = _ACTIVE_CHILD_TOOL_COMPLETIONS.set([])
    try:
        with _gact_app_context(app):
            discovery_tool = _build_child_expert_tool(SimpleNamespace(), parent, discovery)
            station_tool = _build_child_expert_tool(SimpleNamespace(), parent, station)
            early_station_payload = json.loads(station_tool(question="rank stations too early"))
            discovery_payload = json.loads(discovery_tool(question="discover"))
            station_payload = json.loads(station_tool(question="rank"))
    finally:
        _ACTIVE_CHILD_TOOL_COMPLETIONS.reset(completions_token)
        _ACTIVE_GACT_SESSION_ID.reset(session_token)

    assert discovery_payload["agent_id"] == "ndp_dataset_discovery"
    assert early_station_payload["status"] == "skipped"
    assert early_station_payload["skip_reason"] == "child_contract_precondition_unmet"
    assert early_station_payload["allowed_next_children"] == ["ndp_dataset_discovery"]
    assert station_payload["agent_id"] == "earthscope_station_catalog"


def test_completed_row_contract_evidence_includes_structured_workflow_state() -> None:
    parent = AgentDef(
        id="ndp_dataset_discovery",
        source="expert_pack",
        title="Discovery",
        parameters={
            "continuation_contracts": [
                {
                    "id": "discovery_to_station_catalog",
                    "when_state": {
                        "catalog.status": {
                            "in": [
                                "candidates_found",
                                "metadata_found",
                                "partial",
                                "search_incomplete",
                            ]
                        },
                        "station_catalog.status": {"exists": False},
                    },
                    "match": "all",
                    "next_expert": "earthscope_station_catalog",
                    "next_action": "rank stations",
                }
            ]
        },
    )
    row = {
        "agent_id": "earthscope_station_catalog",
        "status": "completed",
        "stage": "delegate.completed",
        "output_summary": "compact child text without enough state",
        "workflow_state": {
            "catalog": {"status": "metadata_found"},
            "station_catalog": {"status": "ranked_metadata_only"},
        },
    }

    evidence = _completed_row_contract_evidence(row)
    rows = _continuation_contract_handoffs(
        parent,
        source_text="",
        answer_text="",
        completed_outputs=[evidence],
        declared_child_ids={"earthscope_station_catalog"},
        completed_child_ids=set(),
    )

    assert "ranked_metadata_only" in evidence
    assert rows == []


def test_completed_row_contract_evidence_uses_return_summary_over_local_trace_text() -> None:
    row = {
        "agent_id": "geospatial",
        "status": "completed",
        "stage": "delegate.completed",
        "local_output_summary": (
            "local answer: coordinates resolved, downstream artifact not available here"
        ),
        "output_summary": "bubbled parent evidence: staged station CSV is ready",
        "return_output_summary": "return evidence: analysis_ready=true",
        "workflow_state": {"acquisition": {"status": "staged", "analysis_ready": True}},
        "local_workflow_state": {"geospatial": {"status": "resolved"}},
    }

    evidence = _completed_row_contract_evidence(row)

    assert "return evidence: analysis_ready=true" in evidence
    assert "bubbled parent evidence" not in evidence
    assert "local answer" not in evidence
    assert '"analysis_ready": true' in evidence


def test_provenance_wrapped_station_csv_state_is_reconciled_as_workflow_state() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "event_catalog": {"status": "recorded"},
                        "selected_station": {
                            "csv_path": "/workspace/.clio/artifacts/WWMT.CI.LY_.40.csv",
                            "station_id": None,
                        },
                        "provenance": {
                            "acquisition": {
                                "status": "staged",
                                "analysis_ready": True,
                                "local_path": "/workspace/.clio/artifacts/WWMT.CI.LY_.40.csv",
                                "source_url": "https://example.test/WWMT.CI.LY_.40.csv",
                                "required_columns": ["time", "east", "north", "up"],
                            },
                            "resource_candidate": {
                                "status": "selected",
                                "resource_name": "WWMT.CI.LY_.40.csv",
                                "resource_url": "https://example.test/WWMT.CI.LY_.40.csv",
                            },
                            "catalog": {"status": "metadata_found"},
                        },
                    }
                }
            )
        ]
    )

    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is False
    assert "geographic provenance" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["station_id"] == "WWMT"
    assert state["resource_candidate"]["geographically_grounded"] is False
    assert state["selected_station"]["station_id"] == "WWMT"


def test_generated_child_expert_tool_enforces_contract_order_from_child_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    parent = AgentDef(
        id="data",
        source="expert_pack",
        title="Data",
        parameters={
            "enforce_child_contract_order": True,
            "continuation_contracts": [
                {
                    "id": "start_with_discovery",
                    "next_expert": "ndp_dataset_discovery",
                    "next_action": "discover resources",
                },
                {
                    "id": "discovery_to_station",
                    "when_child_completed": "ndp_dataset_discovery",
                    "next_expert": "earthscope_station_catalog",
                    "next_action": "rank stations",
                },
            ],
        },
    )
    discovery = AgentDef(
        id="ndp_dataset_discovery",
        source="expert_pack",
        title="Discovery",
        parent_id="data",
    )
    station = AgentDef(
        id="earthscope_station_catalog",
        source="expert_pack",
        title="Station Catalog",
        parent_id="data",
    )
    rows = [parent, discovery, station]

    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_active_agent_blueprint_rows",
        lambda app, session_id="": rows,
    )
    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "runner")
    monkeypatch.setattr(
        "clio_agent.gact.app._run_dynamic_agent_compat",
        lambda runner, base_agent, agent_def, question, session_id, cancel_requested: SimpleNamespace(
            answer="completed without structured state"
        ),
    )

    session_token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    completions_token = _ACTIVE_CHILD_TOOL_COMPLETIONS.set([])
    try:
        with _gact_app_context(app):
            discovery_tool = _build_child_expert_tool(SimpleNamespace(), parent, discovery)
            station_tool = _build_child_expert_tool(SimpleNamespace(), parent, station)
            early_station_payload = json.loads(station_tool(question="rank stations too early"))
            discovery_tool(question="discover")
            station_payload = json.loads(station_tool(question="rank"))
    finally:
        _ACTIVE_CHILD_TOOL_COMPLETIONS.reset(completions_token)
        _ACTIVE_GACT_SESSION_ID.reset(session_token)

    assert early_station_payload["status"] == "skipped"
    assert early_station_payload["skip_reason"] == "child_contract_precondition_unmet"
    assert early_station_payload["allowed_next_children"] == ["ndp_dataset_discovery"]
    assert station_payload["agent_id"] == "earthscope_station_catalog"


def test_generated_child_expert_tool_blocks_declared_child_when_contract_state_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    parent = AgentDef(
        id="ndp_dataset_discovery",
        source="expert_pack",
        title="Discovery",
        parameters={
            "enforce_child_contract_order": True,
            "continuation_contracts": [
                {
                    "id": "discovery_to_station",
                    "when_state": {"catalog.status": "candidates_found"},
                    "match": "all",
                    "next_expert": "earthscope_station_catalog",
                    "next_action": "rank stations",
                }
            ],
        },
    )
    station = AgentDef(
        id="earthscope_station_catalog",
        source="expert_pack",
        title="Station Catalog",
        parent_id="ndp_dataset_discovery",
    )
    rows = [parent, station]

    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_active_agent_blueprint_rows",
        lambda app, session_id="": rows,
    )
    calls: list[str] = []

    def fake_run_dynamic_agent_compat(
        runner: Any,
        base_agent: Any,
        agent_def: AgentDef,
        question: str,
        session_id: str,
        cancel_requested: Any,
    ) -> Any:
        calls.append(agent_def.id)
        return SimpleNamespace(
            answer='{"workflow_state":{"station_catalog":{"status":"ranked_metadata_only"}}}'
        )

    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "runner")
    monkeypatch.setattr("clio_agent.gact.app._run_dynamic_agent_compat", fake_run_dynamic_agent_compat)

    session_token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    completions_token = _ACTIVE_CHILD_TOOL_COMPLETIONS.set([])
    try:
        with _gact_app_context(app):
            station_tool = _build_child_expert_tool(SimpleNamespace(), parent, station)
            payload = json.loads(station_tool(question="rank stations from current NDP evidence"))
    finally:
        _ACTIVE_CHILD_TOOL_COMPLETIONS.reset(completions_token)
        _ACTIVE_GACT_SESSION_ID.reset(session_token)

    assert payload["agent_id"] == "earthscope_station_catalog"
    assert payload["status"] == "skipped"
    assert payload["skip_reason"] == "child_contract_precondition_unmet"
    assert payload["allowed_next_children"] == []
    assert "not currently authorized" in payload["output_summary"]
    assert calls == []


def test_generated_child_expert_tool_uses_app_state_completion_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    parent = AgentDef(
        id="ndp_dataset_discovery",
        source="expert_pack",
        title="Discovery",
        parameters={
            "enforce_child_contract_order": True,
            "bubble_child_evidence_on_completion": True,
            "continuation_contracts": [
                {
                    "id": "discovery_to_station",
                    "when_state": {"catalog.status": "candidates_found"},
                    "next_expert": "earthscope_station_catalog",
                }
            ],
        },
    )
    station = AgentDef(
        id="earthscope_station_catalog",
        source="expert_pack",
        title="Station Catalog",
        parent_id="ndp_dataset_discovery",
    )
    rows = [parent, station]

    app.state.child_tool_completion_contexts = {
        ("session-123", "ndp_dataset_discovery"): [
            {
                "agent_id": "earthscope_station_catalog",
                "output_summary": (
                    '{"workflow_state":{"station_catalog":{"status":"ranked"}}}'
                ),
            }
        ]
    }
    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_active_agent_blueprint_rows",
        lambda app, session_id="": rows,
    )
    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "runner")

    session_token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    completions_token = _ACTIVE_CHILD_TOOL_COMPLETIONS.set(None)
    try:
        with _gact_app_context(app):
            station_tool = _build_child_expert_tool(SimpleNamespace(), parent, station)
            payload = json.loads(station_tool(question="repeat station catalog"))
    finally:
        _ACTIVE_CHILD_TOOL_COMPLETIONS.reset(completions_token)
        _ACTIVE_GACT_SESSION_ID.reset(session_token)

    assert payload["agent_id"] == "earthscope_station_catalog"
    assert payload["repeat_ignored"] is True
    assert "station_catalog" in payload["output_summary"]


def test_generated_child_expert_tool_blocks_turn_level_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    parent = AgentDef(
        id="ndp_dataset_discovery",
        source="expert_pack",
        title="Discovery",
        parameters={"enforce_child_contract_order": True},
    )
    station = AgentDef(
        id="earthscope_station_catalog",
        source="expert_pack",
        title="Station Catalog",
        parent_id="ndp_dataset_discovery",
    )
    app.state.child_tool_completed_by_turn = {
        ("session-123", "", "ndp_dataset_discovery", "earthscope_station_catalog")
    }
    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_active_agent_blueprint_rows",
        lambda app, session_id="": [parent, station],
    )

    session_token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    completions_token = _ACTIVE_CHILD_TOOL_COMPLETIONS.set(None)
    try:
        with _gact_app_context(app):
            station_tool = _build_child_expert_tool(SimpleNamespace(), parent, station)
            payload = json.loads(station_tool(question="repeat station catalog"))
    finally:
        _ACTIVE_CHILD_TOOL_COMPLETIONS.reset(completions_token)
        _ACTIVE_GACT_SESSION_ID.reset(session_token)

    assert payload["agent_id"] == "earthscope_station_catalog"
    assert payload["repeat_ignored"] is True
    assert "already completed" in payload["output_summary"]


def test_resume_prompt_seeds_multiline_child_tool_state() -> None:
    prompt = """
Original user request:
Explore a region.

Returned child expert results for parent expert 'main':
- geospatial: status=completed; result={
  "workflow_state": {
    "geospatial": {
      "status": "resolved"
    }
  }
}

Continue from these results.
"""

    completions = _seed_child_tool_completions_from_resume_prompt(
        prompt,
        {"geospatial", "data"},
    )

    assert completions == [
        {
            "agent_id": "geospatial",
            "output_summary": '{\n  "workflow_state": {\n    "geospatial": {\n      "status": "resolved"\n    }\n  }\n}',
        }
    ]
    assert _workflow_state_from_outputs([completions[0]["output_summary"]]) == {
        "geospatial": {"status": "resolved"}
    }


def test_generated_child_expert_tool_emits_semantic_delegation_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []

    class FakeSink:
        def emit(self, event: Any) -> dict[str, Any]:
            row = event.to_dict()
            emitted.append(row)
            return row

    app = SimpleNamespace(
        state=SimpleNamespace(
            semantic_event_sink=FakeSink(),
            sessions=SimpleNamespace(get=lambda sid: SimpleNamespace(workspace_id="ws_default")),
            semantic_trace_detail_level="semantic",
        )
    )
    parent = AgentDef(
        id="root",
        source="expert_pack",
        title="Root",
        metadata={"agent_blueprint_id": "genomics"},
    )
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")

    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "child-runner")
    monkeypatch.setattr(
        "clio_agent.gact.app._run_dynamic_agent_compat",
        lambda runner, base_agent, agent_def, question, session_id, cancel_requested: SimpleNamespace(
            answer="delegated answer",
            evidence="support",
        ),
    )

    token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    try:
        with _gact_app_context(app):
            payload = json.loads(
                _build_child_expert_tool(SimpleNamespace(), parent, child)(question="inspect")
            )
    finally:
        _ACTIVE_GACT_SESSION_ID.reset(token)

    assert payload["status"] == "completed"
    assert [row["event_type"] for row in emitted] == [
        "blueprint.delegation.started",
        "blueprint.delegation.completed",
    ]
    assert emitted[0]["blueprint"]["agent_blueprint_id"] == "genomics"
    assert emitted[1]["payload"]["return_payload"] == "compact_result"


def test_blueprint_fanout_tool_enforces_bounds_and_emits_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []

    class FakeSink:
        def emit(self, event: Any) -> dict[str, Any]:
            row = event.to_dict()
            emitted.append(row)
            return row

    app = SimpleNamespace(
        state=SimpleNamespace(
            semantic_event_sink=FakeSink(),
            sessions=SimpleNamespace(get=lambda sid: SimpleNamespace(workspace_id="ws_default")),
            semantic_trace_detail_level="semantic",
        )
    )
    parent = AgentDef(
        id="root",
        source="expert_pack",
        title="Root",
        fanout={"enabled": True, "max_workers": 2},
        metadata={"agent_blueprint_id": "genomics"},
    )
    children = [
        AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root"),
        AgentDef(id="visualization", source="expert_pack", title="Visualization", parent_id="root"),
        AgentDef(id="quality", source="expert_pack", title="Quality", parent_id="root"),
    ]
    calls: list[str] = []

    def fake_run_dynamic_agent_compat(runner, base_agent, agent_def, question, session_id, cancel_requested):
        calls.append(agent_def.id)
        return SimpleNamespace(answer=f"{agent_def.id} compact evidence", evidence=f"{agent_def.id}:evidence")

    monkeypatch.setattr("clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "child-runner")
    monkeypatch.setattr("clio_agent.gact.app._run_dynamic_agent_compat", fake_run_dynamic_agent_compat)

    token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    try:
        with _gact_app_context(app):
            tool = _build_fanout_tool(SimpleNamespace(), parent, children)
            payload = json.loads(tool(question="inspect", child_ids="analysis,visualization,quality"))
    finally:
        _ACTIVE_GACT_SESSION_ID.reset(token)

    assert calls == ["analysis", "visualization"]
    assert payload["status"] == "completed"
    assert payload["executed_child_agent_ids"] == ["analysis", "visualization"]
    assert payload["skipped_child_agent_ids"] == ["quality"]
    assert [row["event_type"] for row in emitted] == [
        "blueprint.fanout.started",
        "blueprint.fanout.completed",
    ]
    assert emitted[0]["payload"]["skipped_child_agent_ids"] == ["quality"]
    assert emitted[1]["payload"]["result_count"] == 2


def test_blueprint_fanout_tool_rejects_undeclared_children() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            semantic_event_sink=None,
            sessions=SimpleNamespace(get=lambda sid: SimpleNamespace(workspace_id="ws_default")),
        )
    )
    parent = AgentDef(
        id="root",
        source="expert_pack",
        title="Root",
        fanout={"enabled": True, "max_workers": 2},
    )
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")

    token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    try:
        with _gact_app_context(app):
            tool = _build_fanout_tool(SimpleNamespace(), parent, [child])
            with pytest.raises(RuntimeError, match="undeclared child"):
                tool(question="inspect", child_ids='["analysis", "missing"]')
    finally:
        _ACTIVE_GACT_SESSION_ID.reset(token)


def test_dynamic_child_expert_tools_adds_fanout_only_when_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    parent = AgentDef(
        id="root",
        source="expert_pack",
        title="Root",
        fanout={"enabled": True, "max_workers": 2},
    )
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")
    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_active_agent_blueprint_rows",
        lambda app, session_id="": [parent, child],
    )

    token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    try:
        with _gact_app_context(app):
            tools = _dynamic_child_expert_tools(SimpleNamespace(), parent)
    finally:
        _ACTIVE_GACT_SESSION_ID.reset(token)

    assert [tool.name for tool in tools] == ["delegate_to_analysis", "fanout_to_children"]


def test_prediction_structured_metadata_omits_empty_values() -> None:
    result = SimpleNamespace(
        workflow_state={"acquisition": {"status": "staged"}},
        evidence="evidence rows",
        artifacts="",
        errors=None,
        delegation='{"next":"root"}',
    )

    assert _prediction_structured_metadata(result) == {
        "workflow_state": {"acquisition": {"status": "staged"}},
        "evidence": "evidence rows",
        "delegation": '{"next":"root"}',
    }


def test_prediction_workflow_state_output_is_parent_visible() -> None:
    output = _append_prediction_workflow_state(
        "metadata staged",
        SimpleNamespace(
            workflow_state={
                "acquisition": {
                    "status": "metadata_only",
                    "analysis_ready": False,
                }
            }
        ),
    )

    state = _workflow_state_from_outputs([output])

    assert state["acquisition"]["status"] == "metadata_only"
    assert state["acquisition"]["analysis_ready"] is False


def test_fallback_answer_from_delegation_uses_latest_completed_parent_resume() -> None:
    assert (
        _fallback_answer_from_delegation(
            [
                {"stage": "parent.resumed", "status": "completed", "output_summary": "first"},
                {"stage": "delegate.completed", "status": "completed", "output_summary": "child"},
                {"stage": "parent.resumed", "status": "failed", "output_summary": "bad"},
                {"stage": "parent.resumed", "status": "completed", "output_summary": "final"},
            ]
        )
        == "final"
    )


def test_delegation_placeholder_answers_are_not_final_results() -> None:
    assert _dynamic_answer_is_delegation_placeholder(
        "Executing returned delegation continuation contract."
    )
    assert _dynamic_answer_is_delegation_placeholder(
        "Proceeding to the visual confirmation step."
    )
    assert _dynamic_answer_is_delegation_placeholder(
        "The next step is to run the outlier analysis."
    )
    assert _dynamic_answer_is_delegation_placeholder(
        "gnss_timeseries_analysis returned compact evidence to main"
    )
    assert not _dynamic_answer_is_delegation_placeholder(
        "The conversion is safe for downstream visualization with skipped caveats."
    )
    assert _fallback_answer_from_delegation([{"stage": "delegate.completed", "output_summary": "child"}]) == ""


def test_native_domain_expert_modules_are_not_runtime_importable(tmp_path: Path) -> None:
    retired_modules = [
        "clio_agent.experts.data_expert",
        "clio_agent.experts.analysis_expert",
        "clio_agent.experts.visualization_expert",
        "clio_agent.experts.ndp_expert",
        "clio_agent.experts.sac_format_expert",
    ]

    for module_name in retired_modules:
        assert importlib.util.find_spec(module_name) is None

    agent = ClioAgent(data_dir=str(tmp_path / "clio"))
    try:
        for attr in (
            "data_expert",
            "analysis_expert",
            "visualization_expert",
            "ndp_catalog_expert",
            "sac_format_expert",
        ):
            assert not hasattr(agent, attr)
        assert not {"data", "analysis", "visualization", "ndp_catalog", "sac_format"} & set(
            agent.registry.list_agents()
        )
    finally:
        agent.shutdown()


def test_validate_agent_blueprint_markdown_root(tmp_path: Path) -> None:
    root = tmp_path / "genomics"
    _write_blueprint(root)

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is True
    assert body["agent_blueprint"]["id"] == "genomics"
    rows = {row["id"]: row for row in body["agents"]}
    assert rows["root"]["tier"] == 1
    assert rows["variant"]["parent_id"] == "root"
    assert rows["variant"]["tools"] == ["memory_search_sessions"]


def test_agent_blueprint_activation_replaces_default_agent_graph(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "genomics", "workspace_id": wid},
        ).json()["id"]
        activated = client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "genomics"},
        )
        assert activated.status_code == 200, activated.text
        agents = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }

    assert set(agents) == {"root", "variant"}
    assert "data" not in agents
    assert agents["variant"]["metadata"]["agent_blueprint_id"] == "genomics"


def test_agent_blueprint_root_runtime_context_lists_declared_children(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(blueprint)
    blueprint.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Genomics Root
tier: 1
prompt_id: clio.main.planner
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "genomics", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "genomics"},
        ).status_code == 200
        root = next(
            AgentDef(**row)
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
            if row["id"] == "root"
        )

    context = _runtime_dynamic_agent_children_context(app, root, session_id=sid)

    assert "Declared child experts" in context
    assert "variant: Variant Expert" in context
    assert "memory_search_sessions" in context
    assert "expert_handoffs JSON array" in context
    assert "{{" not in root.system_prompt
    assert "- variant: Variant Expert" in root.system_prompt


def test_session_agent_overlay_is_session_local(tmp_path: Path) -> None:
    blueprint = tmp_path / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid_a = client.post("/v1/sessions", json={"title": "A"}).json()["id"]
        sid_b = client.post("/v1/sessions", json={"title": "B"}).json()["id"]
        for sid in (sid_a, sid_b):
            assert client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(blueprint)},
            ).status_code == 200
        saved = client.put(
            f"/v1/sessions/{sid_a}/agent-overlay",
            json={
                "agents": {
                    "variant": {
                        "title": "Session A Variant Expert",
                        "default_model": "gpt-5-mini",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text
        agent_a = client.get("/v1/agents/variant", params={"session_id": sid_a}).json()
        agent_b = client.get("/v1/agents/variant", params={"session_id": sid_b}).json()

    assert agent_a["title"] == "Session A Variant Expert"
    assert agent_a["default_model"] == "gpt-5-mini"
    assert agent_a["metadata"]["agent_blueprint_overlay"]["status"] == "applied"
    assert agent_b["title"] == "Variant Expert"
    assert agent_b["default_model"] == ""


def test_session_agent_overlay_rejects_invalid_contracts(tmp_path: Path) -> None:
    blueprint = tmp_path / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "A"}).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"path": str(blueprint)},
        ).status_code == 200

        broken_parent = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"parent_id": "missing-parent"}}},
        )
        bad_tool = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"tools": ["definitely_missing_tool"]}}},
        )
        bad_provider = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"default_provider": "definitely_missing_provider"}}},
        )
        bad_prompt = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"prompt_id": "definitely.missing.prompt"}}},
        )
        unknown_agent = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"missing-agent": {"title": "Nope"}}},
        )
        overlay_state = client.get(f"/v1/sessions/{sid}/agent-overlay").json()

    assert broken_parent.status_code == 422
    assert "parent_id not found" in broken_parent.text
    assert bad_tool.status_code == 422
    assert "unknown tool" in bad_tool.text
    assert bad_provider.status_code == 422
    assert "provider not found" in bad_provider.text
    assert bad_prompt.status_code == 422
    assert "prompt not found" in bad_prompt.text
    assert unknown_agent.status_code == 422
    assert "unknown expert" in unknown_agent.text
    assert overlay_state["agent_overlay"] == {}
    assert overlay_state["validation"]["enabled"] is True


def test_session_agent_overlay_can_export_workspace_blueprint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source" / "genomics"
    _write_blueprint(source)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "A", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"path": str(source)},
        ).status_code == 200
        saved = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={
                "agents": {
                    "variant": {
                        "title": "Session A Variant Expert",
                        "system_prompt": "Review this workspace's variant evidence.",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text

        exported = client.post(
            f"/v1/sessions/{sid}/agent-overlay/export",
            json={
                "blueprint_id": "genomics-session-a",
                "title": "Genomics Session A",
                "workspace_id": wid,
            },
        )
        listed = client.get("/v1/agent-blueprints", params={"workspace_id": wid}).json()

    assert exported.status_code == 201, exported.text
    exported_root = workspace / ".clio" / "agent-blueprints" / "genomics-session-a"
    assert exported_root.joinpath("AGENT.md").exists()
    assert "Session A Variant Expert" in exported_root.joinpath("experts", "variant.md").read_text()
    assert "Variant Expert" in source.joinpath("experts", "variant.md").read_text()
    assert {row["id"] for row in exported.json()["agents"]} == {"root", "variant"}
    assert "genomics-session-a" in {row["id"] for row in listed["agent_blueprints"]}


def test_session_agent_overlay_prompt_provenance_reaches_prompts_and_turn_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    blueprint = tmp_path / "remote-data"
    _write_data_root_blueprint(blueprint)
    calls: list[dict[str, str]] = []

    async def no_stream(*args, **kwargs):
        return None

    def fake_blueprint_runner(base_agent, agent_def, question, session_id, cancel_requested=None):
        del base_agent, cancel_requested
        calls.append(
            {
                "agent_id": agent_def.id,
                "system_prompt": agent_def.system_prompt,
                "model": agent_def.default_model,
                "question": question,
                "session_id": session_id,
            }
        )
        return SimpleNamespace(
            answer="overlay provenance ok",
            selected_expert=agent_def.id,
            routing_rationale="session overlay",
            route_source="agent_blueprint",
            error_info=None,
    )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", no_stream)
    monkeypatch.delenv("CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS", raising=False)
    monkeypatch.setattr("clio_agent.gact.app._run_blueprint_dspy_agent", fake_blueprint_runner)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "overlay runtime"}).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"path": str(blueprint)},
        ).status_code == 200
        saved = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={
                "agents": {
                    "data": {
                        "system_prompt": "SESSION OVERLAY PROMPT.",
                        "default_model": "gpt-5-mini",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text
        prompts = client.get("/v1/prompts", params={"session_id": sid}).json()
        assistant = complete_turn(client, sid, "prove overlay provenance")

    overlay_sources = prompts["agent_overlay"]["agents"]
    assert overlay_sources == [
        {
            "agent_id": "data",
            "fields": ["default_model", "system_prompt"],
            "has_system_prompt": True,
            "prompt_id": "",
            "prompt_profile": "",
            "default_provider": "",
            "default_model": "gpt-5-mini",
            "source": "session_agent_overlay",
            "session_id": sid,
        }
    ]
    assert calls == [
        {
            "agent_id": "data",
            "system_prompt": "SESSION OVERLAY PROMPT.",
            "model": "gpt-5-mini",
            "question": "prove overlay provenance",
            "session_id": sid,
        }
    ]
    runtime = assistant["metadata"]["agent_runtime"]
    assert runtime["agent_blueprint"]["id"] == "remote-data"
    assert runtime["agent_overlay"]["status"] == "applied"
    assert runtime["agent_overlay"]["fields"] == ["default_model", "system_prompt"]
    assert runtime["prompt"]["source"] == "session_agent_overlay"


def test_agent_blueprint_install_from_local_marketplace(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        )
        assert installed.status_code == 201, installed.text
        listed = client.get("/v1/agent-blueprints", params={"workspace_id": wid}).json()

    ids = {row["id"] for row in listed["agent_blueprints"]}
    assert "genomics" in ids
    assert (workspace / ".clio" / "agent-blueprints" / "genomics" / ".clio-install.md").exists()


def test_agent_blueprint_marketplace_sources_persist_and_install_by_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        created = client.post(
            "/v1/agent-blueprints/sources",
            json={
                "source": str(marketplace),
                "name": "Local marketplace",
                "pinned_commit": "",
            },
        )
        assert created.status_code == 201, created.text
        source = created.json()["source"]
        source_id = source["id"]
        assert source["status"] == "ready"
        assert source["source_kind"] == "path"
        assert source["pinned_commit"] == ""
        assert [row["id"] for row in source["available_blueprints"]] == ["genomics"]

        listed = client.get("/v1/agent-blueprints/sources").json()
        assert [row["id"] for row in listed["sources"]] == [source_id]

        refreshed = client.post(f"/v1/agent-blueprints/sources/{source_id}/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["source"]["available_blueprints"][0]["id"] == "genomics"

        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={"source_id": source_id, "scope": "workspace", "workspace_id": wid},
        )
        assert installed.status_code == 201, installed.text

        deleted = client.delete(f"/v1/agent-blueprints/sources/{source_id}")
        assert deleted.status_code == 200, deleted.text
        assert client.get("/v1/agent-blueprints/sources").json()["sources"] == []

    assert (workspace / ".clio" / "agent-blueprints" / "genomics" / ".clio-install.md").exists()


def test_marketplace_install_supports_distinct_session_blueprints(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics-review", blueprint_id="genomics-review")
    _write_data_root_blueprint(
        marketplace / "materials-crystal-review",
        blueprint_id="materials-crystal-review",
    )
    workspace = tmp_path / "workspace"
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        install = client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        )
        assert install.status_code == 201, install.text
        installed = {row["id"] for row in install.json()["installed"]}
        assert installed == {"genomics-review", "materials-crystal-review"}

        sid_genomics = client.post(
            "/v1/sessions",
            json={"title": "genomics", "workspace_id": wid},
        ).json()["id"]
        sid_materials = client.post(
            "/v1/sessions",
            json={"title": "materials", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid_genomics}/agent-blueprint",
            json={"blueprint_id": "genomics-review"},
        ).status_code == 200
        assert client.post(
            f"/v1/sessions/{sid_materials}/agent-blueprint",
            json={"blueprint_id": "materials-crystal-review"},
        ).status_code == 200

        genomics = client.get(f"/v1/sessions/{sid_genomics}/agent-blueprint").json()
        materials = client.get(f"/v1/sessions/{sid_materials}/agent-blueprint").json()
        genomics_agents = client.get(
            "/v1/agents",
            params={"session_id": sid_genomics},
        ).json()["agents"]
        materials_agents = client.get(
            "/v1/agents",
            params={"session_id": sid_materials},
        ).json()["agents"]

    assert genomics["active_agent_blueprint_id"] == "genomics-review"
    assert materials["active_agent_blueprint_id"] == "materials-crystal-review"
    assert genomics["activation"]["active_agent_blueprint_source"] == str(marketplace)
    assert genomics["activation"]["active_agent_blueprint_source_kind"] == "path"
    assert genomics["activation"]["active_agent_blueprint_checksum"]
    assert genomics["activation"]["active_agent_blueprint_installed_at"]
    assert materials["activation"]["active_agent_blueprint_source"] == str(marketplace)
    assert materials["activation"]["active_agent_blueprint_checksum"]
    assert {row["id"] for row in genomics_agents} == {"root", "variant"}
    assert {row["id"] for row in materials_agents} == {"data"}


def test_agent_blueprint_install_from_git_marketplace_records_pinned_metadata(
    tmp_path: Path,
) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    subprocess.run(["git", "init"], cwd=marketplace, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=marketplace, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CLIO Test",
            "-c",
            "user.email=clio@example.invalid",
            "commit",
            "-m",
            "Add genomics blueprint",
        ],
        cwd=marketplace,
        check=True,
        stdout=subprocess.PIPE,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(marketplace), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={
                "source": marketplace.as_uri(),
                "scope": "workspace",
                "workspace_id": wid,
                "blueprint_id": "genomics",
            },
        )

    assert installed.status_code == 201, installed.text
    metadata = installed.json()["installed"][0]["install"]
    assert metadata["source_kind"] == "git"
    assert metadata["source"] == marketplace.as_uri()
    assert metadata["commit"] == commit
    assert metadata["checksum"]
    assert metadata["installed_at"]


def test_agent_blueprint_update_and_delete_installed_blueprint(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        assert client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        ).status_code == 201
        marketplace.joinpath("genomics", "experts", "variant.md").write_text(
            """---
id: variant
title: Updated Variant Expert
parent_id: root
tier: 2
---
Updated behavior.
""",
            encoding="utf-8",
        )
        updated = client.post(
            "/v1/agent-blueprints/genomics/update",
            json={"scope": "workspace", "workspace_id": wid},
        )
        assert updated.status_code == 200, updated.text
        assert "Updated Variant Expert" in (
            workspace / ".clio" / "agent-blueprints" / "genomics" / "experts" / "variant.md"
        ).read_text()
        deleted = client.delete(
            "/v1/agent-blueprints/genomics",
            params={"scope": "workspace", "workspace_id": wid},
        )
        assert deleted.status_code == 200, deleted.text

    assert not (workspace / ".clio" / "agent-blueprints" / "genomics").exists()


def test_active_agent_blueprint_drives_turn_runtime_and_overrides_builtin_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "remote-data"
    _write_data_root_blueprint(blueprint)
    calls: list[dict[str, str]] = []

    async def no_stream(*args, **kwargs):
        return None

    def fake_blueprint_runner(base_agent, agent_def, question, session_id, cancel_requested=None):
        del base_agent, cancel_requested
        calls.append(
            {
                "agent_id": agent_def.id,
                "title": agent_def.title,
                "system_prompt": agent_def.system_prompt,
                "question": question,
                "session_id": session_id,
            }
        )
        return SimpleNamespace(
            answer=f"runtime from {agent_def.id}",
            selected_expert=agent_def.id,
            routing_rationale="session blueprint",
            route_source="agent_blueprint",
            error_info=None,
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", no_stream)
    monkeypatch.delenv("CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS", raising=False)
    monkeypatch.setattr("clio_agent.gact.app._run_blueprint_dspy_agent", fake_blueprint_runner)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid_blueprint = client.post(
            "/v1/sessions",
            json={"title": "blueprint", "workspace_id": wid},
        ).json()["id"]
        sid_builtin = client.post(
            "/v1/sessions",
            json={"title": "builtin", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid_blueprint}/agent-blueprint",
            json={"blueprint_id": "remote-data"},
        ).status_code == 200
        agents_blueprint = client.get(
            "/v1/agents",
            params={"session_id": sid_blueprint},
        ).json()["agents"]
        agents_builtin = client.get(
            "/v1/agents",
            params={"session_id": sid_builtin},
        ).json()["agents"]
        assistant = complete_turn(client, sid_blueprint, "prove runtime")

    assert [row["id"] for row in agents_blueprint] == ["data"]
    assert any(row["id"] == "analysis" for row in agents_builtin)
    assert calls == [
        {
            "agent_id": "data",
            "title": "Remote Data Orchestrator",
            "system_prompt": "REMOTE BLUEPRINT ORCHESTRATOR MARKER.",
            "question": "prove runtime",
            "session_id": sid_blueprint,
        }
    ]
    assert assistant["metadata"]["agent_runtime"]["agent_id"] == "data"
    assert assistant["metadata"]["agent_runtime"]["source"] == "expert_pack"
    assert assistant["metadata"]["agent_runtime"]["pack"]["id"] == "remote-data"


def test_agent_blueprint_mcp_descriptor_installs_disabled(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    descriptor = body["mcp_descriptors"][0]
    assert descriptor["id"] == "earthscope"
    assert descriptor["enabled"] is False
    assert descriptor["status"] == "disabled"
    assert descriptor["transport"] == "stdio"


def test_agent_blueprint_mcp_tool_references_require_enablement(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - earthscope_query
---
Use the external EarthScope catalog.
""",
        encoding="utf-8",
    )
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)
    rows = {row["id"]: row for row in body["agents"]}

    assert body["enabled"] is False
    assert "MCP tool requires explicit enablement" in "\n".join(body["validation_errors"])
    assert rows["variant"]["enabled"] is False
    assert rows["variant"]["metadata"]["tool_diagnostics"][0]["tool"] == "earthscope_query"
    assert body["mcp_descriptors"][0]["tools"][0]["status"] == "disabled"


def test_agent_blueprint_validation_reports_unknown_tools(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - missing_external_tool
---
Use an undeclared external tool.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is False
    assert "unknown tool reference: missing_external_tool" in "\n".join(
        body["validation_errors"]
    )


def test_agent_blueprint_mcp_descriptor_requires_explicit_enablement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        rows_before = client.get("/v1/mcp/servers", params={"workspace_id": wid}).json()["servers"]
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid, "probe": False},
        )
        assert enabled.status_code == 200, enabled.text
        rows_after = client.get("/v1/mcp/servers", params={"workspace_id": wid}).json()["servers"]

    assert any(
        row.get("source") == "agent_blueprint" and row.get("enabled") is False
        for row in rows_before
    )
    assert enabled.json()["status"] == "enabled_pending_probe"
    assert any(row["id"] == "agent_blueprint_mcp_earth_earthscope" for row in rows_after)


def test_enabled_agent_blueprint_mcp_descriptor_exposes_declared_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid, "probe": False},
        )
        tools = client.get("/v1/tools").json()["tools"]
        detail = client.get("/v1/tools/earthscope_query").json()

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["tools_count"] == 1
    declared = next(row for row in tools if row["id"] == "earthscope_query")
    assert declared["source"] == "agent_blueprint_mcp_descriptor"
    assert declared["status"] == "enabled_pending_probe"
    assert declared["enabled"] is False
    assert detail["descriptor_id"] == "earthscope"


def test_enabled_agent_blueprint_mcp_descriptor_probes_and_calls_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        called_tool = ""

        def __init__(self, transport: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return [
                SimpleNamespace(
                    name="earthscope_query",
                    description="query EarthScope catalog",
                    inputSchema={"type": "object"},
                    outputSchema={"type": "object"},
                )
            ]

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            FakeClient.called_tool = name
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"{name}:{args['q']}")],
                isError=False,
            )

    import fastmcp
    import fastmcp.client.transports as transports

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(transports, "StdioTransport", lambda command, args: (command, args))

    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid},
        )
        call = client.post(
            "/v1/mcp/servers/agent_blueprint_mcp_earth_earthscope/call",
            json={"tool": "earthscope_query", "args": {"q": "ANMO"}},
        )
        tools = client.get("/v1/tools").json()["tools"]

    assert enabled.status_code == 200, enabled.text
    body = enabled.json()
    assert body["status"] == "ready"
    assert body["tools"][0]["enabled"] is True
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert call.status_code == 200, call.text
    assert FakeClient.called_tool == "earthscope_query"
    assert call.json()["content"] == [
        {"type": "text", "text": "earthscope_query:ANMO"}
    ]
    declared = next(row for row in tools if row["id"] == "earthscope_query")
    assert declared["enabled"] is True
    assert declared["status"] == "ready"


def test_enabled_agent_blueprint_mcp_tool_reenables_session_expert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, transport: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return [
                SimpleNamespace(
                    name="earthscope_query",
                    description="query EarthScope catalog",
                    inputSchema={"type": "object"},
                    outputSchema={"type": "object"},
                )
            ]

    import fastmcp
    import fastmcp.client.transports as transports

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(transports, "StdioTransport", lambda command, args: (command, args))

    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - earthscope_query
---
Use the external EarthScope catalog.
""",
        encoding="utf-8",
    )
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "earth", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "earth"},
        ).status_code == 200
        before = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid},
        )
        after = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }

    assert before["variant"]["enabled"] is False
    assert "MCP tool requires explicit enablement" in "\n".join(
        before["variant"]["validation_errors"]
    )
    assert enabled.status_code == 200, enabled.text
    assert after["variant"]["enabled"] is True
    assert after["variant"]["validation_errors"] == []
    assert "tool_diagnostics" not in after["variant"]["metadata"]


def test_dynamic_agent_tools_include_enabled_agent_blueprint_mcp_tool(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.external_mcp_servers = {
        "agent_blueprint_mcp_earth_earthscope": {
            "id": "agent_blueprint_mcp_earth_earthscope",
            "name": "EarthScope MCP",
            "status": "ready",
            "spec": {
                "transport": "stdio",
                "command": "earthscope-mcp",
                "args": ["serve"],
            },
            "tools": [
                {
                    "id": "earthscope_query",
                    "name": "earthscope_query",
                    "description": "query EarthScope catalog",
                    "status": "ready",
                    "enabled": True,
                    "input_schema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                }
            ],
        }
    }
    base_agent = SimpleNamespace(
        tool_executor=SimpleNamespace(to_dspy_tools=lambda: []),
    )
    agent_def = AgentDef(
        id="variant",
        source="expert_pack",
        title="Variant",
        tools=["earthscope_query"],
    )

    with _gact_app_context(app):
        tools = _dynamic_agent_tools(base_agent, agent_def)

    assert [tool.name for tool in tools] == ["earthscope_query"]
