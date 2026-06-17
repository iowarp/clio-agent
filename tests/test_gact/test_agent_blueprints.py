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
    _ACTIVE_GACT_SESSION_ID,
    _active_base_agent_tool_executor,
    _append_prediction_workflow_state,
    _blueprint_fanout_config,
    _blueprint_module_kind,
    _blueprint_runtime_signature,
    _bubbled_child_evidence_output_summary,
    _build_blueprint_dspy_module,
    _build_child_expert_tool,
    _build_fanout_tool,
    _builtin_agents,
    _coerce_fanout_child_ids,
    _compact_dynamic_delegation_output,
    _dynamic_agent_tools,
    _dynamic_child_expert_tools,
    _extract_tools_called_from_trajectory,
    _failed_child_delegation_output_summary,
    _fallback_answer_from_delegation,
    _gact_app_context,
    _gact_turn_timeout_s,
    _ground_fabricated_local_artifact_paths,
    _latest_final_child_output_summary,
    _merge_tool_call_rows,
    _prediction_structured_metadata,
    _recording_blueprint_tool,
    _run_blueprint_dspy_agent,
    _runtime_dynamic_agent_children_context,
    _should_execute_delegated_handoff,
    _tool_calls_from_handoff_rows,
    _user_agent_bool_param,
    _user_facing_dynamic_evidence_summary,
    _workflow_state_from_outputs,
    build_app,
)
from clio_agent.gact.types import AgentDef
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
        row.id: row for row in discover_agent_blueprints(home=tmp_path, cwd=tmp_path / "workspace")
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
    assert (
        blueprints[DEFAULT_AGENT_BLUEPRINT_ID].metadata["install"]["commit"]
        == DEFAULT_REGISTRY_COMMIT
    )


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
    assert (
        "modules/ndp-collector/experts/ndp_catalog.md"
        in rows["ndp_catalog"].metadata["definition_path"]
    )


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

    # ``system_prompt`` is always carried as an input so the expert's built body
    # reaches the model (see _blueprint_runtime_signature). Declared inputs follow.
    assert list(signature.input_fields) == ["system_prompt", "question", "dataset_summary"]
    # CLEAN CONTRACT: the only auto-injected structured output is ``workflow_state``
    # (default-enabled); the agent-driven routing fields ``next_expert``/``next_task``
    # are always appended. The legacy companions (evidence/artifacts/errors/delegation/
    # expert_handoffs) are no longer injected regardless of structured_outputs.
    assert list(signature.output_fields) == [
        "answer",
        "artifact_plan",
        "workflow_state",
        "next_expert",
        "next_task",
    ]
    for legacy in ("evidence", "artifacts", "errors", "delegation", "expert_handoffs"):
        assert legacy not in signature.output_fields


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
        "next_expert",
        "next_task",
    ]


