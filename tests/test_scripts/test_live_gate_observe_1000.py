"""Contract tests for the #1000 live-gate launcher."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tests._marketplace import MARKETPLACE_ROOT

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "live_gate_observe_1000.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_gate_observe_1000", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.marketplace
def test_live_gate_inherits_model_and_uses_real_marketplace_agent() -> None:
    """The gate must not pin a model or reference the removed experiment tree."""
    module = _load_script()

    parser = module.build_parser()
    args = parser.parse_args([])
    destinations = {action.dest for action in parser._actions}

    assert "model" not in destinations
    assert args.agent == "clio/earthscope"
    agents = MARKETPLACE_ROOT / "agents"
    assert any(
        'id = "earthscope"' in path.read_text(encoding="utf-8") for path in agents.glob("*.py")
    )
