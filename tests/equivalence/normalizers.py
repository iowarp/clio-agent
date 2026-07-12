"""Per-surface normalizers + a precise first-divergence differ (design §4.1.A).

A normalizer maps a surface's raw stored/served form onto the canonical shape at
which "equivalent" is defined: it MASKS non-normative fields (wall-clock, ids,
costs) to a sentinel and EXCLUDES non-logged transport rows, so a byte diff of two
normalized forms is a *semantic* equivalence check. Anything a normalizer masks is,
by construction, declared non-normative — and the report says WHAT was masked so a
clean diff is auditable (the plan's open question, §5.2 Q-determinism).

Nothing here decides routing/completion or scrubs content (⚑ #1/#2): masking is a
mechanical field-level projection with a fixed, declared mask set. The differ is the
gate's teeth — it returns the FIRST field-path where two normalized forms diverge,
so a one-byte projection bug is reported as ``messages[3].parts[1].text`` rather
than a bare ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from clio_agent.gact.types import Message

# --------------------------------------------------------------------------- #
# Masking sentinels + declared non-normative field sets (§4.1.A)
# --------------------------------------------------------------------------- #

MASKED = "█masked█"  # a sentinel no real payload produces (full-block char)

#: SSE fields masked on every event (wall-clock, ids, costs — non-normative per
#: SPEC §7.4a: ``final_text`` is authoritative, these are advisory).
SSE_MASK_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "event_id",
        "created_at",
        "updated_at",
        "occurred_at",
        "duration_ms",
        "tokens",
        "cost_usd",
    }
)

#: SSE event types EXCLUDED from the normalized diff entirely — non-logged bus/
#: transport rows (§2.8: the resume buffer is a transport plane, not a log
#: projection) and non-normative timing rows.
SSE_EXCLUDE_TYPES: frozenset[str] = frozenset(
    {
        "message.part.delta",  # coalesced into the completed part's final_text
        "server.heartbeat",
        "session.status_changed",
    }
)

#: SSE event-type PREFIXES excluded (permission gate + delegation timing rows).
SSE_EXCLUDE_PREFIXES: tuple[str, ...] = ("permission.", "delegate.")


@dataclass(frozen=True)
class Divergence:
    """The first place two normalized surfaces disagree.

    ``path`` is a consumer-readable field path (``.messages[3].parts[1].text``);
    ``left``/``right`` are the diverging values (reference vs candidate); ``reason``
    is a short machine tag (``value`` / ``length`` / ``keys`` / ``type``).
    """

    path: str
    left: Any
    right: Any
    reason: str

    def pretty(self) -> str:
        return (
            f"DIVERGENCE at {self.path or '<root>'} ({self.reason}):\n"
            f"    reference = {self.left!r}\n"
            f"    candidate = {self.right!r}"
        )


@dataclass
class DiffReport:
    """The result of diffing a reference surface against a candidate surface."""

    surface: str
    divergence: Optional[Divergence]
    masked_fields: list[str] = field(default_factory=list)
    excluded_types: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """True when reference and candidate are equivalent under the normalizer."""
        return self.divergence is None

    def pretty(self) -> str:
        head = f"[{self.surface}] "
        if self.empty:
            masked = ", ".join(self.masked_fields) or "(none)"
            excl = ", ".join(self.excluded_types) or "(none)"
            return f"{head}EMPTY diff. masked={masked} excluded={excl}"
        return head + "DIVERGENT\n" + self.divergence.pretty()


# --------------------------------------------------------------------------- #
# The differ — first field-path divergence (§4.1.A "not just a boolean")
# --------------------------------------------------------------------------- #


def first_divergence(left: Any, right: Any, path: str = "") -> Optional[Divergence]:
    """Return the FIRST field-path at which ``left`` and ``right`` differ, else None.

    Recurses dicts (compared by key set then per-key value) and lists (by length
    then elementwise). Assertions use the CONSUMER's value semantics — exact
    equality on the already-normalized structures (masking has removed the
    non-normative deltas upstream), so a surviving difference is a real one.
    """

    if type(left) is not type(right):
        # int/float are compared numerically (JSON round-trips 0 as 0/0.0).
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if left != right:
                return Divergence(path, left, right, "value")
            return None
        return Divergence(path, left, right, "type")

    if isinstance(left, dict):
        lkeys, rkeys = set(left), set(right)
        if lkeys != rkeys:
            missing = sorted(lkeys - rkeys)
            extra = sorted(rkeys - lkeys)
            return Divergence(
                path or "<root>",
                f"keys+{missing}" if missing else "keys",
                f"keys+{extra}" if extra else "keys",
                "keys",
            )
        for key in sorted(left):
            sub = first_divergence(left[key], right[key], f"{path}.{key}")
            if sub is not None:
                return sub
        return None

    if isinstance(left, list):
        if len(left) != len(right):
            return Divergence(path, len(left), len(right), "length")
        for i, (lv, rv) in enumerate(zip(left, right, strict=False)):
            sub = first_divergence(lv, rv, f"{path}[{i}]")
            if sub is not None:
                return sub
        return None

    if left != right:
        return Divergence(path, left, right, "value")
    return None


# --------------------------------------------------------------------------- #
# Generic masking
# --------------------------------------------------------------------------- #


def _mask(value: Any, mask_keys: frozenset[str]) -> Any:
    """Recursively replace any dict value whose key is in ``mask_keys`` with the
    sentinel, preserving all structure. Lists/dicts are rebuilt (pure)."""

    if isinstance(value, dict):
        return {
            k: (MASKED if k in mask_keys else _mask(v, mask_keys)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask(v, mask_keys) for v in value]
    return value


# --------------------------------------------------------------------------- #
# SSE surface (1.2 / §4.1.A)
# --------------------------------------------------------------------------- #


def bus_events_to_records(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Project bus ``Event`` objects (or dicts) into ``{type, payload}`` records."""

    records: list[dict[str, Any]] = []
    for ev in events:
        if isinstance(ev, dict):
            records.append({"type": ev.get("type", ""), "payload": ev.get("payload") or {}})
        else:
            records.append({"type": ev.type, "payload": ev.payload or {}})
    return records


