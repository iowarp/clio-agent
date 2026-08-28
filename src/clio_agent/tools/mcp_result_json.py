"""A narrow ``json.dumps(default=...)`` hook for typed MCP results.

Split out of ``mcp_executor.py`` (Finding E follow-up) rather than appended
there: that module sits exactly at its size-ratchet baseline
(``scripts/check_file_size.py``), so a real, if small, serialization concern
gets its own owner module instead of accreting onto a file with zero slack.

The hook here is deliberately narrow: it recognizes ONLY ``pydantic.BaseModel``
instances (a FastMCP client can hand back a generated typed result, e.g. a
``Root`` model, in place of the plain dict ``_result_to_text`` otherwise
JSON-encodes) and re-raises ``TypeError`` for everything else. It must never
swallow a genuinely unserializable object into a stringified placeholder --
that would silently defeat ``_result_to_text``'s own repr fallback and its
typed ``mcp_result_to_text_repr_fallback`` reason, the no-silent-fallback
contract this package's degradation paths follow.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def pydantic_json_default(obj: Any) -> Any:
    """Return a JSON-encodable projection of a pydantic ``BaseModel``.

    ``json.dumps`` calls its ``default=`` hook for any value it cannot encode
    natively -- including one nested inside a list/dict, not only a bare
    top-level result -- so passing this as that hook serializes a typed MCP
    result correctly however deep it sits, in one ``json.dumps`` pass. This is
    a real, lossless serialization path (``model_dump(mode="json")`` produces
    the same JSON-compatible projection Pydantic itself would emit), not a
    degradation, so a caller using it must NOT record a fallback reason for
    values this hook handles.

    Args:
        obj: The value ``json.dumps`` could not encode natively.

    Returns:
        The value's JSON-mode ``model_dump`` projection.

    Raises:
        TypeError: ``obj`` is not a pydantic ``BaseModel``. The caller's own
            ``except`` clause is expected to catch this and apply its
            (typed-reason-logging) repr fallback -- this hook only ADDS a
            serialization path, it never suppresses that one.
    """

    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


__all__ = ["pydantic_json_default"]
