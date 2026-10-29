"""The science log's entry types: their fields, statuses and the links each needs.

Every entry carries an id, its study, its branch (the session that wrote it), a
timestamp, a type, typed fields and links (``{"relation", "to"}``). A status change
is a ``status`` entry naming the entry it changes, and a relation added later is a
``link`` entry: nothing is edited in place. The rules here are schema and reference
rules only; whether a claim is right is never judged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

EntryType = Literal[
    "question",
    "hypothesis",
    "experiment",
    "observation",
    "evidence",
    "reframe",
    "conclusion",
    "conflict",
    "view",
]
ENTRY_TYPES: tuple[str, ...] = (
    "question",
    "hypothesis",
    "experiment",
    "observation",
    "evidence",
    "reframe",
    "conclusion",
    "conflict",
    "view",
)
#: Entries that record a change to another entry rather than a finding.
META_TYPES: tuple[str, ...] = ("status", "link")

#: A new entry's status, and the statuses ``set_status`` may give it.
DEFAULT_STATUS: dict[str, str] = {
    "question": "open",
    "hypothesis": "open",
    "experiment": "planned",
    "observation": "recorded",
    "evidence": "recorded",
    "reframe": "recorded",
    "conclusion": "recorded",
    "conflict": "open",
    "view": "recorded",
}
STATUSES: dict[str, frozenset[str]] = {
    "question": frozenset({"open", "answered", "superseded"}),
    "hypothesis": frozenset({"open", "supported", "refuted", "superseded"}),
    "experiment": frozenset({"planned", "running", "done", "abandoned", "superseded"}),
    "observation": frozenset({"recorded", "superseded", "withdrawn"}),
    "evidence": frozenset({"recorded", "superseded", "withdrawn"}),
    "reframe": frozenset({"recorded", "superseded", "withdrawn"}),
    "conclusion": frozenset({"recorded", "superseded", "withdrawn"}),
    "conflict": frozenset({"open", "resolved"}),
    "view": frozenset({"recorded", "superseded", "withdrawn"}),
}


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(_Fields):
    amount: float = Field(gt=0)
    unit: str = Field(min_length=1)


class QuestionFields(_Fields):
    statement: str = Field(min_length=1)
    scope: str = ""
    #: Only on a study's root question: one shared pool, charged by Observations.
    budget: Budget | None = None


class HypothesisFields(_Fields):
    claim: str = Field(min_length=1)
    refuting_prediction: str = Field(min_length=1)
    #: The claim as a formula, when the hypothesis states a law.
    formula: str = ""


class ExperimentFields(_Fields):
    method: str = Field(min_length=1)
    sample_size: int | None = Field(default=None, ge=0)


class Measurement(_Fields):
    """One structured value: typed by the model, or named by a field of a cited call's
    structured result (``source``), in which case the log copies the value."""

    metric: str = Field(min_length=1)
    value: float | int | str | bool | None = None
    source: dict[str, str] | None = None
    unit: str = ""
    #: The conditions it was taken under: settings, seed, strategy, node, repeat index.
    conditions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def _source(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None and (not value.get("call") or not value.get("field")):
            raise ValueError("source names a cited call ('call') and a result field ('field')")
        return value


class ObservationFields(_Fields):
    #: The recorded calls that produced the result (``call:<session>/<call_id>``).
    from_calls: list[str] = Field(default_factory=list)
    result: str = ""
    measurements: list[Measurement] = Field(default_factory=list)
    #: In the study budget's unit; required when the study declares a budget.
    cost: float | None = Field(default=None, ge=0)
    #: The Observation or artifact version this candidate came from.
    derived_from: str = ""
    environment: str = ""


class EvidenceFields(_Fields):
    interpretation: str = Field(min_length=1)
    strength: Literal["weak", "moderate", "strong", "inconclusive"]
    #: The analysis call (a shell or Python run) that produced the numbers, if any.
    analysis_call: str = ""
    #: The seed of a random analysis (a bootstrap, a permutation test).
    seed: int | str | None = None


class ReframeFields(_Fields):
    rationale: str = Field(min_length=1)


class Claim(_Fields):
    text: str = Field(min_length=1)
    cites: list[str] = Field(min_length=1)
    analysis_call: str = ""


class ConclusionFields(_Fields):
    claims: list[Claim] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]
    caveats: list[str] = Field(default_factory=list)
    relation: Literal["answers", "refutes_premise", "replaces"]


class ConflictFields(_Fields):
    description: str = Field(min_length=1)


class ViewFields(_Fields):
    """A dashboard of the writing session attached to the study (Addendum B)."""

    surface_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    purpose: str = Field(min_length=1)


FIELDS: dict[str, type[_Fields]] = {
    "question": QuestionFields,
    "hypothesis": HypothesisFields,
    "experiment": ExperimentFields,
    "observation": ObservationFields,
    "evidence": EvidenceFields,
    "reframe": ReframeFields,
    "conclusion": ConclusionFields,
    "conflict": ConflictFields,
    "view": ViewFields,
}

#: ``type -> relation -> (target types, least, most)``; most ``None`` is unbounded.
LINKS: dict[str, dict[str, tuple[frozenset[str], int, int | None]]] = {
    "question": {"reframes": (frozenset({"question"}), 0, 1)},
    "hypothesis": {"question": (frozenset({"question"}), 1, 1)},
    "experiment": {"hypothesis": (frozenset({"hypothesis"}), 1, 1)},
    "observation": {"experiment": (frozenset({"experiment"}), 1, 1)},
    "evidence": {
        "observation": (frozenset({"observation"}), 1, None),
        "supports": (frozenset({"hypothesis"}), 0, None),
        "refutes": (frozenset({"hypothesis"}), 0, None),
    },
    "reframe": {
        "old_question": (frozenset({"question"}), 1, 1),
        "new_question": (frozenset({"question"}), 1, 1),
        "evidence": (frozenset({"evidence", "observation"}), 1, None),
    },
    "conclusion": {
        "question": (frozenset({"question"}), 1, 1),
        "merges": (frozenset({"conclusion"}), 0, None),
    },
    "conflict": {"between": (frozenset(ENTRY_TYPES), 2, None)},
    "view": {"serves": (frozenset({"question", "hypothesis"}), 1, None)},
}


def type_signatures() -> str:
    """One line per entry type: its fields (``?`` optional) and its links, from the models."""

    lines = []
    for name, model in FIELDS.items():
        fields = ", ".join(
            key + ("" if info.is_required() else "?") for key, info in model.model_fields.items()
        )
        links = ", ".join(
            f"{rel}: {'|'.join(sorted(targets))}{'' if least else '?'}{'[]' if most != 1 else ''}"
            for rel, (targets, least, most) in LINKS[name].items()
        )
        lines.append(f"{name} {{{fields}}}" + (f" links {{{links}}}" if links else ""))
    return "; ".join(lines)


#: Relations ``link`` may add later between any two visible entries.
LATER_RELATIONS = frozenset(
    {"supports", "refutes", "relates_to", "cites", "derived_from", "answers", "merges"}
)


class EntryRejected(ValueError):
    """A typed refusal of an entry: ``reason`` is the stable code the tool returns."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {"accepted": False, "reason": self.reason, "error": f"{self.reason}: {self}"}


