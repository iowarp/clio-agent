"""Regression lock for the #736 double-answer at the TurnTranscript/finalize seam.

The user-visible #736 bug was synthesis's answer appearing TWICE: once from the
child (which lands its deliverable at its own LM-call site) and once restated by
the main orchestrator when finalize appended the turn's canonical answer channel.
The #736 fix makes a declared ``final_responder`` child's answer BE the turn
deliverable and flips ``selected_expert`` to that child, so at finalize
``responder_agent_id`` names the child and finalize's op-identity dedup collapses
the server-side double to exactly one answer part.

WHAT THIS MODULE LOCKS (and what it does NOT):

* It locks the load-bearing SEAM: the collapse depends on
  ``turn_terminal.adopt_final_responder_answer`` setting ``selected_expert =
  child_id`` (``turn_terminal.py`` line 129). This module derives the finalize
  responder id FROM the real ``adopt_final_responder_answer`` output — it does
  NOT hardcode ``"synthesis"``. Comment out that flip and
  :func:`test_finalize_collapses_when_adopt_flips_selected_expert` and
  :func:`test_the_flip_is_what_collapses_the_double` FAIL, observing the #736
  double return. That is the teeth: the assertions are wired to the production
  mutation, not to a literal in the harness.
* It does NOT re-prove the settle-loop decision (``tests/test_turn_terminal.py``
  owns that) nor the two attribution expressions' own wiring in ``turn.py`` /
  ``turn_finalize.py`` (those inline reads are exercised, verbatim, below but
  the inline forms themselves are not import-linked — see the note on
  ``_adopt_and_derive_responder``).

The seam reproduced faithfully with the REAL objects (no mocks of the mechanism):

* ``turn_delegation.py`` line 230 — the terminal child (synthesis) lands its
  answer at its LM-call site via ``field_stream(child_id, "answer").finish(
  fallback_text=...)``: a batch FALLBACK BURST with no prior stream (NOT
  ``append()`` then ``finish()``). This module matches that shape exactly.
* ``turn.py`` line 482 — ``state.selected_agent = getattr(state.pred,
  "selected_expert", "") or ""`` reads the settled prediction.
* ``turn_finalize.py`` lines 346/354 — finalize takes
  ``transcript.turn_answer_stream(responder_agent_id, covering_label)`` where
  ``responder_agent_id = state.selected_agent or state.invocation_agent_id or
  "main"`` and ``FieldStream.finish`` decides — by op identity over the
  channel's ``covers`` set (``transcript.py`` line 843 ``_landed_locked``),
  never by string comparison — whether the batch fallback lands. When the
  covered channel already produced a closed part, the fallback is audited
  ``transcript.fieldstream.fallback_ignored reason=already_streamed``
  (``transcript.py`` line 896) and NO second part lands.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest

from clio_agent.gact import transcript as transcript_mod
from clio_agent.gact.transcript import TurnTranscript
from clio_agent.gact.turn_terminal import adopt_final_responder_answer
from clio_agent.gact.workflow_state.schema import WorkflowStateSchema

# The exact deliverable the terminal child (synthesis) streamed at its LM-call
# site; the parent would restate this verbatim, producing the #736 double.
_CHILD_ANSWER = "THE FINAL SYNTHESIS ANSWER."


class _CapturePublisher:
    """Records every wire event the ledger publishes (no bus, no network)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event_type: str, payload: Any) -> None:
        self.events.append((event_type, dict(payload)))