def _sse_included(event_type: str) -> bool:
    if event_type in SSE_EXCLUDE_TYPES:
        return False
    return not event_type.startswith(SSE_EXCLUDE_PREFIXES)


def normalize_sse(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Coalesced, ``final_text``-authoritative, masked SSE stream (§4.1.A).

    Excludes non-logged/timing rows, masks the non-normative field set, and keeps
    the SERVED, LOGGED event order. ``message.part.delta`` rows are dropped because
    ``message.part.completed`` already carries the authoritative ``final_text`` (the
    coalescing is "trust the completed part", not "replay the deltas").
    """

    out: list[dict[str, Any]] = []
    for rec in bus_events_to_records(events):
        etype = rec["type"]
        if not _sse_included(etype):
            continue
        out.append({"type": etype, "payload": _mask(rec["payload"], SSE_MASK_FIELDS)})
    return out


def sse_present_types(events: Iterable[Any]) -> list[str]:
    """The SORTED distinct set of served, logged event types present in a stream.

    This is the DROP-DETECTION signal (1.1): comparing a reference stream's present
    set against a candidate's fails the moment the candidate *suppresses* a type —
    it is presence-of-each-expected-type, not set-equality of payloads.
    """

    seen = {rec["type"] for rec in bus_events_to_records(events) if _sse_included(rec["type"])}
    return sorted(seen)


def diff_sse(reference: Iterable[Any], candidate: Iterable[Any]) -> DiffReport:
    """Diff two SSE streams: FIRST assert type-presence (drop-detection), THEN the
    coalesced/masked payload stream. A suppressed type fails on the presence check
    with a clear message even if the surviving payloads happen to align."""

    ref_types = sse_present_types(reference)
    cand_types = sse_present_types(candidate)
    if ref_types != cand_types:
        dropped = sorted(set(ref_types) - set(cand_types))
        added = sorted(set(cand_types) - set(ref_types))
        return DiffReport(
            "sse",
            Divergence(
                "event_type_presence",
                f"present={ref_types}",
                f"present={cand_types} (dropped={dropped} added={added})",
                "drop_detection",
            ),
            masked_fields=sorted(SSE_MASK_FIELDS),
            excluded_types=sorted(SSE_EXCLUDE_TYPES) + [p + "*" for p in SSE_EXCLUDE_PREFIXES],
        )
    div = first_divergence(normalize_sse(reference), normalize_sse(candidate))
    return DiffReport(
        "sse",
        div,
        masked_fields=sorted(SSE_MASK_FIELDS),
        excluded_types=sorted(SSE_EXCLUDE_TYPES) + [p + "*" for p in SSE_EXCLUDE_PREFIXES],
    )


# --------------------------------------------------------------------------- #
# Persistence surface (1.3 / §4.1.A) — Message(**payload).to_wire()
# --------------------------------------------------------------------------- #

#: Server-assigned message-envelope fields masked ONLY for cross-run (dual-run)
#: comparison, where two independent turns mint different ids/timestamps. For the
#: captured-corpus sweep this is EMPTY — ids/parts/output are normative, verbatim.
PERSISTENCE_XRUN_MASK: frozenset[str] = frozenset(
    {"id", "session_id", "turn_id", "created_at", "updated_at", "call_id", "part_id"}
)


def normalize_persistence(
    rows: Iterable[dict[str, Any]], *, mask: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Re-parse each stored payload through ``Message(**payload).to_wire()``.

    This is the ONLY correct persistence normalizer: the on-disk file is
    ``model_dump(exclude_none=True)`` + sorted-indent JSON (``messages.py``), a
    DIFFERENT serialization from the served ``to_wire()``. Diffing raw file bytes is
    a false-negative machine; re-parsing to the served projection is the real gate.

    ``mask`` masks server-assigned envelope fields (see ``PERSISTENCE_XRUN_MASK``);
    default empty for the byte-verbatim corpus sweep.
    """

    wired = [Message(**payload).to_wire() for payload in rows]
    if mask:
        return [_mask(w, mask) for w in wired]
    return wired


def diff_persistence(
    reference: Iterable[dict[str, Any]],
    candidate: Iterable[dict[str, Any]],
    *,
    mask: frozenset[str] = frozenset(),
) -> DiffReport:
    """Diff two message ledgers under the re-parse normalizer."""

    div = first_divergence(
        normalize_persistence(reference, mask=mask),
        normalize_persistence(candidate, mask=mask),
    )
    return DiffReport("persistence", div, masked_fields=sorted(mask))


# --------------------------------------------------------------------------- #
# Context surface (1.4 / §4.1.A) — byte-identical render_working_set
# --------------------------------------------------------------------------- #


def normalize_context(segments: Iterable[Any]) -> list[dict[str, Any]]:
    """Project working-set segments to the ordered ``(kind, content)`` render.

    Byte-exact and UNMASKED — this is an internal surface (no wire), so the render
    order + content is the whole contract. Segment identity/clock (id/logical_time/
    created_at) are NOT part of the render and are intentionally dropped by taking
    only ``(kind, content)``.
    """

    out: list[dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, dict):
            out.append({"kind": seg.get("kind"), "content": seg.get("content")})
        else:
            out.append({"kind": seg.kind, "content": seg.content})
    return out


def diff_context(reference: Iterable[Any], candidate: Iterable[Any]) -> DiffReport:
    """Diff two working-set renders byte-for-byte (no masking)."""

    div = first_divergence(normalize_context(reference), normalize_context(candidate))
    return DiffReport("context", div)


# --------------------------------------------------------------------------- #
# Trace surface (1.6 / §4.1.A) — replay-to-T == live-at-T, mask occurred_at/event_id
# --------------------------------------------------------------------------- #


def normalize_trace(segments: Iterable[Any]) -> list[dict[str, Any]]:
    """Project log segments to the replay-comparable shape, masking the clock.

    ``occurred_at`` (``created_at``) and ``event_id`` (``id``) are masked; the
    ordered ``(kind, content, order)`` triple is the trace's semantic content. The
    trace view = the log with operations VISIBLE, so — unlike the context view —
    tombstoned records are the caller's to include or not (via ``as_of``); this
    normalizer compares whatever segment list it is handed.
    """

    out: list[dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, dict):
            out.append(
                {
                    "kind": seg.get("kind"),
                    "content": seg.get("content"),
                    "order": seg.get("order"),
                    "status": seg.get("status", "live"),
                    "occurred_at": MASKED,
                    "event_id": MASKED,
                }
            )
        else:
            out.append(
                {
                    "kind": seg.kind,
                    "content": seg.content,
                    "order": seg.order,
                    "status": seg.status,
                    "occurred_at": MASKED,
                    "event_id": MASKED,
                }
            )
    return out


def diff_trace(reference: Iterable[Any], candidate: Iterable[Any]) -> DiffReport:
    """Diff two trace renders (e.g. replay-to-T vs live-at-T), clock masked."""

    div = first_divergence(normalize_trace(reference), normalize_trace(candidate))
    return DiffReport("trace", div, masked_fields=["occurred_at", "event_id"])
