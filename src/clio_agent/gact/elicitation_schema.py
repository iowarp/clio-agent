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

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from clio_agent.gact.types import UserQuestion, UserQuestionOption

__all__ = [
    "ELICITATION_QUESTION_SOURCE",
    "FormTranslation",
    "build_form_content",
    "build_form_metadata",
    "build_url_metadata",
    "check_elicitation_answer",
    "check_url_trust",
    "default_answer_metadata",
    "punycode_warning",
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

    ``kind="multi_choice"`` (SEP-1330) is the ONE new kind this module adds: a
    single flat array-of-enum field (``{"type": "array", "items": {"enum": [...]}}``)
    -- a multi-select whose options are the item enum values, distinct from
    ``"choice"`` (a single SCALAR enum field, still exactly one selection).
    """

    kind: Literal["freeform", "choice", "confirmation", "multi_choice"] = "freeform"
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


def _flat_multi_select(spec: Mapping[str, Any]) -> tuple[list[Any], str, list[Any] | None] | None:
    """Return ``(enum, item_type, enum_names)`` for a flat array-of-enum ``spec``.

    SEP-1330 multi-select: only a FLAT array whose ``items`` is itself an
    enum-bearing scalar is admitted -- a missing/nested/unconstrained ``items``
    returns ``None`` so the caller degrades ``elicitation_schema_not_flat``,
    exactly like a bare (non-enum) array field was already rejected before
    this shape existed. Only the multi-select-enum exception is newly opened.
    """

    items = spec.get("items")
    if (
        not isinstance(items, Mapping)
        or items.get("type") in {"object", "array"}
        or "properties" in items
        or "items" in items
    ):
        return None
    item_enum = items.get("enum")
    if not (isinstance(item_enum, Sequence) and not isinstance(item_enum, (str, bytes))):
        return None
    names = items.get("enumNames")
    enum_names = (
        list(names) if isinstance(names, Sequence) and not isinstance(names, (str, bytes)) else None
    )
    return list(item_enum), str(items.get("type") or "string"), enum_names


def _array_field(name: str, spec: Mapping[str, Any], required: set[str]) -> dict[str, Any] | str:
    """Build one multi-select field descriptor, or return a typed degrade reason."""

    multi = _flat_multi_select(spec)
    if multi is None:
        return "elicitation_schema_not_flat"
    item_enum, item_type, item_enum_names = multi
    return {
        "name": str(name),
        "type": "array",
        "enum": item_enum,
        "enum_names": item_enum_names,
        "multi": True,
        "item_type": item_type,
        "default": spec.get("default"),
        "title": str(spec.get("title") or name),
        "description": str(spec.get("description") or ""),
        "required": str(name) in required,
    }


def _scalar_field(name: str, spec: Mapping[str, Any], required: set[str]) -> dict[str, Any] | str:
    """Build one scalar/enum field descriptor, or return a typed degrade reason."""

    field_type = spec.get("type")
    enum = spec.get("enum")
    # A nested object (with or without a declared type) is not flat.
    if field_type == "object" or "properties" in spec or "items" in spec:
        return "elicitation_schema_not_flat"
    if enum is None and field_type not in _SUPPORTED_SCALAR_TYPES:
        return "elicitation_unsupported_field_type"
    return {
        "name": str(name),
        "type": str(field_type or ("string" if enum is not None else "")),
        # Keep the enum's ORIGINAL types so membership stays type-preserving
        # (``1`` must not satisfy an enum of ``['1']``) — finding 7 remnant.
        "enum": list(enum)
        if isinstance(enum, Sequence) and not isinstance(enum, (str, bytes))
        else None,
        "enum_names": None,
        "multi": False,
        "item_type": "",
        "default": spec.get("default"),
        "title": str(spec.get("title") or name),
        "description": str(spec.get("description") or ""),
        "required": str(name) in required,
    }


def _single_field_kind(
    only: Mapping[str, Any], properties: Mapping[str, Any]
) -> Literal["choice", "confirmation", "multi_choice"] | None:
    """Return the ``FormTranslation.kind`` a single-field schema resolves to, or
    ``None`` when it stays ``freeform`` (multi-field forms never call this)."""

    if only["enum"]:
        return "multi_choice" if only["multi"] else "choice"
    if only["type"] == "boolean":
        return "confirmation"
    return None


def translate_form_schema(requested_schema: Mapping[str, Any]) -> FormTranslation:
    """Translate a flat restricted JSON Schema into a UserQuestion shape.

    Accepts the MCP form-mode subset: a top-level ``{"type": "object",
    "properties": {...}}`` whose property values are scalar (string / number /
    integer / boolean), enum, or a flat array-of-enum (SEP-1330 multi-select),
    optionally with ``default`` / ``title`` / ``description``. Any other
    nesting (a plain object, an unconstrained/nested array) or an unsupported
    scalar type returns a typed :attr:`FormTranslation.degrade` instead of
    raising.

    Kind selection: a single boolean field -> ``confirmation``; a single
    scalar-enum field -> ``choice``; a single array-of-enum field ->
    ``multi_choice`` (SEP-1330); anything else -> ``freeform`` (the UI renders
    the multi-field / scalar form from the ``fields`` descriptor carried in
    the question metadata -- a multi-select field inside a multi-field form
    still renders this way, keyed by that field's own ``multi``/``item_type``).
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
        builder = _array_field if spec.get("type") == "array" else _scalar_field
        built = builder(name, spec, required)
        if isinstance(built, str):
            return FormTranslation(degrade=built)
        fields.append(built)

    if len(fields) == 1:
        kind = _single_field_kind(fields[0], properties)
        if kind is not None:
            names = fields[0].get("enum_names") or properties[fields[0]["name"]].get("enumNames")
            options = (
                _enum_options(fields[0]["enum"], names if isinstance(names, Sequence) else None)
                if kind != "confirmation"
                else []
            )
            return FormTranslation(
                kind=kind, options=options, fields=fields, additional_properties=additional
            )
    return FormTranslation(kind="freeform", fields=fields, additional_properties=additional)


def default_answer_metadata(fields: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pre-populate the answer-metadata surface from each field's default (SEP-1034).

    Every field carrying a non-``None`` ``default`` (already preserved verbatim
    by :func:`translate_form_schema`) is copied onto the returned dict, keyed by
    field name -- the SAME shape :func:`build_form_content` reads back at submit
    time (its highest-precedence source). A caller mints the PENDING question
    with this as its ``answer_metadata``, so a client that renders the pending
    question's ``answer_metadata`` as pre-filled form values, then submits
    unchanged, round-trips through the exact declared default -- not just a
    value carried inertly on ``metadata.elicitation.fields[i].default`` that no
    one reads until submit.
    """

    prefill: dict[str, Any] = {}
    for spec in fields:
        default = spec.get("default")
        if default is not None:
            prefill[str(spec["name"])] = default
    return prefill


def build_form_metadata(
    translation: FormTranslation,
    *,
    request_id: Any,
    namespace: str | None,
    tool_name: str | None,
    invocation_id: str,
    forwarded_from_session: str,
) -> dict[str, dict[str, Any]]:
    """Assemble the form-mode ``metadata.elicitation`` block for a new question."""

    return {
        "elicitation": {
            "mode": "form",
            "fields": translation.fields,
            "additional_properties": translation.additional_properties,
            "request_id": request_id,
            "namespace": namespace,
            "tool_name": tool_name,
            "invocation_id": invocation_id,
            "forwarded_from_session": forwarded_from_session,
        }
    }


def build_url_metadata(
    url: str,
    *,
    request_id: Any,
    namespace: str | None,
    tool_name: str | None,
    invocation_id: str,
    forwarded_from_session: str,
) -> dict[str, dict[str, Any]]:
    """Assemble the url-mode ``metadata.elicitation`` block for a new question.

    Carries the FULL url verbatim plus the F3 punycode-warning fields
    (``punycode_warning`` / ``punycode_host``) alongside it -- URL-mode consent
    data completeness (C1-S4/#1284 build item 3); rendering the warning is the
    UI's lane, this is the data it renders from.
    """

    warning, display_host = punycode_warning(url)
    return {
        "elicitation": {
            "mode": "url",
            "url": url,
            # Client MUST render in an isolated, non-inspectable container
            # (ephemeral, no shared session/referrer) — see the bridge module docstring.
            "container": "isolated",
            "punycode_warning": warning,
            "punycode_host": display_host,
            "request_id": request_id,
            "namespace": namespace,
            "tool_name": tool_name,
            "invocation_id": invocation_id,
            "forwarded_from_session": forwarded_from_session,
        }
    }


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


def punycode_warning(url: str) -> tuple[bool, str]:
    """Detect an IDN/punycode host in ``url`` (F3: URL-mode consent MUST).

    Returns ``(warning, display_host)``: ``warning`` is ``True`` when the
    host carries any ACE-encoded label (an ``xn--`` prefix, RFC 3492) -- the
    classic homograph-attack surface a consent surface must flag; the FULL
    original url is never altered by this check (see :func:`build_url_metadata`).
    ``display_host`` is the best-effort Unicode-decoded host, for a
    side-by-side "shown next to the raw url" comparison; on any decode
    failure it falls back to the raw (still-encoded) host rather than
    raising -- this function never fails a caller's flow.
    """

    host = urlsplit(url).hostname or ""
    if not any(label.lower().startswith("xn--") for label in host.split(".")):
        return False, host
    try:
        return True, host.encode("ascii").decode("idna")
    except (UnicodeError, LookupError):
        return True, host


# --- Answer -> content + validation ---


_TRUE_STRINGS = frozenset({"true", "yes", "1", "on", "y"})
_FALSE_STRINGS = frozenset({"false", "no", "0", "off", "n"})


def _coerce(value: Any, field_type: str) -> Any:
    """Coerce a string answer toward the declared scalar type (best-effort, strict).

    Only STRINGS are coerced; non-strings pass through unchanged so :func:`_valid_scalar`
    can reject a wrong type (e.g. a bool handed to an integer field). An unparseable
    numeric string or an UNRECOGNISED boolean string is returned unchanged — never
    silently coerced to ``0``/``False`` — so validation rejects it (finding 7).
    """

    if not isinstance(value, str):
        return value
    if field_type == "integer":
        try:
            return int(value.strip())
        except ValueError:
            return value
    if field_type == "number":
        try:
            return float(value.strip())
        except ValueError:
            return value
    if field_type == "boolean":
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
        return value  # unrecognised -> stays a str so validation rejects it
    return value


def _valid_scalar(value: Any, field_type: str, enum: Sequence[Any] | None) -> bool:
    """Exact JSON-Schema scalar check on a POST-coercion value (finding 7).

    ``enum`` present (including an EMPTY enum, which admits nothing) enforces
    TYPE-PRESERVING membership (``1`` is not ``'1'``) and supersedes the primitive
    type. Otherwise every supported primitive is checked exactly: a string must be
    ``str``; booleans are NOT numbers/integers; an integer must be integral; a number
    must be finite; both numeric types exclude ``bool``.
    """

    if enum is not None:
        return value in enum  # type-preserving; empty enum admits nothing
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "number":
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    if field_type == "string":
        return isinstance(value, str)
    return True  # unconstrained (no declared type, no enum)


def _valid_multi_enum(value: Any, item_type: str, enum: Sequence[Any]) -> bool:
    """Validate a SEP-1330 multi-select answer: a list, every element a
    type-preserving member of the declared item ``enum`` (checked per-element
    through :func:`_valid_scalar`, so the same type-preserving rule applies)."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return False
    return all(_valid_scalar(v, item_type, enum) for v in value)


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
    else the schema default. Values are coerced to the declared scalar type. A
    ``multi`` field (SEP-1330) instead coerces EVERY element to its ``item_type``
    and yields a list value; ``selected_options`` (a multi-select's own natural
    shape) is taken whole rather than only its first entry.
    """

    content: dict[str, Any] = {}
    single = len(fields) == 1
    for spec in fields:
        name = str(spec["name"])
        field_type = str(spec.get("type") or "string")
        multi = bool(spec.get("multi"))
        item_type = str(spec.get("item_type") or "string")
        if name in answer_metadata:
            raw = answer_metadata[name]
            if multi and isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                content[name] = [_coerce(v, item_type) for v in raw]
            else:
                content[name] = _coerce(raw, field_type)
        elif single and selected_options:
            content[name] = (
                [_coerce(v, item_type) for v in selected_options]
                if multi
                else _coerce(selected_options[0], field_type)
            )
        elif single and answer != "":
            content[name] = _coerce(answer, field_type)
        elif spec.get("default") is not None:
            content[name] = spec["default"]
    return content


def _field_validation_error(spec: Mapping[str, Any], content: Mapping[str, Any]) -> str | None:
    """Validate one field's derived value against its descriptor.

    Returns a 422 error message, or ``None`` when the field is absent-and-
    optional or valid. Multi-select (SEP-1330) and scalar fields validate
    through the same type-preserving primitives (:func:`_valid_multi_enum` /
    :func:`_valid_scalar`), just checked against a list vs. a bare value.
    """

    name = str(spec["name"])
    if name not in content:
        return f"missing required field: {name!r}" if spec.get("required") else None
    value = content[name]
    enum = spec.get("enum")
    field_type = str(spec.get("type") or "string")
    if spec.get("multi"):
        item_type = str(spec.get("item_type") or "string")
        if _valid_multi_enum(value, item_type, enum or []):
            return None
        return f"field {name!r} must be a list of values from {list(enum or [])}"
    if _valid_scalar(value, field_type, enum):
        return None
    if enum is not None:
        return f"field {name!r} must be one of {list(enum)}"
    return f"field {name!r} must be a valid {field_type}"


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
        error = _field_validation_error(spec, content)
        if error is not None:
            return error
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