@pytest.mark.parametrize(
    ("raw_signature", "expected_inputs", "expected_outputs"),
    [
        ({}, ["system_prompt", "question"], ["answer"]),
        ({"inputs": {}, "outputs": {}}, ["system_prompt", "question"], ["answer"]),
        ({"inputs": [], "outputs": []}, ["system_prompt", "question"], ["answer"]),
        ({"outputs": {"summary": "Short summary"}}, ["system_prompt", "question"], ["summary"]),
        (
            # declared inputs without system_prompt → it is prepended
            {"inputs": ["question", "file_context"], "outputs": ["answer", "quality_flags"]},
            ["system_prompt", "question", "file_context"],
            ["answer", "quality_flags"],
        ),
        (
            {
                "input": [{"name": "question", "description": "User goal"}],
                "output": [{"id": "answer", "desc": "Final answer"}],
            },
            ["system_prompt", "question"],
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
    # workflow_state is disabled here; the agent-driven routing fields are always
    # appended (a leaf with no children still gets next_expert=Literal["finish"]).
    assert list(signature.output_fields) == [*expected_outputs, "next_expert", "next_task"]


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
    # ``workflow_state`` is the only auto-injected structured output, and its
    # ``structured_outputs`` toggle accepts bools and truthy/falsey strings.
    signature = _blueprint_runtime_signature(
        AgentDef(
            id="structured",
            source="expert_pack",
            title="Structured",
            structured_outputs={"workflow_state": value},
        )
    )

    assert ("workflow_state" in signature.output_fields) is enabled


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
        (
            {"enabled": True, "max_workers": 3},
            {"enabled": True, "max_workers": 3, "strategy": "declared_children"},
        ),
        (
            {"enabled": "false", "max_workers": 0},
            {"enabled": False, "max_workers": 1, "strategy": "declared_children"},
        ),
        (
            {"enabled": "yes", "workers": "2", "strategy": "map_reduce"},
            {"enabled": True, "max_workers": 2, "strategy": "map_reduce"},
        ),
    ],
)
def test_blueprint_fanout_config_matrix(raw: dict[str, Any], expected: dict[str, Any]) -> None:
    assert (
        _blueprint_fanout_config(
            AgentDef(id="root", source="expert_pack", title="Root", fanout=raw)
        )
        == expected
    )


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


def test_ground_fabricated_local_artifact_path_rewrites_to_verified(tmp_path) -> None:
    real_csv = tmp_path / "ndp-staging" / "P475.CI.LY_.20.csv"
    real_png = tmp_path / "ndp-staging" / "P475.CI.LY_.20_plot.png"
    real_csv.parent.mkdir(parents=True)
    real_csv.write_text("time,east,north,up\n0,0,0,0\n")
    real_png.write_bytes(b"\x89PNG" + b"0" * 64)
    state = {
        "acquisition": {"status": "staged", "local_path": str(real_csv)},
        "artifact": {"status": "ready", "path": str(real_png)},
    }
    answer = (
        "Staged CSV: /home/x/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv\n"
        "Plot (PNG): /home/x/.clio/artifacts/plots/P475_CI_LY_timeseries.png\n"
        "Source URL: https://ds2.datacollaboratory.org/raw_csv/P475.CI.LY_.20.csv"
    )
    grounded = _ground_fabricated_local_artifact_paths(answer, state)

    # The fabricated PNG path (not on disk) is rewritten to the verified one.
    assert str(real_png) in grounded
    assert "plots/P475_CI_LY_timeseries.png" not in grounded
    # The fabricated CSV citation is rewritten to the verified staged CSV.
    assert str(real_csv) in grounded
    # The remote source URL is left untouched.
    assert "https://ds2.datacollaboratory.org/raw_csv/P475.CI.LY_.20.csv" in grounded


def test_ground_fabricated_csv_path_ignores_metadata_catalog_for_substitution(tmp_path) -> None:
    # The staged metadata catalog exists on disk too, but must NOT make the
    # verified-CSV set ambiguous: the deliverable CSV is acquisition.local_path,
    # so a fabricated csv citation is still grounded to it.
    staging = tmp_path / "ndp-staging"
    staging.mkdir()
    real_csv = staging / "P475.CI.LY_.20.csv"
    real_csv.write_text("time,east,north,up\n0,0,0,0\n")
    catalog = staging / "earthscope_converted_data.csv"
    catalog.write_text("Site,Latitude,Longitude\nP475,32,-117\n")
    state = {
        "acquisition": {
            "status": "staged",
            "local_path": str(real_csv),
            "metadata_path": str(catalog),
        },
    }
    answer = "Staged station CSV: /tmp/SAN_timeseries.csv"
    grounded = _ground_fabricated_local_artifact_paths(answer, state)

    assert str(real_csv) in grounded
    assert "/tmp/SAN_timeseries.csv" not in grounded
    assert str(catalog) not in grounded


def test_ground_fabricated_local_artifact_path_respects_missing_framing(tmp_path) -> None:
    real_png = tmp_path / "ndp-staging" / "P475.CI.LY_.20_plot.png"
    real_png.parent.mkdir(parents=True)
    real_png.write_bytes(b"\x89PNG" + b"0" * 64)
    state = {"artifact": {"status": "ready", "path": str(real_png)}}
    answer = "No figure was produced; a PNG has not been staged at /tmp/expected/P475_plot.png yet."
    grounded = _ground_fabricated_local_artifact_paths(answer, state)

    # An honestly-framed missing/expected path must not be rewritten.
    assert grounded == answer


def test_ground_fabricated_local_artifact_path_no_verified_neutralizes() -> None:
    # With no verified on-disk artifact in state (a data-blocked run), a fabricated
    # local artifact path must be neutralized rather than presented as real.
    answer = "Plot (PNG): /home/x/.clio/artifacts/plots/SAN_timeseries.png"
    grounded = _ground_fabricated_local_artifact_paths(
        answer, {"acquisition": {"status": "blocked"}}
    )

    assert "SAN_timeseries.png" not in grounded
    assert ".png" not in grounded
    assert "no local png artifact was produced" in grounded


def test_ground_fabricated_local_artifact_path_collapses_doubled_prefix(tmp_path) -> None:
    # Path-doubling: the model emits a REAL (on-disk) path with a duplicated
    # prefix. Even with multiple verified artifacts present, collapse the
    # malformed token to the embedded real path. The verified set is filesystem-
    # backed, so the real artifacts must actually exist on disk.
    staging = tmp_path / ".clio" / "artifacts" / "ndp-staging"
    staging.mkdir(parents=True)
    real = staging / "P473.PW.LY_.00.csv"
    real.write_text("time,east,north,up\n0,0,0,0\n")
    (staging / "catalog.csv").write_text("Site,Latitude,Longitude\nP473,1,2\n")
    real_s = str(real)
    # Duplicated-prefix mangling: ".../artifacts/ndp-" + the full real path.
    doubled = f"{staging.parent}/ndp-{real_s}"
    state = {
        "acquisition": {"local_path": real_s},
        "catalog": {"metadata_path": str(staging / "catalog.csv")},
    }
    grounded = _ground_fabricated_local_artifact_paths(f"Staged CSV: {doubled}.", state)
    assert real_s in grounded
    assert doubled not in grounded


def test_ground_fabricated_local_artifact_path_keeps_honest_blocked_prose() -> None:
    # An honestly-framed absence in a data-blocked answer is left intact.
    answer = (
        "No PNG was produced because staging was blocked; a figure would be "
        "written to /tmp/expected/figure.png once a station CSV is staged."
    )
    grounded = _ground_fabricated_local_artifact_paths(
        answer, {"acquisition": {"status": "blocked"}}
    )

    assert grounded == answer


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
    assert (
        state["resource_candidate"]["resource_url"]
        == "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv"
    )
    assert (
        state["acquisition"]["source_url"] == "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv"
    )




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
                            "resource_urls": ["https://example.test/raw_csv/EFGH.CI.LY_.30.csv"],
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










def test_skipped_delegated_handoff_does_not_execute_even_with_delegate_target() -> None:
    assert not _should_execute_delegated_handoff(
        {
            "delegate_to": "analysis",
            "status": "skipped",
            "skip_reason": "completed_sync_child_already_returned",
        }
    )




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


def test_blueprint_compiler_selects_declared_dspy_module_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_agent_tools", lambda base_agent, agent_def: [scoped_tool]
    )
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_child_expert_tools",
        lambda base_agent, agent_def: [child_tool],
    )

    base_agent = SimpleNamespace()
    predict = _build_blueprint_dspy_module(
        base_agent,
        AgentDef(
            id="predictor", source="expert_pack", title="Predictor", module={"kind": "predict"}
        ),
    )
    cot = _build_blueprint_dspy_module(
        base_agent,
        AgentDef(
            id="reasoner",
            source="expert_pack",
            title="Reasoner",
            module={"kind": "chain_of_thought"},
        ),
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
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_agent_tools", lambda base_agent, agent_def: []
    )
    monkeypatch.setattr(
        "clio_agent.gact.app._dynamic_child_expert_tools",
        lambda base_agent, agent_def: [],
    )

    module = _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(
            id="source_inspect", source="expert_pack", title="Source", module={"kind": "react"}
        ),
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

    def fake_run_dynamic_agent_compat(
        runner, base_agent, agent_def, question, session_id, cancel_requested
    ):
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

    monkeypatch.setattr(
        "clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "child-runner"
    )
    monkeypatch.setattr(
        "clio_agent.gact.app._run_dynamic_agent_compat", fake_run_dynamic_agent_compat
    )

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
    # clio-kit renamed this tool: ndp_filter_earthscope_station_catalog -> geo_filter_points_by_radius
    assert "  - geo_filter_points_by_radius" in tool_block
    assert "  - ndp_search_datasets" not in tool_block
    assert "  - ndp_stage_resource" not in tool_block
    assert "Do not call `ndp_stage_resource` for a station-specific time-series CSV" in prompt
    assert (
        "do not call `ndp_search_datasets` to search station-specific resources by station ID"
        in prompt
    )
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

    assert (
        "Never convert `rows_scanned` into duration, cadence, or a sampling rate"
        in normalized_prompt
    )
    assert (
        "`rows_scanned`, `rows_examined`, and file size are profiler coverage signals"
        in normalized_prompt
    )
    assert "Treat `numeric_summary_rows` or" in prompt
    assert 'Do not infer a "30-day record" from `.30`' in normalized_prompt
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

    assert "Don't convert `rows_scanned`/`rows_profiled` into" in normalized_prompt
    assert "cadence, duration, Hz, completeness" in normalized_prompt
    assert "a scan-limited profile is coverage evidence only" in normalized_prompt
    assert "Preserve uncertainty units" in normalized_prompt
    assert "not sub-cm" in normalized_prompt
    assert "Don't cite external sources (USGS/UNAVCO" in normalized_prompt
    assert "`model_geographic_prior`" in normalized_prompt


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
    assert 'Do not call uncertainty "sub-cm" unless the' in prompt
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
    assert 'Prefer wording such as "preliminary station/resource' in normalized_prompt
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

    # Per-station search keys on dataset_title with the station id (the prompt
    # explicitly warns AGAINST resource_name for the station id — it 502s).
    assert "in `dataset_title` (NOT `resource_name`, which 502s)" in normalized_prompt
    # The resolver works the ranked station list; it must not widen past it into
    # free-text / city-name searches.
    assert "don't widen beyond the ranked list" in normalized_prompt
    # An out-of-region / no-candidate region is not coverage and must not be
    # searched or staged.
    assert "Do NOT search or stage anything, do NOT" in normalized_prompt
    assert "the region has no EarthScope GNSS coverage" in normalized_prompt


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
    data_prompt = (root / blueprint_id / "experts" / "data.md").read_text(encoding="utf-8")
    discovery_prompt = (root / blueprint_id / "experts" / "ndp_dataset_discovery.md").read_text(
        encoding="utf-8"
    )
    station_prompt = (root / blueprint_id / "experts" / "earthscope_station_catalog.md").read_text(
        encoding="utf-8"
    )
    normalized_data = " ".join(data_prompt.split())
    normalized_discovery = " ".join(discovery_prompt.split())
    normalized_station = " ".join(station_prompt.split())

    # Discovery must stage the metadata catalog; the parent routes the work back
    # to discovery when only a guessed filename (not a staged path) is present.
    assert "acquisition.metadata_path" in data_prompt
    assert (
        "a guessed filename such as `earthscope_stations.csv` is not a staged path"
        in normalized_data
    )
    # Discovery must stage + clean the metadata catalog before ranking can run:
    # the cleaned workspace CSV is the station ranker's required input, and the run
    # is explicitly incomplete until that staging pipeline finishes.
    assert "The downstream ranker needs the CLEANED workspace file" in normalized_discovery
    assert "your run is INCOMPLETE until the `shell_bash` clean has run" in normalized_discovery
    # The station ranker filters the staged metadata path, never a guessed name
    # (tool renamed: ndp_filter_earthscope_station_catalog -> geo_filter_points_by_radius).
    assert "never filter the raw catalog or a guessed filename" in normalized_station
    assert (
        "the cleaned catalog at `acquisition.metadata_path` with `geo_filter_points_by_radius`"
        in normalized_station
    )


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
    synthesis_prompt = (root / blueprint_id / "experts" / "synthesis.md").read_text(
        encoding="utf-8"
    )
    analysis_prompt = (root / blueprint_id / "experts" / "gnss_timeseries_analysis.md").read_text(
        encoding="utf-8"
    )
    visualization_prompt = (root / blueprint_id / "experts" / "visualization.md").read_text(
        encoding="utf-8"
    )
    combined = " ".join(
        "\n".join([main_prompt, synthesis_prompt, analysis_prompt, visualization_prompt]).split()
    )

    assert "rows_profiled`/`numeric_summary_rows" in combined
    assert "appears in `numeric_summary`" in combined
    assert "belongs only to `numeric_summary_rows`" in combined
    assert "say only that the run produced a scan-limited profile" in combined
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
    assert "Named source provenance is allowed only when a tool result" in combined


















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

    monkeypatch.setattr(
        "clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "child-runner"
    )
    monkeypatch.setattr(
        "clio_agent.gact.app._run_dynamic_agent_compat",
        lambda runner,
        base_agent,
        agent_def,
        question,
        session_id,
        cancel_requested: SimpleNamespace(
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

    def fake_run_dynamic_agent_compat(
        runner, base_agent, agent_def, question, session_id, cancel_requested
    ):
        calls.append(agent_def.id)
        return SimpleNamespace(
            answer=f"{agent_def.id} compact evidence", evidence=f"{agent_def.id}:evidence"
        )

    monkeypatch.setattr(
        "clio_agent.gact.app._blueprint_runner_for_agent", lambda agent_def: "child-runner"
    )
    monkeypatch.setattr(
        "clio_agent.gact.app._run_dynamic_agent_compat", fake_run_dynamic_agent_compat
    )

    token = _ACTIVE_GACT_SESSION_ID.set("session-123")
    try:
        with _gact_app_context(app):
            tool = _build_fanout_tool(SimpleNamespace(), parent, children)
            payload = json.loads(
                tool(question="inspect", child_ids="analysis,visualization,quality")
            )
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
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"blueprint_id": "genomics"},
            ).status_code
            == 200
        )
        root = next(
            AgentDef(**row)
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
            if row["id"] == "root"
        )

    context = _runtime_dynamic_agent_children_context(app, root, session_id=sid)

    assert "Your child experts (delegate to these — you may route to no one else):" in context
    assert "- `variant`: Variant Expert" in context
    assert "memory_search_sessions" in context
    assert "set `next_expert` to the id of the ONE child to run next" in context
    assert "`next_task`" in context
    # The child listing moved out of the static system_prompt into the runtime
    # children context (asserted above); the system_prompt must still have no
    # unresolved template markers.
    assert "{{" not in root.system_prompt


