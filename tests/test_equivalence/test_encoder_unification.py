"""The encoder question (design §2.8.b caveat a, #737 S1 — the slice's highest risk).

Two coercions used to encode a semantic event's body:

* :func:`clio_agent.arc.segments._encode_safe` — encodes the CANONICAL ``_events`` log
  (via ``build_event_content``);
* ``clio_agent.gact.semantic_events._json_safe`` — encodes the DURABLE-TRACE JSONL line
  (via ``SemanticEvent.to_dict("full")``) AND the SSE projection.

If the two diverge on any reachable payload, the durable trace is NOT a lossless
derivation of ``_events`` and the #762 backfill round-trip breaks. They DID diverge (the
:data:`_LEGACY_JSON_SAFE_DIVERGENCES` matrix, reproduced here from the pre-unification
function): sets sorted vs iteration order, frozenset/dataclass/plain-object → ``str()``
(a nondeterministic ``<obj at 0x…>`` memory address), and ``exclude_none`` pydantic
dumps. The slice's production change UNIFIES ``_json_safe`` onto the one ``_encode_safe``
coercion (owner module ``arc/segments.py``). This module pins:

* the two are now identical on the exotic-type matrix (and the result is JSON-native,
  idempotent, and json-round-trip stable);
* the pre-unification divergences are documented as a regression fence;
* the unified coercion is strictly MORE robust — the old ``sorted`` set path raised
  ``TypeError`` on a mixed-type set; the unified one never raises.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from clio_agent.arc.segments import _encode_safe
from clio_agent.gact.semantic_events import _json_safe


@dataclasses.dataclass
class _DC:
    a: int = 1
    b: str = "x"


class _Plain:
    def __init__(self) -> None:
        self.p = 1
        self.q = "z"


class _PydLike:
    """A pydantic-ish object whose ``model_dump`` honors ``exclude_none``."""

    def model_dump(self, *_a: Any, **kw: Any) -> dict[str, Any]:
        data = {"x": 1, "y": None}
        if kw.get("exclude_none"):
            return {k: v for k, v in data.items() if v is not None}
        return data


# The exotic-type matrix — the payload classes a happy-path capture never exercises
# (design §4.1.B). Each is a value an emit site can genuinely put in a body field.
_EXOTIC_MATRIX: dict[str, Any] = {
    "none": None,
    "str": "hello",
    "int": 7,
    "float": 2.5,
    "bool": True,
    "plain_dict": {"a": 1, "b": "s"},
    "nested_list": [1, "a", {"k": "v"}],
    "tuple": ("a", 1, 2.5),
    "set_str": {"b", "a", "c"},
    "set_int": {3, 1, 2},
    "frozenset": frozenset({"only"}),
    "dataclass": _DC(),
    "plain_obj": _Plain(),
    "pyd_none": _PydLike(),
    "int_keys": {1: "a", 2: "b"},
    "bytes": b"hello",
    "nested_exotic": {"tags": frozenset({"z"}), "dc": _DC(), "rows": ("q", 1)},
}


# --------------------------------------------------------------------------- #
# The pre-unification function, reproduced so the divergence is DOCUMENTED in-code.
# --------------------------------------------------------------------------- #


def _legacy_json_safe(value: Any) -> Any:
    """The ``_json_safe`` body BEFORE the S1 unification (kept as a regression fence)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _legacy_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_legacy_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(_legacy_json_safe(v) for v in value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _legacy_json_safe(model_dump(exclude_none=True))
        except TypeError:
            return _legacy_json_safe(model_dump())
    return str(value)


#: The exotic classes on which the legacy encoder diverged from the log encoder — the
#: EVIDENCE that unification was required (the slice's headline finding).
_LEGACY_JSON_SAFE_DIVERGENCES = frozenset(
    {"frozenset", "dataclass", "plain_obj", "pyd_none", "nested_exotic"}
)


# --------------------------------------------------------------------------- #
# Post-unification: the two coercions are identical, JSON-native, idempotent.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(_EXOTIC_MATRIX))
def test_json_safe_equals_encode_safe(name: str) -> None:
    """UNIFIED: ``_json_safe`` produces byte-identical output to the log encoder for
    EVERY exotic-type case — so the trace body is a lossless derivation of ``_events``."""
    value = _EXOTIC_MATRIX[name]
    assert _json_safe(value) == _encode_safe(value)


@pytest.mark.parametrize("name", sorted(_EXOTIC_MATRIX))
def test_unified_output_is_json_native_and_idempotent(name: str) -> None:
    """The coercion output is JSON-serializable, idempotent, and json-round-trip stable
    — the properties the #762 backfill round-trip relies on (re-encoding is identity)."""
    value = _EXOTIC_MATRIX[name]
    out = _json_safe(value)
    dumped = json.dumps(out, sort_keys=True)  # must not raise
    assert json.loads(dumped) == out  # json round-trips the coerced form
    assert _json_safe(out) == out  # idempotent on already-coerced input


def test_legacy_encoder_diverged_on_the_exotic_matrix() -> None:
    """Documented evidence: the PRE-unification ``_json_safe`` diverged from the log
    encoder on exactly the exotic classes in :data:`_LEGACY_JSON_SAFE_DIVERGENCES`
    (frozenset/dataclass/plain-object/pydantic-None). This is WHY the slice unified."""
    diverged = {
        name
        for name, value in _EXOTIC_MATRIX.items()
        if _legacy_json_safe(value) != _encode_safe(value)
    }
    # ``set_str``/``set_int`` MAY or may not diverge (iteration vs sorted order can
    # coincide for a given hash seed), so they are not asserted; the deterministic
    # divergences are the fence.
    assert _LEGACY_JSON_SAFE_DIVERGENCES <= diverged


def test_plain_object_no_longer_leaks_a_memory_address() -> None:
    """The sharpest legacy divergence: a plain object went to ``str()`` — a
    NONDETERMINISTIC ``<obj at 0x…>`` address that could never match ``_events`` across
    two encodes. The unified coercion yields its ``__dict__`` (deterministic)."""
    legacy = _legacy_json_safe(_Plain())
    assert isinstance(legacy, str) and "0x" in legacy  # the old address leak
    assert _json_safe(_Plain()) == {"p": 1, "q": "z"}  # unified: stable dict


def test_unified_encoder_is_more_robust_than_legacy_sorted_sets() -> None:
    """A genuinely mixed-type set made the legacy ``sorted`` path raise ``TypeError``
    (a latent crash on the SSE/ trace write); the unified coercion never raises."""
    mixed = {1, "a"}
    with pytest.raises(TypeError):
        _legacy_json_safe(mixed)
    coerced = _json_safe(mixed)  # must not raise
    assert isinstance(coerced, list) and set(coerced) == {1, "a"}
