"""Find what the agent can offer beyond its list: by words or by category (OL-D11).

The session facts name only some of the plugins and connectors a conversation could take
(:func:`clio_agent.gact.plugin_offer.offerable_facts`), and say how many more there are.
``offer_plugin`` reaches the rest:

* ``search`` (words, any case): a row matches when every word is in its id, name,
  summary, category or keywords; rows whose name or id holds a word come first.
* ``category`` (any case, exact): a connector's category is its Discover group, a
  plugin's the category its marketplace entry declares (else none).
* Neither (and no plugin): the categories with their counts and the total, and a line
  saying how to search, never the whole list.

Both reach every candidate, the ones the person left out in Settings too: each row says
whether the session facts list it (``listed``) and marks a left-out one
(``left_out_by_person``), so the agent can say it exists and leave the choice to the
person. Results are capped at ``plugins.offer_listed``, with how many ``more``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: The mark on a row the person left out in Settings ("Plugins CLIO can offer").
LEFT_OUT = "left_out_by_person"
#: Fields copied into a returned row (the model's view: no keys, no keywords).
_ROW_FIELDS = ("kind", "id", "name", "summary", "category", "marketplace", "enabled")


def _haystack(row: Mapping[str, Any]) -> str:
    parts = [row.get("id"), row.get("name"), row.get("summary"), row.get("category")]
    parts += list(row.get("keywords") or ())
    return " ".join(str(part or "") for part in parts).casefold()


def words_of(text: str) -> list[str]:
    """The search words of ``text`` (lowercased, split on spaces and commas)."""

    return [word for word in text.replace(",", " ").casefold().split() if word]


def matching(rows: Sequence[Mapping[str, Any]], words: Sequence[str]) -> list[Any]:
    """The rows holding every word, those with a word in their name or id first."""

    hits = [row for row in rows if all(word in _haystack(row) for word in words)]

    def named(row: Mapping[str, Any]) -> bool:
        title = f"{row.get('id')} {row.get('name')}".casefold()
        return any(word in title for word in words)

    return [row for row in hits if named(row)] + [row for row in hits if not named(row)]


def in_category(rows: Sequence[Mapping[str, Any]], category: str) -> list[Any]:
    wanted = category.strip().casefold()
    return [row for row in rows if str(row.get("category") or "").casefold() == wanted]


def categories(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each category with how many rows it holds, the largest first; uncategorized last."""

    counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get("category") or "")
        counts[name] = counts.get(name, 0) + 1
    named = sorted(
        ((name, count) for name, count in counts.items() if name),
        key=lambda pair: (-pair[1], pair[0].casefold()),
    )
    out = [{"category": name, "count": count} for name, count in named]
    if counts.get(""):
        out.append({"category": "", "count": counts[""]})
    return out


def returned(row: Mapping[str, Any], listed: set[str], left_out: set[str]) -> dict[str, Any]:
    """A row as a search returns it, with whether it is listed and whether left out."""

    from clio_agent.gact.offer_list import entry_key  # noqa: PLC0415

    key = entry_key(row)
    out = {field: row[field] for field in _ROW_FIELDS if row.get(field) not in (None, "")}
    if row.get("kind") == "connector":
        out["enabled"] = bool(row.get("enabled"))
    out["listed"] = key in listed
    if key in left_out:
        out[LEFT_OUT] = True
    return out


def search_result(
    rows: Sequence[Mapping[str, Any]],
    listed_rows: Sequence[Mapping[str, Any]],
    left_out: set[str],
    cap: int,
    *,
    search: str = "",
    category: str = "",
) -> dict[str, Any]:
    """``offer_plugin``'s answer to a search or a category (capped, with ``more``)."""

    from clio_agent.gact.offer_list import entry_key  # noqa: PLC0415

    found = list(rows)
    if category.strip():
        found = in_category(found, category)
    words = words_of(search)
    if words:
        found = matching(found, words)
    listed = {entry_key(row) for row in listed_rows}
    shown = [returned(row, listed, left_out) for row in found[:cap]]
    asked = " and ".join(
        part for part in (f'"{search.strip()}"' if words else "", category.strip()) if part
    )
    out: dict[str, Any] = {"matches": shown, "count": len(found), "more": max(0, len(found) - cap)}
    if search.strip():
        out["search"] = search.strip()
    if category.strip():
        out["category"] = category.strip()
    if not found:
        out["message"] = f"Nothing the person can add matches {asked}."
        return out
    left = sum(1 for row in shown if row.get(LEFT_OUT))
    message = f"{len(found)} found for {asked}" + (
        f"; the first {cap} shown" if out["more"] else ""
    )
    if left:
        message += f". The person left {left} of these out of the list in Settings"
    out["message"] = message + "."
    return out


def overview(
    rows: Sequence[Mapping[str, Any]], listed_count: int, left_out: set[str]
) -> dict[str, Any]:
    """``offer_plugin`` with nothing named: the categories, the totals and how to search."""

    from clio_agent.gact.offer_list import entry_key  # noqa: PLC0415

    left = sum(1 for row in rows if entry_key(row) in left_out)
    return {
        "categories": categories(rows),
        "total": len(rows),
        "listed": listed_count,
        "left_out": left,
        "message": (
            f"{len(rows)} plugins and connectors can be added to this conversation "
            f"({listed_count} listed in the session facts, {left} left out by the person in "
            "Settings). offer_plugin with search (words) or category finds them."
            if rows
            else "Nothing more can be added to this conversation."
        ),
    }