def test_session_agent_overlay_is_session_local(tmp_path: Path) -> None:
    blueprint = tmp_path / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid_a = client.post("/v1/sessions", json={"title": "A"}).json()["id"]
        sid_b = client.post("/v1/sessions", json={"title": "B"}).json()["id"]
        for sid in (sid_a, sid_b):
            assert (
                client.post(
                    f"/v1/sessions/{sid}/agent-blueprint",
                    json={"path": str(blueprint)},
                ).status_code
                == 200
            )
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
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(blueprint)},
            ).status_code
            == 200
        )

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
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(source)},
            ).status_code
            == 200
        )
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
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(blueprint)},
            ).status_code
            == 200
        )
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
        assert (
            client.post(
                f"/v1/sessions/{sid_genomics}/agent-blueprint",
                json={"blueprint_id": "genomics-review"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{sid_materials}/agent-blueprint",
                json={"blueprint_id": "materials-crystal-review"},
            ).status_code
            == 200
        )

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
        assert (
            client.post(
                "/v1/agent-blueprints/install",
                json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
            ).status_code
            == 201
        )
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
        assert (
            "Updated Variant Expert"
            in (
                workspace / ".clio" / "agent-blueprints" / "genomics" / "experts" / "variant.md"
            ).read_text()
        )
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
        assert (
            client.post(
                f"/v1/sessions/{sid_blueprint}/agent-blueprint",
                json={"blueprint_id": "remote-data"},
            ).status_code
            == 200
        )
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
    assert "unknown tool reference: missing_external_tool" in "\n".join(body["validation_errors"])


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
    assert call.json()["content"] == [{"type": "text", "text": "earthscope_query:ANMO"}]
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
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"blueprint_id": "earth"},
            ).status_code
            == 200
        )
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


