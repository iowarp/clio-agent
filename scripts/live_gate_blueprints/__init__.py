"""Shared path resolver for the synthetic #1031 live-gate blueprints."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_BLUEPRINTS_ROOT = (_REPO_ROOT / "tests" / "fixtures" / "gate_blueprints").resolve()
TRANSFORM_SERVER_PATH = (Path(__file__).resolve().parent / "transform_stdio.py").resolve()
_BLUEPRINT_NAMES = frozenset({"orchestrator-worker", "react-leaf", "react-leaf-xform"})


def gate_blueprint_path(name: str) -> Path:
    """Return the shared-corpus path for a known live-gate blueprint."""
    if name not in _BLUEPRINT_NAMES:
        choices = ", ".join(sorted(_BLUEPRINT_NAMES))
        raise ValueError(f"unknown live-gate blueprint {name!r}; expected one of: {choices}")
    return (GATE_BLUEPRINTS_ROOT / name).resolve()
