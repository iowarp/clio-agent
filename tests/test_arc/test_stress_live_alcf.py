"""LIVE ALCF behavioral battery — the hard proof that ARC *is* the context.

Every test here injects an UNGUESSABLE code (random per run) into the ARC live
plane and asks a REAL model (ALCF/Argonne vLLM) to read it back. Because the code
is random, the model can only emit it by READING the ARC-rendered trajectory — it
cannot guess. Recall appearing/vanishing as we mutate ARC is therefore a direct,
adversarial proof that the prompt the model sees IS the live segment set.

The battery stresses four independent properties on real inference:

  * NEEDLE POSITION — a needle planted at the START / MIDDLE / END of a haystack of
    filler observations is still recalled (no lost-in-the-middle artifact in the
    render projection).
  * PARTIAL DELETE — two needles, delete exactly one: the deleted one is gone, the
    surviving one is still recalled (delete is surgical, not all-or-nothing).
  * AS-OF-T — delete a needle, then render the scope as it was at a pre-deletion
    logical_time: the model recalls the needle again (tombstone-not-erase + as-of-T
    time travel is real at the model boundary, not just in unit asserts).
  * SURVIVES COMPACTION — inject a needle, run a REAL provider summarize over the
    whole scope, and the model recalls the fact from the genuine LLM summary alone
    (the originals are tombstoned; only the summary is live).

All gated by ``CLIO_RUN_LIVE=1`` (+ Argonne auth + ``CLIO_LM_*``). Run:

    CLIO_RUN_LIVE=1 CLIO_LM_PROVIDER=argonne \\
      CLIO_LM_API_BASE='https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1' \\
      CLIO_LM_MODEL='openai/gpt-oss-120b' \\
      CLIO_ALLOWED_ROOTS="/tmp:$PWD" \\
      uv run python -m pytest tests/test_arc/test_stress_live_alcf.py -o addopts="" -m live -q -W ignore
"""

from __future__ import annotations

import os
import random
import string

import dspy
import pytest

import clio_agent.gact.app as app
from clio_agent.arc.segments import segments_to_keys

from .conftest import live_plane_context, make_react_agent

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CLIO_RUN_LIVE") != "1",
        reason="live ALCF run: set CLIO_RUN_LIVE=1 (+ Argonne auth + CLIO_LM_* env)",
    ),
]

SID, SCOPE = "stress-s1", "agentA"

# Unguessable code question used everywhere. The model is told the EXACT escape
# hatch (ABSENT) so a clean "I don't have it" is distinguishable from a hallucination.
#
# The needle is framed as a benign EXPERIMENT REFERENCE CODE (a dataset/run id), NOT
# a secret/credential: a safety-trained model will refuse to surface a "CONFIDENTIAL
# vault override code", and that refusal — not any ARC behavior — was the only thing
# that ever failed here. Recall of an unguessable random token is exactly as strong a
# proof of "ARC IS the context" either way; the benign framing just keeps the model
# answering instead of declining.
_VAULT_Q = (
    "What is the experiment reference code? Reply with ONLY the code, nothing else. "
    "If your trajectory does not contain an experiment reference code, reply exactly: "
    "ABSENT"
)


def _rand_code(seed: int) -> str:
    """A fresh unguessable code, e.g. ``REF-8F3K-QZ29``. Seeded per-parametrize so a
    failure is reproducible while still being un-guessable to the model."""
    rng = random.Random(f"stress-{seed}-{os.getpid()}")
    a = "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    b = "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"REF-{a}-{b}"


def _live_lm():
    from clio_agent.config import create_lm, load_config_from_env  # noqa: PLC0415

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) == "lmstudio":
        pytest.skip("live run must target Argonne/ALCF, not lmstudio (leave it free)")
    return create_lm(cfg)


