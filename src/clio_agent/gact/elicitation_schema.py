"""Pure schema translation, answer validation, and URL trust for MCP elicitation.

The stateless leaf of the elicitation bridge (#1113): no ``app``, no question
minting, no I/O — just translating a form-mode elicitation's flat restricted JSON
Schema into a :class:`~clio_agent.gact.types.UserQuestion` shape, validating a
submitted answer against that schema BEFORE it is accepted upstream, and deciding
url-mode trust from an origin (never a fetch). :mod:`clio_agent.gact.elicitation_bridge`
composes these; keeping them here holds both modules under the size ratchet.

The functions return typed reason KEYS (``elicitation_*``) whose human strings live
in :data:`clio_agent.gact.elicitation_bridge.ELICITATION_REASONS`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from clio_agent.gact.types import UserQuestion, UserQuestionOption

__all__ = [
    "ELICITATION_QUESTION_SOURCE",
    "FormTranslation",
    "build_form_content",
    "check_elicitation_answer",
    "check_url_trust",
    "translate_form_schema",
    "validate_elicitation_answer",
]

#: ``UserQuestion.source`` stamped on every elicitation-derived question, so the
#: shared answer route can recognise an elicitation and resolve its parked call.
ELICITATION_QUESTION_SOURCE = "mcp_elicitation"

#: JSON-Schema scalar types this form translator accepts (flat, restricted set).
_SUPPORTED_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})


# --- Schema translation (form mode) ---


@dataclass(frozen=True)
class FormTranslation:
    """The result of translating a form-mode elicitation schema.

    ``degrade`` is a key in ``ELICITATION_REASONS`` when the schema cannot be
    served (non-object / non-flat / unsupported field); all other fields are then
    empty and the caller declines the elicitation with that typed reason.
    """

    kind: Literal["freeform", "choice", "confirmation"] = "freeform"
    options: list[UserQuestionOption] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)
    additional_properties: bool = True
    degrade: str | None = None


def _enum_options(values: Sequence[Any], names: Sequence[Any] | None) -> list[UserQuestionOption]:
    """Build one :class:`UserQuestionOption` per enum value (label from enumNames)."""

    options: list[UserQuestionOption] = []
    for index, value in enumerate(values):
        label = str(names[index]) if names and index < len(names) else str(value)
        options.append(UserQuestionOption(label=label, value=str(value)))
    return options


def translate_form_schema(requested_schema: Mapping[str, Any]) -> FormTranslation:
    """Translate a flat restricted JSON Schema into a UserQuestion shape.

    Accepts the MCP form-mode subset: a top-level ``{"type": "object",
    "properties": {...}}`` whose property values are scalar (string / number /
    integer / boolean) or enum, optionally with ``default`` / ``title`` /
    ``description``. Nesting (object / array property values) or an unsupported
    field type returns a typed :attr:`FormTranslation.degrade` instead of raising.

    Kind selection: a single boolean field -> ``confirmation``; a single enum
    field -> ``choice`` (options are the enum values); anything else ->
    ``freeform`` (the UI renders the multi-field / scalar form from the
    ``fields`` descriptor carried in the question metadata).
    """

    if not isinstance(requested_schema, Mapping) or requested_schema.get("type") != "object":
        return FormTranslation(degrade="elicitation_schema_not_object")
    properties = requested_schema.get("properties")
    if not isinstance(properties, Mapping):
        return FormTranslation(degrade="elicitation_schema_not_object")
    required = set(requested_schema.get("required") or [])
    additional = requested_schema.get("additionalProperties", True) is not False

    fields: list[dict[str, Any]] = []
    for name, spec in properties.items():
        if not isinstance(spec, Mapping):
            return FormTranslation(degrade="elicitation_schema_not_flat")
        field_type = spec.get("type")
        enum = spec.get("enum")
        # A nested object/array (with or without a declared type) is not flat.
        if field_type in {"object", "array"} or "properties" in spec or "items" in spec:
            return FormTranslation(degrade="elicitation_schema_not_flat")
        if enum is None and field_type not in _SUPPORTED_SCALAR_TYPES:
            return FormTranslation(degrade="elicitation_unsupported_field_type")
        fields.append(
            {
                "name": str(name),
                "type": str(field_type or ("string" if enum is not None else "")),
                "enum": [str(v) for v in enum]
                if isinstance(enum, Sequence) and enum is not None
                else None,
                "default": spec.get("default"),
                "title": str(spec.get("title") or name),
                "description": str(spec.get("description") or ""),
                "required": str(name) in required,
            }
        )

    if len(fields) == 1:
        only = fields[0]
        if only["enum"]:
            names = properties[only["name"]].get("enumNames")
            return FormTranslation(
                kind="choice",
                options=_enum_options(only["enum"], names if isinstance(names, Sequence) else None),
                fields=fields,
                additional_properties=additional,
            )
        if only["type"] == "boolean":
            return FormTranslation(
                kind="confirmation", fields=fields, additional_properties=additional
            )
    return FormTranslation(kind="freeform", fields=fields, additional_properties=additional)


# --- URL trust (url mode) ---


def _origin(url: str) -> str:
    """Return the scheme://host[:port] origin of ``url`` (lower-cased)."""

    parts = urlsplit(url)
    netloc = parts.netloc.lower()
    return f"{parts.scheme.lower()}://{netloc}" if netloc else ""


