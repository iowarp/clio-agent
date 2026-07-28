"""Plan-mode reminder attachment + plan-file lifecycle — the per-turn plan-mode contract.

Owner module for the plan-mode PROMPT surface (P1.2 #1064) and the plan-file lifecycle
(P1.3 #1065). ``session.mode`` used to be plumbed to every agent ``forward()`` and then
discarded (``builders.py``), so the model got ZERO signal it was in plan mode — and any signal
placed in the system prompt is lost once the KV-cache prefix is compacted. This module
re-injects the plan-mode contract into the model's *turn input* each turn (exactly like
``enrichment.inject_pending_agent_task_notifications`` re-injects observe-later results), so it
survives compaction without invalidating the prefix.

**Plan-file lifecycle (P1.3 #1065).** clio owns the plan file's PATH, EXISTENCE, and GUIDANCE —
the MODEL authors its content. On the first plan-mode turn a deterministic path
(:func:`_compute_plan_file_path`) is computed under ``grant_resolver.plans_dir()`` (a slug of the
session's first user prompt/title, session-id-suffixed for per-session uniqueness) and RECORDED on
``session.metadata`` (:data:`_PLAN_FILE_METADATA_KEY`) via ``sessions.update(metadata_patch=…)`` —
the #948 ``AgentTask`` no-fifth-store projection pattern, NOT ``workflow_state``, NOT a new store.
The recorded path always falls within the P1.1 plan-ACL carve-out glob (``<plans>/*.md``) so the
model can actually write it via ``fs_apply_edit_write`` (asserted at computation). Because the path
is recorded once and re-read thereafter, it is STABLE across turns regardless of derivation source.
clio never pre-writes the file's content; the reminder only points the model at the path.

Kept LEAN so plan mode never bloats context: a FULL reminder (create-vs-edit branch, adaptive
structure hint, epistemic-ledger headers, staleness + show-the-plan rules) is injected only on the
first plan turn, immediately after a compaction (which drops the earlier one from the model's
view), and once per :data:`_PLAN_REMINDER_FULL_INTERVAL` turns; every turn in between carries a
single-line marker. The tiny suppression counter lives on ``session.metadata`` alongside the plan
path.

The block is SERVER grounding prepended to the turn input — never user text, never model output
— so it carries a stable, greppable marker (:data:`PLAN_MODE_REMINDER_MARKER`, the #881 marker
discipline). ``turn.py`` calls :func:`inject_plan_mode_reminder` from its enrichment step.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Marker heading the plan-mode reminder block (stable + greppable; #881 discipline).
PLAN_MODE_REMINDER_MARKER = "## Plan Mode active — read-only except the plan file"

#: Re-inject the FULL reminder at most once per this many plan-mode turns (a sparse one-liner
#: in between); a compaction forces a full re-inject immediately regardless of the window.
_PLAN_REMINDER_FULL_INTERVAL = 10

#: ``session.metadata`` key holding the tiny per-session suppression counter (no fifth store).
_PLAN_REMINDER_STATE_KEY = "plan_mode_reminder"

#: ``session.metadata`` key holding the deterministic plan-file path (P1.3 #1065; no fifth store).
_PLAN_FILE_METADATA_KEY = "plan_file"

#: Cap the prompt/title-derived slug component so a long first prompt cannot bloat the filename.
_PLAN_SLUG_MAX_LEN = 60


def _session_compaction_count(app: "FastAPI", sid: str) -> int:
    """Return how many compaction summaries are in this session's live transcript.

    A compaction (``POST /v1/sessions/{sid}/compact`` or the auto path) appends an assistant
    message carrying a single ``compaction`` part (SPEC §4.5). Counting those parts gives a
    monotonic "a compaction happened since the last full plan reminder" signal — the trigger to
    re-inject the full contract (it would otherwise have been compacted out of the model's view).
    """

    count = 0
    for msg in app.state.messages.get(sid, []) or []:
        for part in getattr(msg, "parts", []) or []:
            if getattr(part, "type", "") == "compaction":
                count += 1
    return count


def _slugify(text: str) -> str:
    """Return a filesystem-safe slug (lowercase alnum + hyphens) of ``text`` (empty if none).

    Collapses every non-alphanumeric run to a single hyphen and trims edge hyphens, so the result
    can carry NO path separator, ``..``, or extension dot — the property that keeps a derived plan
    filename provably inside the plans dir (it cannot traverse out of the carve-out).
    """

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:_PLAN_SLUG_MAX_LEN].strip("-")


def _first_user_text(app: "FastAPI", sid: str) -> str:
    """Return the first user message's text for ``sid`` (empty when none is recorded yet)."""

    for msg in app.state.messages.get(sid, []) or []:
        if str(getattr(msg, "role", "") or "") != "user":
            continue
        text = " ".join(
            part.text.strip()
            for part in getattr(msg, "parts", []) or []
            if getattr(part, "type", "") == "text" and str(getattr(part, "text", "") or "").strip()
        ).strip()
        if text:
            return text
    return ""


def _compute_plan_file_path(app: "FastAPI", sid: str, session: Any) -> Path:
    """Compute the deterministic plan-file path for a session (P1.3 #1065).

    ``<plans>/<slug>.md`` where ``slug`` derives from the session's first user prompt (falling back
    to the session title, then the session id) and is suffixed with the session-id tail so two
    sessions sharing a first prompt never collide on one file. The path is asserted to fall within
    the P1.1 plan-ACL carve-out glob (``<plans>/*.md``) so the model can write it in plan mode.
    """

    from clio_agent.gact.runtime.grant_resolver import plans_dir  # noqa: PLC0415

    raw_sid = str(getattr(session, "id", "") or sid)
    sid_tail = _slugify(raw_sid.rsplit("_", 1)[-1]) or "session"
    base_slug = _slugify(_first_user_text(app, sid) or str(getattr(session, "title", "") or ""))
    slug = f"{base_slug}-{sid_tail}" if base_slug else sid_tail

    plans = plans_dir()
    path = (plans / f"{slug}.md").resolve(strict=False)
    plan_glob = f"{plans}{os.sep}*.md"
    assert fnmatch.fnmatchcase(str(path), plan_glob), (  # noqa: S101 - carve-out invariant guard
        f"computed plan-file path {path!r} escapes the plan-ACL carve-out {plan_glob!r}"
    )
    return path


def recorded_plan_file(session: Any) -> str | None:
    """Return the plan-file path recorded on ``session.metadata`` (``None`` when unset)."""

    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, Mapping):
        value = metadata.get(_PLAN_FILE_METADATA_KEY)
        if isinstance(value, str) and value:
            return value
    return None