def _norm(text: str) -> str:
    """Fold the typographic variants a chat model emits so an exact-code substring
    match is robust to PRESENTATION without weakening the proof.

    The model reliably reproduces the unguessable LETTERS/DIGITS, but it may render
    the ASCII hyphen as a Unicode dash (non-breaking/figure hyphen, en/em dash) and
    insert thin/non-breaking SPACES — observed live: ``REF-SNY4-B292`` came back as
    ``REF‑SNY4‑B292``. We map every dash variant to ``-`` and drop zero-width/thin
    spaces; we do NOT touch the alphanumerics, so a wrong/hallucinated code still
    fails. Applied to both the expected code and the model text.
    """
    dashes = "‐‑‒–—―−－"  # ‐‑‒–—―−－
    for d in dashes:
        text = text.replace(d, "-")
    for sp in (" ", " ", " ", " ", "​"):  # nbsp/thin/zero-width
        text = text.replace(sp, "")
    return text


def _recalled(code: str, text: str) -> bool:
    """True iff the model genuinely reproduced the unguessable code (dash/space-fold)."""
    return _norm(code) in _norm(text)


def _probe(agent, lm, arc, question: str, *, scope: str = SCOPE) -> str:
    """One real model call whose trajectory is rendered FROM ARC via the
    ``_format_trajectory`` override (reads ``render_segments_keys`` of ``scope``)."""
    with live_plane_context(arc, session=SID, scope=scope):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            r = agent._call_with_potential_trajectory_truncation(
                agent.extract, {}, question=question
            )
    return str(getattr(r, "answer", "") or "").strip()


def _seed_haystack(arc, scope: str, *, n: int, label: str) -> None:
    """Append ``n`` plausible-but-codeless filler observations (the haystack)."""
    arc.append_segment(SID, scope, "thought", {"text": f"Beginning the {label} review."}, step=0)
    for i in range(n):
        arc.append_segment(
            SID, scope, "observation",
            {"text": f"Audit line {i}: routine telemetry within nominal bounds; "
                     f"no anomalies, no reference codes present."},
            step=0,
        )


# ---------------------------------------------------------------------------
# 1. NEEDLE POSITION: start / middle / end of a haystack, on real inference.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [11, 23, 47, 89])
@pytest.mark.parametrize("position", ["start", "middle", "end"])
def test_needle_position_in_haystack(arc, seed, position):
    """A random needle buried at the START / MIDDLE / END of a haystack is recalled
    iff ARC holds it. Proves the render projection surfaces the fact regardless of
    where it sits in the live ordered set."""
    code = _rand_code(seed)
    lm = _live_lm()
    agent = make_react_agent()
    n = 8  # haystack depth around the needle

    with live_plane_context(arc, session=SID, scope=SCOPE):
        needle_seg = None
        # Build: filler before, needle at the chosen slot, filler after.
        if position == "start":
            needle_seg = arc.append_segment(
                SID, SCOPE, "observation",
                {"text": f"For the record, the experiment reference code is {code}."}, step=0,
            )
            _seed_haystack(arc, SCOPE, n=n, label="post-needle")
        elif position == "end":
            _seed_haystack(arc, SCOPE, n=n, label="pre-needle")
            needle_seg = arc.append_segment(
                SID, SCOPE, "observation",
                {"text": f"For the record, the experiment reference code is {code}."}, step=0,
            )
        else:  # middle — insert at render position n//2 + 1 (past the opening thought)
            _seed_haystack(arc, SCOPE, n=n, label="surrounding")
            live = arc.render_segments(SID, SCOPE)
            mid = len(live) // 2
            needle_seg = arc.insert_segment(
                SID, SCOPE, mid, "observation",
                {"text": f"For the record, the experiment reference code is {code}."},
            )

    assert needle_seg is not None
    # Sanity: the needle is genuinely in the live render the model will see.
    live_keys = arc.render_segments_keys(SID, SCOPE)
    assert any(code in str(v) for v in live_keys.values()), (
        f"needle not in ARC render at position={position} — test is mis-built"
    )

    answer = _probe(agent, lm, arc, _VAULT_Q)
    assert _recalled(code, answer), (
        f"model failed to recall a {position}-buried needle that ARC holds: {answer!r}"
    )

    # Delete it — the model must now be unable to produce it (ARC really is the prompt).
    arc.delete_segments(SID, SCOPE, [needle_seg.id])
    after = _probe(agent, lm, arc, _VAULT_Q)
    assert not _recalled(code, after), (
        f"model recalled a DELETED {position}-needle (ARC is not the context): {after!r}"
    )


