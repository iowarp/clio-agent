"""Property-based invariants for the planner action-JSON repair layer (#773).

``ClioAgent._parse_action_json`` is the sanctioned *format-only* barrier: it may
close a truncated advisory ``reason`` string and append missing trailing
delimiters, but it must NEVER invent argument data the model did not send, and it
must NEVER crash on partial input — a truncated stream must either yield a
prefix-consistent object or a plain ``ValueError`` (its ``UnsupportedPlanner
ActionError`` subclass included). This pilot pins two invariants with hypothesis:

* **round-trip** — a well-formed action object (text or mapping) parses back to
  itself (action kind normalised to stripped-lowercase);
* **truncation-never-raises / never-invents** — every character-prefix of a valid
  action object either raises ``ValueError`` or returns an object whose keys and
  values are all present-and-equal in the original, save a closed ``reason`` that
  may only be a prefix of the original reason text.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from clio_agent.agent import SUPPORTED_PLANNER_ACTION_KINDS, ClioAgent

# Printable ASCII minus the two characters that would make truncation reasoning
# about string boundaries ambiguous: a backslash (escape) or a double quote
# (string terminator). Keeping values quote/backslash-free means a closed
# ``reason`` decodes to an exact prefix of the original text.
_SAFE_TEXT = st.text(
    alphabet=st.characters(
        min_codepoint=32,
        max_codepoint=126,
        blacklist_characters='"\\',
    ),
    max_size=24,
)

# Object keys: short, quote/backslash-free, and never the reserved ``action`` key
# (which we always set explicitly).
_KEY = st.text(
    alphabet=st.characters(
        min_codepoint=48,
        max_codepoint=122,
        blacklist_characters='"\\',
    ),
    min_size=1,
    max_size=8,
).filter(lambda k: k != "action")


@st.composite
def _action_objects(draw: st.DrawFn) -> dict[str, object]:
    """A well-formed planner action object with string-only values."""
    obj: dict[str, object] = {
        "action": draw(st.sampled_from(sorted(SUPPORTED_PLANNER_ACTION_KINDS)))
    }
    extras = draw(st.dictionaries(keys=_KEY, values=_SAFE_TEXT, max_size=4))
    obj.update(extras)
    return obj


@given(obj=_action_objects())
@settings(max_examples=200, deadline=None)
def test_round_trip_text(obj: dict[str, object]) -> None:
    """Serialising a valid action and re-parsing the text yields the same object."""
    parsed = ClioAgent._parse_action_json(json.dumps(obj))
    expected = dict(obj)
    expected["action"] = str(obj["action"]).strip().lower()
    assert parsed == expected


@given(obj=_action_objects())
@settings(max_examples=200, deadline=None)
def test_round_trip_mapping(obj: dict[str, object]) -> None:
    """Passing a mapping straight through parses without mutating the input."""
    snapshot = dict(obj)
    parsed = ClioAgent._parse_action_json(obj)
    expected = dict(obj)
    expected["action"] = str(obj["action"]).strip().lower()
    assert parsed == expected
    assert obj == snapshot  # input mapping is not mutated in place


@given(obj=_action_objects(), frac=st.floats(min_value=0.0, max_value=1.0))
@settings(max_examples=200, deadline=None)
def test_truncation_never_raises_or_invents(
    obj: dict[str, object], frac: float
) -> None:
    """Any prefix of a valid action object: sanctioned ValueError or a
    prefix-consistent object — never an unexpected crash, never fabricated data."""
    full = json.dumps(obj)
    cut = int(len(full) * frac)
    prefix = full[:cut]

    try:
        parsed = ClioAgent._parse_action_json(prefix)
    except ValueError:
        # ValueError (and its UnsupportedPlannerActionError subclass) is the only
        # sanctioned failure for malformed/partial planner output.
        return

    assert isinstance(parsed, dict)
    for key, value in parsed.items():
        assert key in obj, f"parser invented key {key!r}"
        original = obj[key]
        if key == "action":
            assert value == str(original).strip().lower()
        elif key == "reason":
            # The only value the repair path may alter: a truncated advisory
            # reason closed to a prefix of the original text.
            assert isinstance(value, str)
            assert isinstance(original, str)
            assert original.startswith(value), (
                f"reason {value!r} is not a prefix of {original!r}"
            )
        else:
            assert value == original, (
                f"parser invented value for {key!r}: {value!r} != {original!r}"
            )


@given(text=st.text(max_size=64))
@settings(max_examples=200, deadline=None)
def test_arbitrary_text_never_crashes(text: str) -> None:
    """Wholly arbitrary input either parses to a dict or raises ValueError only."""
    try:
        result = ClioAgent._parse_action_json(text)
    except ValueError:
        return
    assert isinstance(result, dict)
