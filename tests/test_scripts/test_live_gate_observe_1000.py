"""Contract tests for the #1000 live-gate launcher."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "live_gate_observe_1000.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_gate_observe_1000", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_gate_inherits_model_and_uses_real_marketplace_blueprint() -> None:
    """The gate must not pin a model or reference the removed experiment tree."""
    module = _load_script()

    parser = module.build_parser()
    args = parser.parse_args([])
    destinations = {action.dest for action in parser._actions}

    assert "model" not in destinations
    assert args.blueprint_path == "external/clio-agent-marketplace/earthscope-flat"
    assert (module.REPO / args.blueprint_path / "AGENT.md").is_file()
