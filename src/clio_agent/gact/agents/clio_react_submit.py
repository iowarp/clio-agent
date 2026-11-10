"""Submit-audit helpers for the CLIO agent loop (ClioReAct)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

import pydantic

if TYPE_CHECKING:
    from clio_agent.gact.agents.dspy_native_tool import ClioNativeTool


class SubmittedOutputs(dict[str, Any]):
    """Validated signature values with a separate orchestration disposition."""

    def __init__(self, values: Mapping[str, Any], disposition: str | None) -> None:
        super().__init__(values)
        self.assignment_disposition = disposition


def submit_tool(signature: Any) -> ClioNativeTool:
    """Build the finish boundary without adding orchestration to signature outputs."""
    from clio_agent.gact.agents.dspy_native_tool import ClioNativeTool

    fields = signature.output_fields
    if "assignment_disposition" in fields:
        raise ValueError("assignment_disposition is reserved for conversation orchestration")
    schemas = {name: _json_schema(field.annotation) for name, field in fields.items()}
    for name, field in fields.items():
        present, value = _declared_default(field)
        if present:
            schemas[name]["default"] = value
    schemas["assignment_disposition"] = {
        "anyOf": [{"type": "string", "enum": ["yield", "complete"]}, {"type": "null"}],
        "default": None,
        "description": "Yield preserves this assignment; complete ends it. Accepted background work continues.",
    }

    def submit(**values: Any) -> SubmittedOutputs:
        disposition = values.pop("assignment_disposition", None)
        if disposition not in {None, "yield", "complete"}:
            raise ValueError("assignment_disposition must be yield or complete")
        unexpected = values.keys() - fields.keys()
        if unexpected:
            raise ValueError(f"Unknown output fields: {sorted(unexpected)}")
        validated = {}
        for name, field in fields.items():
            if name not in values:
                present, value = _declared_default(field)
                if not present:
                    raise ValueError(f"Missing required final output field(s): {name}")
            else:
                value = values[name]
            validated[name] = pydantic.TypeAdapter(field.annotation).validate_python(value)
        return SubmittedOutputs(validated, disposition)

    from clio_agent.tools.tool_settings import CALLERS_ATTR, CALLERS_DIRECT

    setattr(submit, CALLERS_ATTR, CALLERS_DIRECT)  # finishing is a step of the agent
    return ClioNativeTool(
        submit,
        name="submit",
        args=schemas,
        arg_types={
            **{name: field.annotation for name, field in fields.items()},
            "assignment_disposition": Literal["yield", "complete"] | None,
        },
        desc="Return the declared outputs and yield or complete this assignment. Yield when awaiting background work or answers.",
    )


def tool_names(tools: Iterable[Any]) -> list[str]:
    """Return stable names from DSPy tool-like objects."""

    return [name for tool in tools if (name := str(getattr(tool, "name", "") or "").strip())]


def active_react_scope_safe() -> str:
    """Return the active ReAct scope, or an empty value outside a live turn."""
    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        return _ctx.active_react_scope()
    except Exception:  # noqa: BLE001 - scope is optional off-turn
        return ""


def record_submit_audit(
    reason: str,
    *,
    agent_id: str,
    field: str,
    text: str,
    suppressed: bool,
) -> None:
    """Emit one queryable V2-path stream-audit record."""
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stream_audit(
        "bridge.contract_field",
        agent_id=agent_id or "",
        field=field,
        chunk_len=len(text),
        visible=False,
        duplicate_suppressed=suppressed,
        duplicate_reason=reason,
        head=text[:120],
        full_text=text[:12000],
    )


def _declared_default(field: Any) -> tuple[bool, Any]:
    """``(True, default)`` for an output field declared with a Pydantic default."""
    from pydantic_core import PydanticUndefined  # noqa: PLC0415

    default = getattr(field, "default", PydanticUndefined)
    if default is not PydanticUndefined:
        return True, default
    factory: Callable[[], Any] | None = getattr(field, "default_factory", None)
    if factory is not None:
        return True, factory()
    return False, None


def _json_schema(annotation: Any) -> dict[str, Any]:
    try:
        return pydantic.TypeAdapter(annotation).json_schema()
    except (pydantic.PydanticSchemaGenerationError, TypeError, ValueError):
        return {"type": "string"}
