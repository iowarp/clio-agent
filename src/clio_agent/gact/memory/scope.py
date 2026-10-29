"""Which memory a session sees, and how memory records are named.

``user/`` is the account that owns the CLIO server (memory ledger MD3).
``project/<workspace_id>/`` is one workspace. A session in ``ws_global`` sees user
scope only; a session in a workspace sees user scope plus its own project scope, or
only its project scope when the workspace turned **Use account memory** off.

A document's record name is ``<scope>/<category>/<slug>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clio_agent.arc.memory_document import CATEGORIES, MemoryDocumentError, check_slug
from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID

USER_SCOPE = "user"
_WORKSPACE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


def project_scope(workspace_id: str) -> str:
    """The project scope of ``workspace_id``."""
    if not _WORKSPACE_ID.match(workspace_id) or workspace_id == GLOBAL_WORKSPACE_ID:
        raise MemoryDocumentError(f"{workspace_id!r} cannot hold project memory")
    return f"project/{workspace_id}"


def check_scope(scope: str) -> str:
    """Return ``scope`` when it is ``user`` or a valid project scope."""
    if scope == USER_SCOPE:
        return scope
    head, sep, workspace_id = scope.partition("/")
    if head != "project" or not sep:
        raise MemoryDocumentError(f"unknown memory scope {scope!r}")
    return project_scope(workspace_id)


def session_scopes(workspace_id: str, *, use_account_memory: bool = True) -> tuple[str, ...]:
    """The scopes a session in ``workspace_id`` reads, broadest first."""
    if not workspace_id or workspace_id == GLOBAL_WORKSPACE_ID:
        return (USER_SCOPE,)
    project = project_scope(workspace_id)
    return (USER_SCOPE, project) if use_account_memory else (project,)


@dataclass(frozen=True)
class RecordName:
    """A document's address: scope, category and slug."""

    scope: str
    category: str
    slug: str

    def __str__(self) -> str:
        return f"{self.scope}/{self.category}/{self.slug}"


def record_name(scope: str, category: str, slug: str) -> RecordName:
    """A checked record name."""
    if category not in CATEGORIES:
        raise MemoryDocumentError(f"unknown category {category!r}")
    return RecordName(check_scope(scope), category, check_slug(slug))


def parse_record_name(name: str) -> RecordName:
    """Split ``<scope>/<category>/<slug>``.

    Raises:
        MemoryDocumentError: ``name`` is not a memory document address.
    """
    parts = name.split("/")
    if parts[:1] == [USER_SCOPE] and len(parts) == 3:
        return record_name(USER_SCOPE, parts[1], parts[2])
    if parts[:1] == ["project"] and len(parts) == 4:
        return record_name(f"project/{parts[1]}", parts[2], parts[3])
    raise MemoryDocumentError(f"{name!r} is not a memory document name")


def scopes_name_re(scopes: tuple[str, ...], category: str | None = None) -> str:
    """One regex selecting the documents of ``scopes`` (optionally one category)."""
    if not scopes:
        raise MemoryDocumentError("a memory search needs at least one scope")
    alternatives = "|".join(re.escape(check_scope(scope)) for scope in scopes)
    if category is not None and category not in CATEGORIES:
        raise MemoryDocumentError(f"unknown category {category!r}")
    middle = re.escape(category) if category else "[a-z]+"
    return f"(?:{alternatives})/{middle}/[a-z0-9-]+"


__all__ = [
    "USER_SCOPE",
    "RecordName",
    "check_scope",
    "parse_record_name",
    "project_scope",
    "record_name",
    "scopes_name_re",
    "session_scopes",
]
