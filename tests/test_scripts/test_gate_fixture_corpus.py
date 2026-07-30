"""Regression coverage for the shared #1031 live-gate blueprint corpus."""

from pathlib import Path

from clio_agent.gact.agent_blueprints import (
    load_agent_blueprint_path,
    parse_agent_blueprint_root,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPO_ROOT / "tests" / "fixtures" / "gate_blueprints"
BLUEPRINT_NAMES = ("orchestrator-worker", "react-leaf", "react-leaf-xform")


def test_gate_blueprint_corpus_parses_and_loader_uses_shared_home() -> None:
    """Every synthetic gate blueprint is valid and resolved from the shared corpus."""
    assert CORPUS_ROOT.is_dir(), f"shared gate fixture corpus is missing: {CORPUS_ROOT}"

    from scripts.live_gate_blueprints import gate_blueprint_path

    for name in BLUEPRINT_NAMES:
        root = CORPUS_ROOT / name
        assert (root / "AGENT.md").is_file()

        blueprint = parse_agent_blueprint_root(root, scope="test")
        experts = load_agent_blueprint_path(root, scope="test")
        assert blueprint.enabled, blueprint.validation_errors
        assert blueprint.root_expert
        assert experts, f"{name} must declare at least one expert"
        assert gate_blueprint_path(name) == root.resolve()


def test_live_gate_parser_and_scenario_table_are_intact() -> None:
    """The non-live CLI seam retains every #1031 scenario and its defaults."""
    from scripts import live_gate_1031

    expected_gates = {
        "bashprobe": "gate_bashprobe",
        "composed": "gate_composed",
        "extras": "gate_extras",
        "p1": "gate_p1",
        "p2": "gate_p2",
        "p3": "gate_p3",
        "smoke": "gate_smoke",
    }
    assert {name: gate.__name__ for name, gate in live_gate_1031.GATES.items()} == expected_gates

    args = live_gate_1031._build_parser().parse_args(["p2"])
    assert vars(args) == {
        "gate": "p2",
        "port": 17931,
        "model": "haiku",
        "transport": "sdk",
        "turn_timeout_s": 900.0,
    }