def _adopt_and_derive_responder(
    parent_selected_expert: str,
    invocation_agent_id: str,
) -> tuple[Any, str, str]:
    """Run the REAL #736 adopt path and derive the responder as production does.

    Builds a real parent routing prediction (the settle loop's ``latest_pred``)
    and a real completed delegation row, calls the PRODUCTION
    :func:`clio_agent.gact.turn_terminal.adopt_final_responder_answer`, then
    applies the two production attribution expressions VERBATIM against the adopt
    output:

    * ``turn.py`` line 482 — ``selected_agent = getattr(pred, "selected_expert",
      "") or ""``.
    * ``turn_finalize.py`` line 346 — ``responder_agent_id = selected_agent or
      invocation_agent_id or "main"``.

    The responder is DERIVED from the real adopt result, never hardcoded, so the
    collapse assertions are bound to production: if
    ``adopt_final_responder_answer`` stops flipping ``selected_expert``
    (``turn_terminal.py`` line 129), the derived responder reverts to the
    parent's own value and the #736 double returns.

    (Note: the ``or … or "main"`` fallback is applied inline here rather than via
    an imported helper. Extracting a shared helper into ``turn_finalize.py`` —
    the ideal, so the test links the production expression symbol — is blocked:
    that file sits at its ``check_file_size.py`` ratchet baseline with zero
    headroom, and the baseline may only ratchet DOWN. The teeth do not depend on
    that extraction: they come from calling the real ``adopt`` above and reading
    its ``selected_expert``.)

    Args:
        parent_selected_expert: The parent's routing value before adoption (the
            settle loop's ``latest_pred.selected_expert``, e.g. ``"main"``).
        invocation_agent_id: The active orchestrator id feeding the
            ``turn_finalize.py`` line 346 fallback.

    Returns:
        ``(deliverable, adopt_responder, pre_adopt_responder)``. ``deliverable``
        is the real adopt output. ``adopt_responder`` is derived from the adopt
        output's (flipped) ``selected_expert``. ``pre_adopt_responder`` is
        derived from the parent's own pre-adopt routing value — the value that
        SURVIVES if ``turn_terminal.py`` line 129 stops flipping — so the two
        responders diverge ONLY because of that flip.
    """

    parent_pred = dspy.Prediction(
        answer="",
        reasoning="route to synthesis",
        selected_expert=parent_selected_expert,
        next_expert="synthesis",
        execution_path="main>synthesis",
    )
    completed_row: dict[str, Any] = {
        "agent_id": "synthesis",
        "status": "completed",
        "stage": "delegate.completed",
        "output": _CHILD_ANSWER,
        "workflow_state": {},
    }
    deliverable = adopt_final_responder_answer(
        parent_pred, completed_row, "synthesis", schema=WorkflowStateSchema()
    )
    # turn.py:482 applied to the REAL adopt output (post-fix: flipped to "synthesis").
    selected_agent = getattr(deliverable, "selected_expert", "") or ""
    # turn.py:482 applied to the parent's PRE-adopt value — the counterfactual
    # where turn_terminal.py:129 no longer flips selected_expert. ``adopt`` copies
    # the prediction, so ``parent_pred`` still carries the original value here.
    pre_adopt_selected = getattr(parent_pred, "selected_expert", "") or ""
    # turn_finalize.py:346 for each.
    adopt_responder = selected_agent or invocation_agent_id or "main"
    pre_adopt_responder = pre_adopt_selected or invocation_agent_id or "main"
    return deliverable, adopt_responder, pre_adopt_responder


