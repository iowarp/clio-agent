"""Refuse attribution when text or model-call identity is ambiguous or stale."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from clio_agent.gact.attention.aggregate import Section
from clio_agent.gact.attention.chat_render import Encoded
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.rendered import find_rendered
from clio_agent.gact.attention.selection import locate_output
from clio_agent.gact.attention.service import SelectionRequest
from clio_agent.gact.attention.transcript_map import TranscriptText, anchor_texts
from tests.test_gact.test_attention._support import fixture_thought
from tests.test_gact.test_attention.test_service_real_run import _call, _explain


def test_revision_check_binds_the_exact_selected_source() -> None:
    """A stale part reference never returns heat for replacement text."""
    request = SelectionRequest(
        message_id="msg_asst_1",
        part_id="call_sel",
        field="thought",
        start=0,
        end=20,
        content_revision="old-revision",
    )
    with pytest.raises(AttentionUnavailable, match="content_revision_changed"):
        _explain(request=request)
    revision = hashlib.sha256(fixture_thought().encode()).hexdigest()
    result = _explain(request=replace(request, content_revision=revision))
    assert result["selection"]["content_revision"] == revision


def test_repeated_rendered_text_requires_an_exact_range() -> None:
    """Repeated words are not silently assigned to their first occurrence."""
    with pytest.raises(AttentionUnavailable, match="selection_ambiguous"):
        find_rendered("First north, then north.", "north")


def test_matching_multiple_outputs_requires_explicit_origin() -> None:
    """The newest matching model call is not evidence of origin."""
    first = _call(content="same answer")
    second = replace(first, event_id="newer", occurred_at="2026-10-04T12:00:00Z")
    with pytest.raises(AttentionUnavailable, match="lm_call_ambiguous"):
        locate_output([first, second], "same answer", 0, 4)


def test_repeated_prompt_occurrences_or_transcript_owners_are_unmapped() -> None:
    """No heat is projected onto an arbitrary repeated transcript passage."""
    text = TranscriptText("m1", "p1", "text", "user_text", "same")
    encoded = Encoded(text="same same", ids=list(range(9)), offsets=[(i, i + 1) for i in range(9)])
    sections = [Section(0, 9, "user", "User", 0, 0, 9)]
    assert anchor_texts([text], encoded, sections) == []
    one = Encoded(text="same", ids=list(range(4)), offsets=[(i, i + 1) for i in range(4)])
    assert len(anchor_texts([text], one, [Section(0, 4, "user", "User", 0, 0, 4)])) == 1
    assert (
        anchor_texts(
            [text, replace(text, message_id="m2")], one, [Section(0, 4, "user", "User", 0, 0, 4)]
        )
        == []
    )