def test_active_base_agent_tool_executor_prefers_per_workspace() -> None:
    """A bound workspace routes dynamic-agent tools to the per-workspace executor.

    Blueprint/expert tools are bound to a concrete executor instance, so the
    cwd of the executor's stdio MCP subprocesses is fixed at bind time. When a
    workspace is active, the resolver must hand back the agent's per-workspace
    executor (whose stdio MCPs spawn with cwd=workspace) rather than the shared
    default executor; otherwise staged artifacts land in the server cwd.
    """
    from clio_agent.tools.execution import tool_workspace_context

    default_executor = object()
    workspace_executor = object()
    base_agent = SimpleNamespace(
        tool_executor=default_executor,
        _active_tool_executor=lambda: (
            workspace_executor
            if __import__(
                "clio_agent.tools.execution",
                fromlist=["get_active_tool_workspace_root"],
            ).get_active_tool_workspace_root()
            else default_executor
        ),
    )

    # No workspace bound -> default executor (current behavior).
    assert _active_base_agent_tool_executor(base_agent) is default_executor

    # Workspace bound -> per-workspace executor takes effect.
    with tool_workspace_context("/ws/alpha"):
        assert _active_base_agent_tool_executor(base_agent) is workspace_executor


def test_active_base_agent_tool_executor_falls_back_without_seam() -> None:
    """Agents lacking the per-workspace seam keep using the default executor."""
    default_executor = object()
    base_agent = SimpleNamespace(tool_executor=default_executor)
    assert _active_base_agent_tool_executor(base_agent) is default_executor
