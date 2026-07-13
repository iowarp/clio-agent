"""The working-set observation encoder, unified onto the log coercion (#737 S2, caveat a).

The working-set writer used to coerce a tool observation through
``clio_agent.gact.runtime.context_tokens._arc_obs_value`` — a SHALLOW rule that passed
JSON-native containers through but collapsed ANY other object to ``str(value)`` — while
the canonical ``_events`` log coerces through
:func:`clio_agent.arc.segments._encode_safe` (which recursively coerces
dicts/lists/tuples/sets/pydantic/dataclass and only ``str()``s as a last resort). With
the working set becoming a FOLD of the log, the two encoders must agree or a folded
observation cannot be byte-identical to the log twin (and the ~4-chars/token heuristic
over it drifts). S2 routes ``_arc_obs_value`` through the one ``_encode_safe``.

This pins the unification and documents the divergence it retired.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from clio_agent.arc.segments import _encode_safe
from clio_agent.gact.runtime.context_tokens import _arc_obs_value


@dataclasses.dataclass
class _DC:
    a: int = 1
    b: str = "x"


class _Plain:
    def __init__(self) -> None:
        self.p = 1
        self.q = "z"


_EXOTIC_MATRIX: dict[str, Any] = {
    "none": None,
    "str": "hello",
    "int": 7,
    "float": 2.5,
    "bool": True,
    "plain_dict": {"a": 1, "b": "s"},
    "nested_list": [1, "a", {"k": "v"}],
    "tuple": ("a", 1, 2.5),
    "set_int": {3, 1, 2},
    "frozenset": frozenset({"only"}),
    "dataclass": _DC(),
    "plain_obj": _Plain(),
    "nested_exotic": {"tags": frozenset({"z"}), "dc": _DC(), "rows": ("q", 1)},
}


def _legacy_arc_obs_value(value: Any) -> Any:
    """``_arc_obs_value`` BEFORE the S2 unification (kept as a regression fence)."""
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    return str(value)


@pytest.mark.parametrize("name", sorted(_EXOTIC_MATRIX))
def test_arc_obs_value_equals_encode_safe(name: str) -> None:
    """UNIFIED: the observation encoder produces byte-identical output to the log
    encoder for every exotic case, so a folded observation matches its log twin."""
    value = _EXOTIC_MATRIX[name]
    assert _arc_obs_value(value) == _encode_safe(value)


@pytest.mark.parametrize("name", sorted(_EXOTIC_MATRIX))
def test_unified_obs_value_is_json_native_and_idempotent(name: str) -> None:
    """The coerced observation is JSON-serializable and idempotent (re-coercion is
    identity) — the property the token heuristic + fold re-render rely on."""
    out = _arc_obs_value(_EXOTIC_MATRIX[name])
    assert json.loads(json.dumps(out, sort_keys=True)) == out
    assert _arc_obs_value(out) == out


def test_legacy_obs_value_diverged_on_exotic_objects() -> None:
    """Documented evidence: the pre-unification ``_arc_obs_value`` diverged from the log
    encoder on non-container objects (dataclass/plain-object/frozenset) — WHY S2 unified.
    A plain object went to a nondeterministic ``<obj at 0x…>`` address."""
    diverged = {
        name
        for name, value in _EXOTIC_MATRIX.items()
        if _legacy_arc_obs_value(value) != _encode_safe(value)
    }
    assert {"frozenset", "dataclass", "plain_obj", "set_int"} <= diverged
    legacy_plain = _legacy_arc_obs_value(_Plain())
    assert isinstance(legacy_plain, str) and "0x" in legacy_plain
    assert _arc_obs_value(_Plain()) == {"p": 1, "q": "z"}
