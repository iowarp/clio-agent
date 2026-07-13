"""Tool-call ``thought`` must not duplicate the already-streamed ``next_thought``.

Regression for the gap where a geospatial (and any ReAct) tool_call carried the
raw ``next_thought`` on its ``thought`` field — including the ``[[ ## next_thought
## ]]`` ChatAdapter marker and the answer repeated — duplicating the next_thought
``text`` part on reload. The dedup compares the streamed text against the raw
step thought; it must match even when the raw thought is LONGER than the streamed
copy (marker re-emission).
"""

from clio_agent.gact.tool_observer import _streamed_text_matches

# The clean next_thought that already streamed as a visible text row.
STREAMED = (
    "The user has requested resolution of the place name \"Los Angeles\" to a "
    "geographic region. I need to call geo_geocode to look it up."
)


def test_drops_when_raw_thought_repeats_streamed_with_marker() -> None:
    # corruption-fixed shape: the raw step thought is the next_thought repeated,
    # with a ChatAdapter field marker between the copies -> LONGER than streamed.
    raw = f"{STREAMED}\n```[[ ## next_thought ## ]]\n{STREAMED}"
    assert _streamed_text_matches(STREAMED, raw) is True


def test_drops_when_raw_thought_is_subset_of_streamed() -> None:
    # the original-supported direction: a short, clean thought ⊂ streamed text.
    assert _streamed_text_matches(STREAMED, STREAMED.split(".")[0]) is True


def test_keeps_a_distinct_tool_thought() -> None:
    # reload-check shape: a genuinely DIFFERENT reasoning on the tool_call must be
    # kept (no false dedup) so distinct thoughts still render.
    distinct = (
        "The request provides a place name \"Los Angeles\" without explicit "
        "coordinates, so I default the radius to 50 km."
    )
    assert _streamed_text_matches(STREAMED, distinct) is False


def test_whitespace_insensitive_and_empty_safe() -> None:
    assert _streamed_text_matches(STREAMED, f"  {STREAMED}\n\n  ") is True
    assert _streamed_text_matches("", STREAMED) is False
    assert _streamed_text_matches(STREAMED, "   ") is False