def check_url_trust(url: str, trusted_origins: Sequence[str]) -> str | None:
    """Return a typed reject reason for ``url``, or ``None`` when it is trusted.

    Trust is decided WITHOUT fetching the URL: only its origin is inspected. A
    non-https URL is rejected outright; an origin absent from ``trusted_origins``
    (a configured allow-list) is rejected. An empty allow-list means the url
    trust flow is not configured, so every url is rejected as not-declared.
    """

    if not trusted_origins:
        return "elicitation_url_not_declared"
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        return "elicitation_url_insecure_scheme"
    allowed = {_origin(o) if "://" in o else o.lower() for o in trusted_origins}
    origin = _origin(url)
    host = parts.netloc.lower()
    if origin not in allowed and host not in allowed:
        return "elicitation_url_untrusted_origin"
    return None


# --- Answer -> content + validation ---


def _coerce(value: Any, field_type: str) -> Any:
    """Coerce a string-ish answer to the field's JSON-Schema scalar type."""

    if field_type in {"number", "integer"} and isinstance(value, str):
        try:
            return int(value) if field_type == "integer" else float(value)
        except ValueError:
            return value
    if field_type == "boolean" and isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "on", "y"}
    return value


def build_form_content(
    fields: Sequence[Mapping[str, Any]],
    *,
    selected_options: Sequence[str],
    answer: str,
    answer_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the accept ``content`` dict from the answer + the field descriptors.

    Precedence per field: an explicit value in ``answer_metadata`` (a multi-field
    form submits ``{field: value}`` there), else the single-field shorthand
    (``selected_options`` for a choice, ``answer`` for freeform/confirmation),
    else the schema default. Values are coerced to the declared scalar type.
    """

    content: dict[str, Any] = {}
    single = len(fields) == 1
    for spec in fields:
        name = str(spec["name"])
        field_type = str(spec.get("type") or "string")
        if name in answer_metadata:
            content[name] = _coerce(answer_metadata[name], field_type)
        elif single and selected_options:
            content[name] = _coerce(selected_options[0], field_type)
        elif single and answer != "":
            content[name] = _coerce(answer, field_type)
        elif spec.get("default") is not None:
            content[name] = spec["default"]
    return content


def validate_elicitation_answer(
    row: UserQuestion,
    *,
    selected_options: Sequence[str],
    answer: str,
    answer_metadata: Mapping[str, Any],
) -> str | None:
    """Return a 422 error message if an elicitation answer is schema-invalid, else None.

    Applies only to form-mode elicitation accepts (url / decline / cancel are always
    valid). Validates the derived content against the restricted schema BEFORE the
    future is resolved (finding 7): required fields present, enum membership, integer
    / number coercibility, and ``additionalProperties`` when the schema forbids
    extras — so an invalid answer becomes a recoverable 422 re-prompt rather than an
    ``accept`` the upstream server rejects with an untyped tool failure.
    """

    elicitation = row.metadata.get("elicitation") or {}
    if row.source != ELICITATION_QUESTION_SOURCE or elicitation.get("mode") != "form":
        return None
    if str((answer_metadata or {}).get("elicitation_action") or "") == "decline":
        return None
    fields = elicitation.get("fields") or []
    by_name = {str(f["name"]): f for f in fields}
    if elicitation.get("additional_properties") is False:
        extra = set(answer_metadata or {}) - set(by_name) - {"elicitation_action"}
        if extra:
            return f"unexpected field(s) for this form: {sorted(extra)}"
    content = build_form_content(
        fields,
        selected_options=selected_options,
        answer=answer,
        answer_metadata=answer_metadata,
    )
    for spec in fields:
        name = str(spec["name"])
        present = name in content
        if spec.get("required") and not present:
            return f"missing required field: {name!r}"
        if not present:
            continue
        value = content[name]
        enum = spec.get("enum")
        if enum and str(value) not in [str(e) for e in enum]:
            return f"field {name!r} must be one of {list(enum)}"
        field_type = str(spec.get("type") or "string")
        if field_type in {"integer", "number"} and isinstance(value, str):
            return f"field {name!r} must be a {field_type}"
    return None


def check_elicitation_answer(row: UserQuestion, req: Any) -> None:
    """Raise a recoverable 422 if ``req`` is a schema-invalid elicitation answer.

    Called by the shared answer route BEFORE the question is marked answered, so an
    invalid form answer re-prompts (question stays pending) instead of resolving the
    parked future with content the upstream server would reject (finding 7).
    """

    message = validate_elicitation_answer(
        row,
        selected_options=list(getattr(req, "selected_options", []) or []),
        answer=str(getattr(req, "answer", "") or ""),
        answer_metadata=dict(getattr(req, "metadata", {}) or {}),
    )
    if message is None:
        return
    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo  # noqa: PLC0415

    raise HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="bad_request",
                message=message,
                details={"session_id": row.session_id, "question_id": row.id},
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )
