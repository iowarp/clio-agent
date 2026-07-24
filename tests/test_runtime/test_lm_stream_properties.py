"""Property-based invariant for the streamed answer-field extractor (#773).

``AnswerFieldExtractor`` turns an in-order ChatAdapter token stream into clean
field deltas, holding back a short tail so a section marker split across chunks
is never leaked as answer text. The load-bearing invariant is *chunking
invariance*: however the same text is split into deltas, the concatenation of
``feed()`` outputs plus the final ``flush()`` must equal the single-shot
extraction of the whole text. This pilot pins that with hypothesis over
arbitrary marker-bearing text and arbitrary chunk boundaries.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from clio_agent.runtime.lm_stream import AnswerFieldExtractor

# Canonical ChatAdapter field names. Each canonical ``[[ ## name ## ]]`` marker is
# no longer than ``workflow_state``'s, so the extractor's hold-back tail always
# covers a marker split across chunks (which is what makes the invariant hold).
_FIELDS = [
    "reasoning",
    "answer",
    "workflow_state",
    "next_thought",
    "next_expert",
    "next_task",
]

# Section body text: printable ASCII plus newlines/tabs. Content is free to
# contain marker-shaped noise; the invariant compares streamed against single-shot
# extraction, so any such line is handled identically on both sides.
_BODY = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126)
    | st.sampled_from(["\n", "\t", " "]),
    max_size=40,
)


@st.composite
def _marker_text(draw: st.DrawFn) -> str:
    """A stream of canonical ``[[ ## field ## ]]`` sections with arbitrary bodies."""
    sections = draw(
        st.lists(
            st.tuples(st.sampled_from(_FIELDS), _BODY),
            min_size=0,
            max_size=6,
        )
    )
    parts = [f"[[ ## {field} ## ]]\n{body}\n" for field, body in sections]
    return "".join(parts)


def _run(field: str, chunks: list[str]) -> str:
    extractor = AnswerFieldExtractor(field)
    emitted = "".join(extractor.feed(chunk) for chunk in chunks)
    return emitted + extractor.flush()


@given(field=st.sampled_from(_FIELDS), text=_marker_text(), data=st.data())
@settings(max_examples=200, deadline=None)
def test_chunking_is_extraction_invariant(field: str, text: str, data: st.DataObject) -> None:
    """Any chunking of the text yields the same field output as one-shot feed."""
    # Draw arbitrary cut points to partition ``text`` into ordered chunks.
    n = len(text)
    if n:
        cuts = sorted(
            data.draw(
                st.lists(
                    st.integers(min_value=1, max_value=n),
                    unique=True,
                    max_size=n,
                )
            )
        )
    else:
        cuts = []
    chunks: list[str] = []
    prev = 0
    for cut in cuts:
        chunks.append(text[prev:cut])
        prev = cut
    chunks.append(text[prev:])

    streamed = _run(field, chunks)
    one_shot = _run(field, [text])
    assert streamed == one_shot


@given(field=st.sampled_from(_FIELDS), text=_marker_text())
@settings(max_examples=200, deadline=None)
def test_single_char_chunking_matches_one_shot(field: str, text: str) -> None:
    """The most adversarial chunking — one char at a time — still matches."""
    streamed = _run(field, list(text))
    one_shot = _run(field, [text])
    assert streamed == one_shot
