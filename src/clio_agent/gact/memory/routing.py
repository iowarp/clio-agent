"""Where a fact the user asked to remember belongs: one typed model call.

The model sees the fact, its subject and category, and the index of that category's
documents (name and description), and names an existing document or a new one. The
server checks the answer against the index and writes the line; nothing here merges
or rewrites facts.
"""

from __future__ import annotations

import dspy

from clio_agent.arc.memory_document import MemoryDocumentError, check_slug
from clio_agent.errors import ClioError


class MemoryRouteError(ClioError):
    """The routing answer was not usable (typed reason)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, error_type=reason, details={"reason": reason})


class RouteMemoryFact(dspy.Signature):
    """Choose the memory document a fact the user asked to remember belongs in.

    Memory holds one document per subject. Pick an existing document only when the fact
    is about that document's subject; otherwise name a new document for the subject.
    """

    fact: str = dspy.InputField(desc="The fact to remember, as the user stated it.")
    subject: str = dspy.InputField(desc="What the fact is about.")
    category: str = dspy.InputField(desc="The memory category the fact belongs to.")
    documents: list[str] = dspy.InputField(
        desc="This category's documents, one 'name: description' per entry (may be empty)."
    )
    existing: str = dspy.OutputField(
        desc="The name of the existing document the fact belongs in, or an empty string."
    )
    new_name: str = dspy.OutputField(
        desc="When no existing document fits: a short name of lowercase words joined by '-'."
    )
    new_description: str = dspy.OutputField(
        desc="When no existing document fits: one line saying what the new document holds."
    )


def route_fact(
    *, fact: str, subject: str, category: str, documents: list[tuple[str, str]]
) -> tuple[str, str]:
    """Return ``(slug, description)``; ``description`` is empty for an existing document.

    Raises:
        MemoryRouteError: No session model is active, or the answer names neither a
            listed document nor a valid new name.
    """
    lm = dspy.settings.lm
    if lm is None:
        raise MemoryRouteError("memory_route_no_model", "no session model to route the fact")
    names = {name for name, _ in documents}
    prediction = dspy.Predict(RouteMemoryFact)(
        fact=fact,
        subject=subject or fact,
        category=category,
        documents=[f"{name}: {description}" for name, description in documents],
    )
    existing = str(getattr(prediction, "existing", "") or "").strip()
    if existing:
        if existing not in names:
            raise MemoryRouteError(
                "memory_route_invalid", f"routing named {existing!r}, which is not a document"
            )
        return existing, ""
    new_name = str(getattr(prediction, "new_name", "") or "").strip()
    try:
        slug = check_slug(new_name)
    except MemoryDocumentError:
        raise MemoryRouteError(
            "memory_route_invalid", f"routing named {new_name!r}, which is not a document name"
        ) from None
    if slug in names:
        return slug, ""
    description = " ".join(str(getattr(prediction, "new_description", "") or "").split())
    return slug, description or subject


__all__ = ["MemoryRouteError", "RouteMemoryFact", "route_fact"]
