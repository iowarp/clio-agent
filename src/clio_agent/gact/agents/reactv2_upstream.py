"""Fail-loud contract checks for CLIO's DSPy ReActV2 integration."""

from __future__ import annotations

import hashlib
import inspect
from typing import Any

import dspy.predict.react_v2 as upstream
from dspy.predict.react_v2 import ReActV2

_EXPECTED_SOURCE_HASHES = {
    "forward": "a3406601647c38dd29e1f1b6635bb5259ead78244580850175a3d01506e73a9c",
    "_make_submit_tool": "edda6a957897c2e25816ce47c855037edd886980ab7129babb66e6982ee20cd4",
    "_make_react_signature": "c241a5165bbb093bf46ca79b060b243e8960942041d6ab055c278fc759e1422b",
}
_PRIVATE_HELPERS = (
    "_append_history_event",
    "_coerce_history",
    "_coerce_tool_calls",
    "_ensure_tool_call_ids",
    "_json_schema_for_annotation",
)


def assert_dspy_reactv2_contract() -> None:
    """Assert every consumed private seam and the three upstream bodies are pinned."""
    missing = [name for name in _PRIVATE_HELPERS if not hasattr(upstream, name)]
    if missing:
        raise RuntimeError(f"DSPy ReActV2 private API drift: missing {missing}")
    expected_signatures: dict[str, str] = {
        "_make_react_signature": "(self) -> 'type[Signature]'",
        "_make_submit_tool": "(self) -> 'Tool'",
        "forward": "(self, **input_args)",
    }
    for name, expected in expected_signatures.items():
        actual = str(inspect.signature(getattr(ReActV2, name)))
        if actual != expected:
            raise RuntimeError(
                f"DSPy ReActV2 signature drift for {name}: expected {expected}, got {actual}"
            )
    for name, expected_hash in _EXPECTED_SOURCE_HASHES.items():
        source = inspect.getsource(getattr(ReActV2, name))
        actual_hash = hashlib.sha256(source.encode()).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"DSPy ReActV2 body drift for {name}: expected {expected_hash}, got {actual_hash}"
            )


def dspy_reactv2_contract_snapshot() -> dict[str, Any]:
    """Return the pinned private-contract evidence for diagnostic tests."""
    assert_dspy_reactv2_contract()
    return {"hashes": dict(_EXPECTED_SOURCE_HASHES), "helpers": _PRIVATE_HELPERS}


__all__ = ["assert_dspy_reactv2_contract", "dspy_reactv2_contract_snapshot"]