def validate_fields(entry_type: str, fields: Any) -> dict[str, Any]:
    """The entry's fields, validated against its type; a typed refusal otherwise."""

    if entry_type not in FIELDS:
        raise EntryRejected(
            "entry_type_unknown",
            f"type must be one of {', '.join(ENTRY_TYPES)}, not {entry_type!r}",
        )
    try:
        return FIELDS[entry_type].model_validate(fields or {}).model_dump(exclude_none=True)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'fields'}: {error['msg']}"
            for error in exc.errors()
        )
        raise EntryRejected("entry_fields_invalid", f"{entry_type} fields: {problems}") from exc


def normalize_links(entry_type: str, links: Any) -> list[dict[str, str]]:
    """``[{"relation", "to"}]`` from a list of rows or a ``{relation: id | [ids]}`` mapping."""

    rows: list[dict[str, str]] = []
    if isinstance(links, dict):
        for relation, targets in links.items():
            for target in targets if isinstance(targets, list) else [targets]:
                rows.append({"relation": str(relation), "to": str(target)})
    elif isinstance(links, list):
        for row in links:
            if not isinstance(row, dict) or not row.get("relation") or not row.get("to"):
                raise EntryRejected("entry_links_invalid", "each link names a relation and 'to'")
            rows.append({"relation": str(row["relation"]), "to": str(row["to"])})
    elif links not in (None, ""):
        raise EntryRejected("entry_links_invalid", "links is a list or a mapping")
    allowed = LINKS.get(entry_type, {})
    for row in rows:
        if row["relation"] not in allowed:
            names = ", ".join(sorted(allowed)) or "none"
            raise EntryRejected(
                "entry_links_invalid",
                f"a {entry_type} links by {names}, not {row['relation']!r}",
            )
    for relation, (_, least, most) in allowed.items():
        count = sum(row["relation"] == relation for row in rows)
        if count < least or (most is not None and count > most):
            bound = f"exactly {least}" if least == most else f"at least {least}"
            raise EntryRejected(
                "entry_links_invalid", f"a {entry_type} needs {bound} {relation!r} link(s)"
            )
    if entry_type == "evidence" and not any(
        row["relation"] in {"supports", "refutes"} for row in rows
    ):
        raise EntryRejected(
            "entry_links_invalid", "evidence supports or refutes at least one hypothesis"
        )
    return rows


__all__ = [
    "DEFAULT_STATUS",
    "ENTRY_TYPES",
    "FIELDS",
    "LATER_RELATIONS",
    "LINKS",
    "META_TYPES",
    "STATUSES",
    "EntryRejected",
    "normalize_links",
    "validate_fields",
]