def plan_file_exists(session: Any) -> bool:
    """Return whether the plan file recorded for ``session`` exists on disk (P1.3 #1065)."""

    path = recorded_plan_file(session)
    return path is not None and Path(path).exists()


def _plan_mode_reminder_block(*, full: bool, plan_file: str, exists: bool) -> str:
    """Compose the plan-mode reminder block (full contract or sparse one-liner).

    The FULL block carries the create-vs-edit branch (keyed on ``exists``), an adaptive-structure
    hint, the epistemic-ledger headers, the re-entry staleness note, the show-the-plan rule, the
    read-only restriction, and the turn-ending contract. The SPARSE block is a single line naming
    the restriction + the recorded plan path, so most turns cost almost nothing (do not bloat
    context). ``plan_file`` is the deterministic per-session path recorded on ``session.metadata``.
    """

    if not full:
        return (
            PLAN_MODE_REMINDER_MARKER
            + f" ({plan_file}). Keep writing your plan there; end your turn to hand it back "
            "for approval rather than executing it yourself."
        )
    if exists:
        create_or_edit = (
            f"A plan file already exists at {plan_file}. Make incremental edits to it as you learn."
        )
    else:
        create_or_edit = (
            f"No plan file exists yet. Create your plan at {plan_file} (write a *.md there — it is "
            "the ONLY writable path in plan mode)."
        )
    return (
        PLAN_MODE_REMINDER_MARKER
        + "\n\n"
        "You are in PLAN MODE. Investigate freely, but do NOT modify the system: every write, "
        "edit, and file-mutating tool is blocked.\n"
        f"- {create_or_edit}\n"
        "- Structure the plan to fit the task: Simple change → Changes + Verification; Standard "
        "task → Objective, Key Files & Context, Implementation Steps, Verification; Complex / "
        "architectural → Background, Scope, Proposed Solution, Alternatives, a phased Plan, "
        "Verification, Migration/Rollback.\n"
        "- Keep an epistemic ledger of what you know vs. must find out, under the headers: "
        "Given / Learned / To look up / To derive.\n"
        "- If a plan already exists, evaluate whether it is still relevant to THIS task before "
        "editing; treat a new task as a fresh plan.\n"
        "- Show the plan to the user in your response — don't just write it to disk.\n"
        "- Turn-ending contract: when the plan is complete, END YOUR TURN and hand it back for "
        "approval — do NOT try to execute the plan while in plan mode."
    )