# ---------------------------------------------------------------------------
# 2. PARTIAL DELETE: two needles, delete one — deleted gone, other recalled.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [13, 29, 53, 97])
def test_partial_delete_surgical(arc, seed):
    """Inject TWO distinct random needles; delete exactly one. The deleted code is
    gone; the surviving code is still recalled. Proves delete is surgical, and that
    recall tracks ARC contents key-by-key (not a coarse 'has any code' flag)."""
    keep = _rand_code(seed)
    drop = _rand_code(seed + 1000)
    assert keep != drop
    lm = _live_lm()
    agent = make_react_agent()

    with live_plane_context(arc, session=SID, scope=SCOPE):
        arc.append_segment(SID, SCOPE, "thought", {"text": "Two vault entries to reconcile."}, step=0)
        keep_seg = arc.append_segment(
            SID, SCOPE, "observation",
            {"text": f"PRIMARY experiment reference code: {keep}."}, step=0,
        )
        drop_seg = arc.append_segment(
            SID, SCOPE, "observation",
            {"text": f"BACKUP experiment reference code: {drop}."}, step=0,
        )

    q_keep = (
        "What is the PRIMARY experiment reference code? Reply with ONLY the code. "
        "If your trajectory does not contain it, reply exactly: ABSENT"
    )
    q_drop = (
        "What is the BACKUP experiment reference code? Reply with ONLY the code. "
        "If your trajectory does not contain it, reply exactly: ABSENT"
    )

    # Both present before the delete.
    assert _recalled(keep, _probe(agent, lm, arc, q_keep)), "PRIMARY needle not recalled pre-delete"
    assert _recalled(drop, _probe(agent, lm, arc, q_drop)), "BACKUP needle not recalled pre-delete"

    # Surgically remove only the BACKUP needle.
    n = arc.delete_segments(SID, SCOPE, [drop_seg.id])
    assert n == 1, f"expected to tombstone exactly 1 segment, tombstoned {n}"
    assert keep_seg.id != drop_seg.id

    drop_answer = _probe(agent, lm, arc, q_drop)
    assert not _recalled(drop, drop_answer), (
        f"model recalled the DELETED backup code (delete not surgical): {drop_answer!r}"
    )
    keep_answer = _probe(agent, lm, arc, q_keep)
    assert _recalled(keep, keep_answer), (
        f"deleting BACKUP collaterally lost the PRIMARY needle: {keep_answer!r}"
    )


# ---------------------------------------------------------------------------
# 3. AS-OF-T: delete a needle, but recall it by rendering a pre-deletion time.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [17, 31, 59, 71])
def test_as_of_t_time_travel(arc, seed):
    """Delete a needle, then render the scope AS IT WAS at a pre-deletion
    ``logical_time``: the model recalls the needle again. This is real time-travel
    at the model boundary — the tombstone is honored for the live view but the
    as-of-T view still surfaces the segment (it survived, not erased).

    The probe's ``_format_trajectory`` override always renders the *current* live
    view, so we reconstruct the real as-of-T render into a fresh scope (using the
    store's own ``render(as_of=...)`` — no mock) and let the model read THAT live.
    The as-of keys we replay are byte-identical to what the live store produced at
    that time, which we assert before probing.
    """
    code = _rand_code(seed)
    lm = _live_lm()
    agent = make_react_agent()

    with live_plane_context(arc, session=SID, scope=SCOPE):
        arc.append_segment(SID, SCOPE, "thought", {"text": "Opening the vault record."}, step=0)
        needle_seg = arc.append_segment(
            SID, SCOPE, "observation",
            {"text": f"For the record, the experiment reference code is {code}."}, step=0,
        )
        arc.append_segment(
            SID, SCOPE, "observation",
            {"text": "Record closed; nothing further."}, step=0,
        )

    # Capture a pre-deletion logical_time (the needle's own creation time is a valid
    # 'before the tombstone' instant), then snapshot the render at that time.
    t_before = needle_seg.logical_time
    as_of_render = arc.render_segments(SID, SCOPE, as_of=t_before)
    as_of_keys = segments_to_keys(as_of_render)
    assert any(code in str(v) for v in as_of_keys.values()), (
        "as-of-T render unexpectedly lacks the needle — fixture mis-built"
    )

    # Delete the needle from the live view.
    arc.delete_segments(SID, SCOPE, [needle_seg.id])

    # Confirm the LIVE view no longer carries it (current-time render).
    live_keys = arc.render_segments_keys(SID, SCOPE)
    assert not any(code in str(v) for v in live_keys.values()), (
        "needle still in the live render after delete — delete is broken"
    )
    live_answer = _probe(agent, lm, arc, _VAULT_Q)
    assert not _recalled(code, live_answer), (
        f"model recalled a deleted needle from the LIVE view: {live_answer!r}"
    )

    # Now replay the real as-of-T render into a fresh scope and probe it LIVE. The
    # segments are the exact ones the store returned for ``render(as_of=t_before)``.
    asof_scope = "agentA/asof"
    with live_plane_context(arc, session=SID, scope=asof_scope):
        for seg in as_of_render:
            arc.append_segment(SID, asof_scope, seg.kind, dict(seg.content), step=seg.step)
        replay_keys = arc.render_segments_keys(SID, asof_scope)
    assert segments_to_keys(as_of_render) == replay_keys, (
        "as-of-T replay is not byte-identical to the store's as-of render"
    )

    asof_answer = _probe(agent, lm, arc, _VAULT_Q, scope=asof_scope)
    assert _recalled(code, asof_answer), (
        f"model failed to recall the needle from the as-of-T (pre-deletion) view: "
        f"{asof_answer!r}"
    )