def _run_finalize_seam(
    responder_agent_id: str,
    covering_label: str,
    fallback_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Drive the real ledger through the child-land + finalize-answer seam.

    Args:
        responder_agent_id: The agent the finalize canonical-answer channel is
            authored to (``turn_finalize.py`` line 355). Derived from production
            by :func:`_adopt_and_derive_responder`, never hardcoded.
        covering_label: The stream-tap attribution fallback label
            (``turn_finalize.py`` line 356 — ``active/invocation agent or
            "main"``).
        fallback_text: The batch fallback the finalize channel would land
            (``turn_finalize.py`` line 436 — ``state.answer_text``, i.e. the
            settled deliverable's answer).
        monkeypatch: Captures ``transcript.py``'s module-global ``stream_audit``.

    Returns:
        ``(answer_texts, fallback_ignored_audits)`` where ``answer_texts`` is the
        persisted text of every closed ``answer``-field text part in arrival
        order — its LENGTH is the number of user-visible answer parts.
    """

    audits: list[tuple[str, dict[str, Any]]] = []
    # ``stream_audit`` is module-global in transcript.py; capture it so the
    # no-silent-fallback ``fallback_ignored`` reason is observable.
    monkeypatch.setattr(
        transcript_mod,
        "stream_audit",
        lambda stage, **fields: audits.append((stage, dict(fields))),
        raising=True,
    )

    transcript = TurnTranscript(
        session_id="sess_736",
        turn_id="turn_736",
        publisher=_CapturePublisher(),
        # Identity scrubber: the collapse is op-identity, not text-based, so a
        # verbatim clean_text keeps the assertions exact without changing the
        # mechanism under test.
        clean_text=lambda text: text,
    )

    # (1) The terminal child (synthesis) lands its answer at its own LM-call site
    # EXACTLY as turn_delegation.py line 230 does: a batch FALLBACK BURST with no
    # prior stream (``field_stream(...).finish(fallback_text=...)``, NOT
    # ``append()`` then ``finish()``), which closes into
    # ``_closed_text[("synthesis", "answer")]``.
    transcript.field_stream("synthesis", "answer").finish(fallback_text=_CHILD_ANSWER)

    # (2) The finalize seam (turn_finalize.py lines 354/407/434), in production
    # order: take the canonical answer channel, close open text, then finish with
    # the deliverable's answer as the batch fallback.
    answer_channel = transcript.turn_answer_stream(responder_agent_id, covering_label)
    transcript.close_open_text()
    answer_channel.finish(fallback_text=fallback_text)

    parts = transcript.finalize()
    answer_texts = [
        part.text
        for part in parts
        if part.type == "text" and part.metadata.get("signature_field_name") == "answer"
    ]
    fallback_ignored = [
        fields for stage, fields in audits if stage == "transcript.fieldstream.fallback_ignored"
    ]
    return answer_texts, fallback_ignored


def test_finalize_collapses_when_adopt_flips_selected_expert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION LOCK (teeth): responder DERIVED from real adopt → ONE answer part.

    ``adopt_final_responder_answer`` flips ``selected_expert`` to ``synthesis``
    (``turn_terminal.py`` line 129); production reads that back
    (``turn.py`` line 482) and computes ``responder_agent_id`` = ``synthesis``
    (``turn_finalize.py`` line 346). The finalize channel then covers the label
    the child already landed on, so its batch fallback is audited + ignored by op
    identity — the server-side double collapses to one part.

    If line 129 is reverted, ``adopt`` no longer flips, the derived responder
    reverts to the parent's value (``main``), the finalize channel covers a label
    NOTHING landed on, the batch fallback lands, and this assertion FAILS
    observing the #736 double.
    """

    deliverable, adopt_responder, _pre = _adopt_and_derive_responder(
        parent_selected_expert="main", invocation_agent_id="main"
    )
    answer_texts, fallback_ignored = _run_finalize_seam(
        adopt_responder, "main", str(deliverable.answer or ""), monkeypatch
    )

    assert answer_texts == [_CHILD_ANSWER], (
        "the child's answer must appear EXACTLY ONCE; a second part is the #736 "
        f"double regression (responder derived from adopt was {adopt_responder!r}): "
        f"{answer_texts!r}"
    )
    # No-silent-fallback: the suppressed batch copy is recorded, not swallowed.
    assert len(fallback_ignored) == 1
    audit = fallback_ignored[0]
    assert audit["reason"] == "already_streamed"
    assert audit["agent_id"] == "synthesis"
    assert audit["field"] == "answer"


def test_seam_doubles_when_responder_is_not_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHARACTERIZATION (not a fix-lock): responder ≠ child → the #736 double.

    This documents the seam MECHANISM the fix relies on: when the finalize
    channel is authored to an agent that did NOT land the answer, its ``covers``
    set finds nothing, the batch fallback lands, and the child part + the
    finalize restatement are two real parts. The responder here is the
    ``pre_adopt`` value derived from the parent's OWN routing prediction (the
    value that survives if ``turn_terminal.py`` line 129 stops flipping) — not a
    bare literal, but this test passes with or without the fix, so it is a
    mechanism characterization, NOT the regression lock. The lock is
    :func:`test_finalize_collapses_when_adopt_flips_selected_expert`.
    """

    deliverable, _adopt, pre_adopt_responder = _adopt_and_derive_responder(
        parent_selected_expert="main", invocation_agent_id="main"
    )
    # The counterfactual responder is the parent's un-flipped attribution.
    assert pre_adopt_responder == "main"
    answer_texts, fallback_ignored = _run_finalize_seam(
        pre_adopt_responder, "main", str(deliverable.answer or ""), monkeypatch
    )

    assert answer_texts == [_CHILD_ANSWER, _CHILD_ANSWER], (
        "un-flipped attribution must reproduce the #736 double (child part + "
        f"restatement): {answer_texts!r}"
    )
    # The double is NOT flagged as a suppressed fallback — it is two real parts.
    assert fallback_ignored == []


def test_the_flip_is_what_collapses_the_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIFFERENTIAL LOCK (teeth): the ONLY difference is adopt's selected_expert flip.

    From ONE parent prediction and ONE real ``adopt`` call, derive both the
    adopt-attributed responder and the pre-adopt responder, then drive the SAME
    seam with each. The adopt-attributed responder collapses to one part; the
    pre-adopt responder doubles. Because both responders come from the same
    ``_adopt_and_derive_responder`` call, they differ ONLY by the
    ``turn_terminal.py`` line 129 flip — so this binds the collapse to that exact
    mutation.

    If line 129 is reverted, ``adopt_responder == pre_adopt_responder == "main"``,
    the ``adopt`` branch doubles, and the ``adopt_texts == [_CHILD_ANSWER]``
    assertion FAILS.
    """

    deliverable, adopt_responder, pre_adopt_responder = _adopt_and_derive_responder(
        parent_selected_expert="main", invocation_agent_id="main"
    )
    fallback = str(deliverable.answer or "")

    # Post-fix: adopt flipped selected_expert -> responder is the child -> collapse.
    assert adopt_responder == "synthesis"
    adopt_texts, adopt_ignored = _run_finalize_seam(adopt_responder, "main", fallback, monkeypatch)
    assert adopt_texts == [_CHILD_ANSWER]
    assert len(adopt_ignored) == 1

    # Counterfactual (line 129 removed): parent's own attribution -> double.
    pre_texts, pre_ignored = _run_finalize_seam(pre_adopt_responder, "main", fallback, monkeypatch)
    assert pre_texts == [_CHILD_ANSWER, _CHILD_ANSWER]
    assert pre_ignored == []

    # The collapse is driven by the flip and nothing else.
    assert adopt_responder != pre_adopt_responder