def inject_plan_mode_reminder(app: "FastAPI", sid: str, session: Any, enriched_text: str) -> str:
    """Prepend the plan-mode reminder to this turn's input when the session is in plan mode.

    Returns ``enriched_text`` unchanged for every non-plan mode (edit/architect) — the
    attachment is scoped to ``plan`` for now (P1.2). In plan mode it prepends a reminder block
    and advances a tiny suppression counter on ``session.metadata`` so the FULL contract is
    injected on the first plan turn, immediately after any compaction, and once per
    :data:`_PLAN_REMINDER_FULL_INTERVAL` turns; a one-line marker is injected on every turn in
    between. Because it rides the per-turn input (not the system prompt), it survives compaction
    without invalidating the KV-cache prefix — the fix for "plan mode lost after compaction".
    """

    mode = str(getattr(session, "mode", "") or "")
    if mode != "plan":
        return enriched_text

    metadata = getattr(session, "metadata", None)
    state = metadata.get(_PLAN_REMINDER_STATE_KEY) if isinstance(metadata, Mapping) else None
    first_time = not isinstance(state, Mapping)

    # P1.3 #1065: compute the deterministic plan-file path ONCE and record it on session.metadata
    # (no fifth store). Thereafter the recorded path is re-read, so it is stable across turns.
    metadata_patch: dict[str, Any] = {}
    plan_file = recorded_plan_file(session)
    if plan_file is None:
        plan_file = str(_compute_plan_file_path(app, sid, session))
        metadata_patch[_PLAN_FILE_METADATA_KEY] = plan_file
    exists = Path(plan_file).exists()

    compactions = _session_compaction_count(app, sid)
    prev_turn = int(state.get("turn_index", 0)) if isinstance(state, Mapping) else 0
    last_full = int(state.get("last_full_turn", 0)) if isinstance(state, Mapping) else 0
    last_full_compactions = (
        int(state.get("compactions_at_last_full", 0)) if isinstance(state, Mapping) else 0
    )
    turn_index = prev_turn + 1

    compacted_since_full = compactions > last_full_compactions
    window_elapsed = (turn_index - last_full) >= _PLAN_REMINDER_FULL_INTERVAL
    full = first_time or compacted_since_full or window_elapsed

    metadata_patch[_PLAN_REMINDER_STATE_KEY] = {
        "turn_index": turn_index,
        "last_full_turn": turn_index if full else last_full,
        "compactions_at_last_full": compactions if full else last_full_compactions,
    }
    app.state.sessions.update(sid, metadata_patch=metadata_patch)
    return (
        _plan_mode_reminder_block(full=full, plan_file=plan_file, exists=exists)
        + "\n\n---\n\n"
        + enriched_text
    )
