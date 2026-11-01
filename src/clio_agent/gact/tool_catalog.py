"""The catalog of an agent's deferred tools, and how ``search_tools`` searches it.

One catalog serves the ``search_tools`` tool and ``run_code``'s ``tools.search``. Each
entry is a deferred tool with its owner (the MCP server or plugin), description,
argument names and JSON schema. :meth:`ToolCatalog.search` takes three query forms:

- ``select:a,b`` returns exactly those tools;
- ``+term rest`` keeps tools whose name contains ``term`` and ranks them by ``rest``;
- plain keywords rank by BM25 over name, description, argument names and owner.

Names are tokenized whole and on ``_``/``__``/``-``, and any word of the query that is
exactly a tool's name puts that tool first, ahead of the ranked results, so a search
that names a tool always finds it (the miss Codex issues #21503 and #56973 describe).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["CatalogEntry", "ToolCatalog"]

_WORD = re.compile(r"[a-z0-9]+")
_K1, _B = 1.2, 0.75


#: Words that say nothing about which tool is meant.
_STOP = frozenset(
    "a an and are as at be by for from in into is it its of on or the to this that with "
    "you your".split()
)


def _tokens(text: str) -> list[str]:
    return [word for word in _WORD.findall(text.lower()) if word not in _STOP]


@dataclass(frozen=True)
class CatalogEntry:
    """One deferred tool as the catalog knows it."""

    name: str
    owner: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def document(self) -> list[str]:
        """The tokens BM25 ranks: the name weighted, then the rest once."""
        properties = (
            self.parameters.get("properties") if isinstance(self.parameters, Mapping) else {}
        )
        args = " ".join(properties or {})
        name = _tokens(self.name)
        return [
            *name,
            *name,
            *name,
            self.name.lower(),
            *_tokens(self.owner),
            *_tokens(self.description),
            *_tokens(args),
        ]


class ToolCatalog:
    """The deferred tools of one agent, searchable by name or keywords."""

    def __init__(self, entries: Iterable[CatalogEntry] = ()) -> None:
        self._entries: dict[str, CatalogEntry] = {}
        for entry in entries:
            self._entries[entry.name] = entry

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def names(self) -> list[str]:
        return sorted(self._entries)

    def entry(self, name: str) -> CatalogEntry | None:
        return self._entries.get(name)

    def add(self, entry: CatalogEntry) -> None:
        """Add or replace a tool (a deferred server that announced a new one)."""
        self._entries[entry.name] = entry

    def remove(self, name: str) -> None:
        self._entries.pop(name, None)

    def by_owner(self) -> dict[str, list[CatalogEntry]]:
        """Entries grouped by owner, owners and names sorted (a stable prompt entry)."""
        groups: dict[str, list[CatalogEntry]] = {}
        for name in sorted(self._entries):
            entry = self._entries[name]
            groups.setdefault(entry.owner, []).append(entry)
        return dict(sorted(groups.items()))

    def search(self, query: str, limit: int = 8) -> tuple[list[CatalogEntry], list[str]]:
        """Matching entries, best first, and any ``select:`` names that do not exist."""
        query = query.strip()
        if query.lower().startswith("select:"):
            wanted = [name.strip() for name in query[7:].split(",") if name.strip()]
            found = [self._entries[name] for name in wanted if name in self._entries]
            return found[: max(limit, len(found))], [n for n in wanted if n not in self._entries]
        required = ""
        if query.startswith("+"):
            required, _, query = query[1:].partition(" ")
            required = required.lower()
        pool = [
            entry
            for entry in self._entries.values()
            if not required or required in entry.name.lower()
        ]
        allowed = {entry.name for entry in pool}
        exact = [word for word in query.split() if word in allowed]
        ranked = (
            self._rank(pool, _tokens(query))
            if query.strip()
            else sorted(pool, key=lambda e: e.name)
        )
        ordered = dict.fromkeys([*exact, *(entry.name for entry in ranked)])
        return [self._entries[name] for name in ordered][:limit], []

    def _rank(self, pool: list[CatalogEntry], terms: list[str]) -> list[CatalogEntry]:
        documents = {entry.name: entry.document() for entry in pool}
        if not documents or not terms:
            return []
        average = sum(len(d) for d in documents.values()) / len(documents)
        frequency = Counter(term for doc in documents.values() for term in set(doc))
        scores: dict[str, float] = {}
        for name, doc in documents.items():
            counts, length, score = Counter(doc), len(doc), 0.0
            for term in terms:
                if term not in counts:
                    continue
                idf = math.log(
                    1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5)
                )
                tf = counts[term]
                score += idf * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * length / average))
            if score > 0:
                scores[name] = score
        by_name = {entry.name: entry for entry in pool}
        return [by_name[name] for name in sorted(scores, key=lambda n: (-scores[n], n))]