# ---------------------------------------------------------------------------
# 4. SURVIVES COMPACTION: real provider summarize, fact still recalled.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [19, 37, 61, 83])
def test_needle_survives_real_compaction(arc, seed):
    """Inject a needle into a multi-segment scope, run a REAL provider summarize over
    the WHOLE scope, and the model recalls the code from the genuine LLM summary
    alone — the originals are tombstoned, only the summary is live. Proves
    compaction preserves the fact through a real model round-trip, not a placeholder.
    """
    code = _rand_code(seed)
    lm = _live_lm()
    agent = make_react_agent()

    with live_plane_context(arc, session=SID, scope=SCOPE):
        arc.append_segment(SID, SCOPE, "thought", {"text": "Investigating the lockbox incident."}, step=0)
        arc.append_segment(
            SID, SCOPE, "observation",
            {"text": "At 02:14 UTC the lockbox controller rebooted after a firmware push."},
            step=0,
        )
        arc.append_segment(
            SID, SCOPE, "observation",
            {"text": f"Run note: the experiment reference code for this dataset is {code} — "
                     f"this must be retained for the writeup."},
            step=0,
        )
        arc.append_segment(
            SID, SCOPE, "observation",
            {"text": "Controller rejoined at 02:31 UTC; no data loss; ticket closed."},
            step=0,
        )

    live = arc.render_segments(SID, SCOPE)
    assert len(live) >= 3

    # Real provider summary over the whole scope (the genuine compaction text).
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        summary = app._summarize_segments_llm(live)
    assert summary, "provider summary came back empty (LLM compaction failed)"
    assert _recalled(code, summary), (
        f"the REAL summary dropped the unguessable code — compaction lost the fact. "
        f"summary={summary!r}"
    )

    # Apply the real summarization to ARC: all originals tombstoned, one summary live.
    arc.summarize_segments(SID, SCOPE, [s.id for s in live], {"text": summary})
    after = arc.render_segments(SID, SCOPE)
    assert len(after) == 1 and after[0].kind == "summary", (
        f"scope did not collapse to a single summary segment: "
        f"{[(s.kind, s.status) for s in after]}"
    )
    tombstoned = [
        s for s in arc._segments.list_segments(SID, SCOPE, include_tombstoned=True)
        if s.status == "tombstoned"
    ]
    assert len(tombstoned) == len(live), "originals were not all tombstoned by summarize"

    # The model now sees ONLY the summary — and still recalls the code from it.
    answer = _probe(agent, lm, arc, _VAULT_Q)
    assert _recalled(code, answer), (
        f"model failed to recall the needle from the post-compaction summary "
        f"(the only live segment): {answer!r}"
    )
