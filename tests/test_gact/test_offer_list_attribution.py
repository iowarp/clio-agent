"""The offer list in the session facts is its own context category (OL-D8)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from dspy.lm15 import Message, Request, TextPart

from clio_agent import injections
from clio_agent.gact import prompt_pieces
from clio_agent.gact.agents.clio_react_codec import user_from_record
from clio_agent.gact.prompt_breakdown import request_breakdown

ROWS = [
    {"kind": "connector", "id": "hdf5", "name": "HDF5", "summary": "Read HDF5 files.", "enabled": False},
    {"kind": "plugin", "id": "tariff-calc", "name": "tariff-calc", "marketplace": "lab",
     "summary": "Landed cost of imported goods: duty rates by HS code and origin."},
]  # fmt: skip


def _facts() -> prompt_pieces.Composed:
    """Session facts composed as :mod:`~clio_agent.gact.agents.composition` does."""

    from clio_agent.gact.agents.composition import _offer_piece

    def fact(piece: str, variables: dict[str, Any]) -> str | None:
        return injections.render(f"session_facts{piece}", variables)

    head = fact("", {"root": "/work/project"})
    unlisted = [{"category": "Earth science", "count": 2}, {"category": "", "count": 1}]
    piece = _offer_piece(None, "", fact, ROWS, 5, 2, unlisted)
    return prompt_pieces.join([head, piece, "Machine: lab-7."])


def test_the_composed_facts_carry_the_list_span_with_one_item_per_row() -> None:
    facts = _facts()
    (span,) = facts.pieces
    assert span["id"] == prompt_pieces.OFFER_PIECE
    listed = facts[span["start"] : span["end"]]
    assert listed.startswith("Plugins and connectors the person has")
    # The footer that says how many more there are is inside the span (OL-D11).
    assert "plugins installed (Earth science 2; uncategorized 1); offer_plugin" in listed
    assert listed.rstrip().endswith("offer_plugin offers them the same way.")
    assert [item["label"] for item in span["items"]] == ["HDF5", "tariff-calc"]
    # Each row's size is its own line in the text, measured on the template.
    hdf5_line = next(line for line in listed.splitlines() if line.startswith("- HDF5"))
    assert abs(span["items"][0]["chars"] - (len(hdf5_line) + 1)) <= 2
    assert sum(item["chars"] for item in span["items"]) < len(listed)


def test_the_breakdown_puts_the_list_in_its_own_category_and_the_rest_in_messages() -> None:
    facts = _facts()
    record = {"text": str(facts), "source": "session_facts", "actor": "algorithm"}
    record["pieces"] = list(facts.pieces)
    folded = user_from_record(record)  # the fold keeps the spans for this exact text
    request = Request(
        model="test",
        messages=(folded, Message(role="user", parts=(TextPart(text="Plot the flux"),))),
    )
    result = request_breakdown({"request": request})
    assert result is not None
    sections = {section.id: section for section in result.sections}
    offer = sections["offerable"]
    assert offer.label == "Plugin list"
    labels = [(item.label, item.id) for item in offer.items]
    assert ("HDF5", None) in labels and ("tariff-calc", None) in labels
    assert ("List heading and closing line", prompt_pieces.FRAME_ITEM) in labels
    span = facts.pieces[0]
    assert abs(offer.tokens_estimated - (span["end"] - span["start"]) / 4) <= len(offer.items)
    # Messages keep the rest of the facts and the person's own message.
    rest = len(folded.parts[0].text) - (span["end"] - span["start"]) + len("Plot the flux")
    assert abs(sections["messages"].tokens_estimated - rest / 4) <= 2
    # The same text without recorded pieces is a plain user message.
    plain = request_breakdown(
        {"messages": [{"role": "user", "parts": [{"type": "text", "text": str(facts) + "!"}]}]}
    )
    assert plain is not None and [s.id for s in plain.sections] == ["messages"]


def test_the_ledger_shape_of_the_request_is_attributed_too() -> None:
    facts = _facts()
    folded = user_from_record(
        {"text": str(facts), "source": "session_facts", "actor": "algorithm",
         "pieces": list(facts.pieces)}
    )  # fmt: skip
    entry = {
        "messages": [{"role": "user", "parts": [{"type": "text", "text": folded.parts[0].text}]}]
    }
    result = request_breakdown(entry)
    assert result is not None and "offerable" in {s.id for s in result.sections}


def test_the_per_kind_fallback_moves_the_list_out_of_messages() -> None:
    facts = _facts()
    segment = SimpleNamespace(
        kind="user",
        content={"text": str(facts), "source": "session_facts", "pieces": list(facts.pieces)},
    )
    other = SimpleNamespace(kind="user", content={"text": "hi", "pieces": [{"id": "x"}]})
    categories = prompt_pieces.with_offer_category({"messages": 900, "tools": 50}, [segment, other])
    span = facts.pieces[0]
    tokens = (span["end"] - span["start"] + 3) // 4
    assert categories == {"messages": 900 - tokens, "tools": 50, "offerable": tokens}
    assert prompt_pieces.with_offer_category({"tools": 5}, [segment]) == {"tools": 5}


def test_the_recorder_keeps_the_spans_with_the_segment() -> None:
    from clio_agent.gact.agents.clio_react_record import StepRecorder

    facts = _facts()
    written: list[tuple[str, dict[str, Any]]] = []
    recorder = StepRecorder.__new__(StepRecorder)
    recorder.session, recorder.scope, recorder.expert_id = "sess_x", "main", "main"
    recorder.arc = SimpleNamespace(render_working_set=lambda *_a: [])
    recorder._write = lambda kind, record, *_a: written.append((kind, record))  # type: ignore[method-assign]
    recorder.injections([("session_facts", facts)])
    ((kind, record),) = written
    assert kind == "user" and record["pieces"] == list(facts.pieces)
