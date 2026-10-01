"""A BestOfN / Refine run as clio-core records it (Phase 9).

Every run -- a blueprint or spawn-strategy variant, or ``draft_alternatives`` -- is one
:class:`VariantRun`: its tries (scope, final text, score, tokens), the judge's scores or
the user's pick and comment, the advice each Refine try was given, and what was
selected. Each state change appends one full snapshot (kind ``variant_record``) to the
session's ``_events/v`` lane in clio-core; the latest snapshot per ``variants_id`` is the
run. That is what a human-judged run pauses on and resumes from (it survives a
restart: nothing lives only in memory), and a finished run's last snapshot is its
preference record (:func:`preference_records`), the log a DSPy optimizer reads.

The lane is under ``_events``: search-excluded, never an agent context, erased with the
session's log. In the loud History mode the plane is the in-memory History plane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from clio_agent.errors import ClioError

__all__ = [
    "VARIANT_RECORD_KIND",
    "VARIANT_RECORD_SCOPE",
    "PreferenceCandidate",
    "PreferenceRecord",
    "TryRecord",
    "VariantRun",
    "VariantRunNotFoundError",
    "load_run",
    "preference_records",
    "save_run",
]

VARIANT_RECORD_SCOPE = "_events/v"
VARIANT_RECORD_KIND = "variant_record"
SCHEMA = "clio.variant_run.v1"

RunStatus = Literal["running", "awaiting_pick", "answered", "selected", "failed"]
TryStatus = Literal["running", "completed", "failed"]


class VariantRunNotFoundError(ClioError):
    """A run named by a question or a resume is not in clio-core."""

    reason = "variant_run_not_found"

    def __init__(self, session_id: str, variants_id: str) -> None:
        super().__init__(
            f"variant run {variants_id!r} is not recorded for session {session_id!r}",
            error_type=self.reason,
            details={"session_id": session_id, "variants_id": variants_id},
        )


@dataclass
class TryRecord:
    """One try of a run: its scope, what it produced and what it cost."""

    try_index: int
    scope: str
    status: TryStatus = "running"
    text: str = ""
    score: float | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    forked_from: int | None = None
    advice: str = ""
    error: str = ""
    # The ids of the conversation prefix this try was forked onto (its first
    # ``len(prefix_ids)`` segments): what follows them is the try's own line.
    prefix_ids: list[str] = field(default_factory=list)


@dataclass
class VariantRun:
    """The state of one BestOfN / Refine run (one snapshot of it)."""

    variants_id: str
    session_id: str
    agent_id: str
    turn_id: str
    origin: str  # "draft_alternatives" | "module_variant"
    strategy: str  # "best_of_n" | "refine"
    judge: str  # "lm" | "user"
    n: int
    n_requested: int
    rubric: str
    threshold: float | None = None
    status: RunStatus = "running"
    tries: list[TryRecord] = field(default_factory=list)
    pick: int | None = None
    comment: str = ""
    selected_index: int | None = None
    question_id: str = ""
    # draft_alternatives: the first segment of the turn on the base scope; the
    # selected line replaces everything from it on.
    base_cut_id: str = ""

    def try_at(self, try_index: int) -> TryRecord:
        """The try with ``try_index`` (``KeyError`` when the run has none)."""
        for record in self.tries:
            if record.try_index == try_index:
                return record
        raise KeyError(try_index)

    def to_content(self) -> dict[str, Any]:
        """This snapshot as the ``variant_record`` segment's content."""
        return {"schema": SCHEMA, **asdict(self)}

    @classmethod
    def from_content(cls, content: Mapping[str, Any]) -> "VariantRun":
        """Rebuild a snapshot from :meth:`to_content`."""
        data = {k: v for k, v in content.items() if k != "schema"}
        tries = [TryRecord(**dict(t)) for t in data.pop("tries", []) or []]
        return cls(**data, tries=tries)


@dataclass(frozen=True)
class PreferenceCandidate:
    """One candidate of a finished run, as an optimizer reads it."""

    try_index: int
    scope: str
    text: str
    score: float | None
    tokens: Mapping[str, int]
    advice: str
    forked_from: int | None


@dataclass(frozen=True)
class PreferenceRecord:
    """A finished run: the candidates, the judge's verdict and the user's words."""

    variants_id: str
    session_id: str
    agent_id: str
    turn_id: str
    origin: str
    strategy: str
    judge: str
    n: int
    rubric: str
    candidates: tuple[PreferenceCandidate, ...]
    scores: tuple[tuple[int, float], ...]
    pick: int | None
    comment: str
    advice: tuple[str, ...]
    selected_index: int


def _plane(app: Any) -> Any:
    from clio_agent.arc.history_plane import plane_for  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import NoContextStoreError  # noqa: PLC0415

    plane = plane_for(app) if app is not None else None
    if plane is None:
        raise NoContextStoreError()
    return plane


def save_run(app: Any, run: VariantRun) -> None:
    """Append ``run``'s current state to the session's variant lane in clio-core."""
    _plane(app).append_segment(
        run.session_id, VARIANT_RECORD_SCOPE, VARIANT_RECORD_KIND, run.to_content()
    )


def _latest(app: Any, session_id: str) -> dict[str, VariantRun]:
    """The latest snapshot of every run of ``session_id``, in first-recorded order."""
    runs: dict[str, VariantRun] = {}
    for seg in _plane(app).list_segments(session_id, VARIANT_RECORD_SCOPE):
        content = getattr(seg, "content", None)
        if getattr(seg, "kind", "") != VARIANT_RECORD_KIND or not isinstance(content, Mapping):
            continue
        run = VariantRun.from_content(content)
        runs[run.variants_id] = run
    return runs


def load_run(app: Any, session_id: str, variants_id: str) -> VariantRun:
    """The latest recorded state of run ``variants_id`` (typed error when absent)."""
    run = _latest(app, session_id).get(variants_id)
    if run is None:
        raise VariantRunNotFoundError(session_id, variants_id)
    return run


def preference_records(app: Any, session_id: str) -> list[PreferenceRecord]:
    """Every finished run of ``session_id`` as a typed preference record, oldest first."""
    records: list[PreferenceRecord] = []
    for run in _latest(app, session_id).values():
        if run.status != "selected" or run.selected_index is None:
            continue
        records.append(
            PreferenceRecord(
                variants_id=run.variants_id,
                session_id=run.session_id,
                agent_id=run.agent_id,
                turn_id=run.turn_id,
                origin=run.origin,
                strategy=run.strategy,
                judge=run.judge,
                n=run.n,
                rubric=run.rubric,
                candidates=tuple(
                    PreferenceCandidate(
                        try_index=t.try_index,
                        scope=t.scope,
                        text=t.text,
                        score=t.score,
                        tokens=dict(t.tokens),
                        advice=t.advice,
                        forked_from=t.forked_from,
                    )
                    for t in run.tries
                    if t.status == "completed"
                ),
                scores=tuple(
                    (t.try_index, float(t.score)) for t in run.tries if t.score is not None
                ),
                pick=run.pick,
                comment=run.comment,
                advice=tuple(t.advice for t in run.tries if t.advice),
                selected_index=run.selected_index,
            )
        )
    return records
