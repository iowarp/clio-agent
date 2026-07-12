"""clio ReActV2 subclass — dormant infrastructure behind the kill-switch (#901 S1).

This is the *minimal viable* clio subclass of dspy's (experimental) ``ReActV2``.
It exists so the OFF-by-default kill-switch in
:func:`clio_agent.gact.agents.runtime._retaining_react_cls` can select a V2 class
*without changing any production behaviour* — the classic ``_RetainingReAct`` stays
the production path until parity is proven (design slices S2–S6).

**Why V2 at all** (design ``901_reactv2_design.md`` §1–3): ReActV2 composes each
turn as an append-only ``dspy.History`` of structured messages instead of one
ever-growing re-rendered ``trajectory`` string, so a provider's prompt-cache sees a
byte-stable prefix across ``self.react`` calls (the #891 payoff). clio's own ARC
compact/delete op becomes the *sole* prefix-reset author (V2 has no
``truncate_trajectory``).

**The one behavioural override S1 carries** — the frozen-contract defense (§4,
risk 1): clio's ReAct-internal ``next_thought`` field is typed plain ``str``, NOT
``dspy.Reasoning``. ``dspy.Reasoning.adapt_to_native_lm_feature`` deletes the
Reasoning field from the signature and sets ``reasoning_effort`` on any
reasoning-capable model, which would route ``next_thought`` onto the provider's
*native reasoning channel* (clio's thinking lane) instead of the visible
``[[ ## next_thought ## ]]`` response lane — inverting the frozen wire contract
(thinking = provider CoT ONLY; next_thought = response). Typing it ``str`` keeps it
a text-rendered field and leaves the #877 marker-split path unchanged.

Everything else is inherited from ReActV2 unchanged. In particular the internal
``submit`` tool's typed args are ``self.signature.output_fields`` verbatim
(``ReActV2._make_submit_tool``), so clio's load-bearing typed ``workflow_state``
output field rides ``submit``'s ``arg_types`` automatically once the builder injects
it into the user signature (design fact 4) — no extra wiring needed here.

**Deliberately NOT wired in S1**: the ARC live-plane segments→messages fold (S2),
the #878 submit-turn contract rework (S3), and the retention/repair hooks (S4).

**Import placement**: ``dspy`` is imported at module scope on purpose. The rest of
the ``agents`` package keeps dspy off the boot path by importing it lazily inside
functions; this module preserves that property differently — it is itself
*deferred*: nothing imports it at package load, only :func:`retaining_reactv2_cls`
(reached from ``_retaining_react_cls`` when the kill-switch is ON, default OFF) or an
explicit test import pulls it in. Defining the subclass at module scope keeps it a
real, importable, testable unit (no hidden class in a function — the ``ast`` guard
``scripts/check_no_class_in_function.py`` stays at zero for this file).
"""

from __future__ import annotations

from typing import Any

import dspy


class _RetainingReActV2(dspy.ReActV2):  # type: ignore[misc, name-defined]
    """clio subclass of dspy's experimental ``ReActV2`` (see module docstring)."""

    def _make_react_signature(self) -> type[dspy.Signature]:
        """Build the ReAct-internal predict signature, retyping ``next_thought``.

        Mirrors ``dspy.predict.react_v2.ReActV2._make_react_signature`` and then
        applies the single frozen-contract change: ``next_thought`` becomes plain
        ``str`` instead of ``dspy.Reasoning`` (see the class/module docstring). Using
        the public :meth:`dspy.Signature.with_updated_fields` — rather than
        re-authoring the whole method body — keeps clio in lockstep with upstream:
        only the field *type* is overridden, every other field, instruction, and
        ordering flows through unchanged. A unit test guards the resulting field
        types / signature shape.
        """
        signature = super()._make_react_signature()
        return signature.with_updated_fields("next_thought", type_=str)


def retaining_reactv2_cls() -> type[Any]:
    """Return the clio ReActV2 subclass — the V2 leg of ``_retaining_react_cls``.

    Parallels :func:`clio_agent.gact.agents.runtime._retaining_react_cls`'s classic
    factory so the kill-switch is a single branch. The class is a module-level
    constant (no per-base cache is needed: unlike the classic path, clio binds
    directly to the concrete ``dspy.ReActV2`` here rather than a test-monkeypatched
    ``dspy.ReAct``).
    """
    return _RetainingReActV2
