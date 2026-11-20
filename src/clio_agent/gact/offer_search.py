"""Find what the agent can offer beyond its list: by words or by category (OL-D11).

The session facts name only some of the plugins and connectors a conversation could take
(:func:`clio_agent.gact.plugin_offer.offerable_facts`), and say how many more there are.
``offer_plugin`` reaches the rest:

* ``search`` (words, any case, a plain sentence's filler words dropped): a row matches
  when every word is in its id, name, description, category, keywords or, for a plugin,
  the instructions its MCP servers last sent (what they reach: sites, datasets,
  packages), or when the search holds its id, name or a declared keyword as a whole word
  (a connector also answers to the plugins that follow it); rows whose name or id holds
  a word come first, and a row matched by its instructions quotes the sentence
  (``mentions``).
* ``category`` (any case, exact): a connector's category is its Discover group, a
  plugin's the category its marketplace entry declares (else none).
* Neither (and no plugin): the categories with their counts and the total, and a line
  saying how to search, never the whole list.

Both reach every candidate, the ones the person left out in Settings too: each row says
whether the session facts list it (``listed``) and marks a left-out one
(``left_out_by_person``). Leaving an entry out only removes it from the list the agent
is shown; ``offer_plugin`` offers it the same way as any other (OL-F6), and the result
says so as a fact, with where the person made that choice (OL-F5). Results are capped at
``plugins.offer_listed``, with how many ``more``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from clio_agent.gact.offer_list import SETTINGS_PLACE, entry_key

#: The mark on a row the person left out in Settings (:data:`SETTINGS_PLACE`).
LEFT_OUT = "left_out_by_person"
#: The session facts' name for the entries with no category (``offer_plugin`` takes it).
UNCATEGORIZED = "uncategorized"
#: Fields copied into a returned row (the model's view: no keys, no keywords).
_ROW_FIELDS = ("kind", "id", "name", "summary", "category", "marketplace", "enabled")


def left_out_fact(count: int) -> str:
    """What ``count`` left-out entries are: not in the agent's plugin list, by the person's
    choice in Settings, and offered the same way as any other. Facts that neither forbid
    nor discourage an offer (OL-F5, OL-F6)."""

    one = count == 1
    return (
        f"{count} of these {'is' if one else 'are'} not in your plugin list (the person's "
        f"choice in {SETTINGS_PLACE}); offer_plugin offers {'it' if one else 'them'} the "
        "same way"
    )


def _haystack(row: Mapping[str, Any]) -> str:
    parts = [row.get("id"), row.get("name"), row.get("summary"), row.get("about")]
    parts += [row.get("category"), *(row.get("keywords") or ())]
    parts.append(row.get("instructions"))
    return " ".join(str(part or "") for part in parts).casefold()


#: How much of the instructions sentence that holds a search word a result quotes.
MENTION_CHARS = 240
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def _quoted(sentence: str, at: int) -> str:
    """At most :data:`MENTION_CHARS` of ``sentence``, keeping the word at ``at`` in view."""

    if len(sentence) <= MENTION_CHARS:
        return sentence
    start = max(0, min(at - MENTION_CHARS // 3, len(sentence) - MENTION_CHARS + 2))
    if start:
        start = sentence.rfind(" ", 0, start + 1) + 1
    end = start + MENTION_CHARS - (2 if start else 1)
    piece = sentence[start:end]
    if end < len(sentence):
        piece = piece.rsplit(" ", 1)[0] + "…"
    return ("…" + piece) if start else piece


def mention(row: Mapping[str, Any], words: Sequence[str]) -> str:
    """The first sentence of ``row``'s server instructions holding a search word that its
    id, name, summary, category and keywords do not ("" when none): why it matched."""

    own = " ".join(
        str(part or "")
        for part in (
            row.get("id"),
            row.get("name"),
            row.get("summary"),
            row.get("about"),
            row.get("category"),
            *(row.get("keywords") or ()),
        )
    ).casefold()
    missing = [word for word in words if word not in own]
    for sentence in _SENTENCE_END.split(str(row.get("instructions") or "")):
        sentence = " ".join(sentence.split())
        folded = sentence.casefold()
        hits = [folded.find(word) for word in missing if word in folded]
        if hits:
            return _quoted(sentence, min(hits))
    return ""


#: Words a need said in plain words carries that name nothing to find ("in my notebook").
_FILLER = frozenset(
    "a an the my our your me i we us it its this that these those to of in on at for from "
    "with by and or into onto as is are be please can could would should will".split()
)


def words_of(text: str) -> list[str]:
    """The search words of ``text`` (lowercased, split on spaces and commas, without the
    filler words of a plain sentence)."""

    return [w for w in text.replace(",", " ").casefold().split() if w and w not in _FILLER]


def _said_in(phrase: Any, asked: str) -> bool:
    folded = " ".join(str(phrase or "").casefold().split())
    return bool(folded) and re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", asked) is not None


def found_by(row: Mapping[str, Any], words: Sequence[str]) -> bool:
    """Whether ``row`` answers a search: every word is in what it says of itself (id, name,
    description, category, keywords, its servers' instructions), or the search names it
    or one of its declared keywords as a whole word ("prepare Spack", "field notebook")."""

    if not words:
        return False
    if all(word in _haystack(row) for word in words):
        return True
    asked = " ".join(words)
    named = (row.get("id"), row.get("name"), *(row.get("keywords") or ()))
    return any(_said_in(phrase, asked) for phrase in named)


def matching(rows: Sequence[Mapping[str, Any]], words: Sequence[str]) -> list[Any]:
    """The rows a search finds (:func:`found_by`), those with a word in their name or id
    first."""

    hits = [row for row in rows if found_by(row, words)]

    def named(row: Mapping[str, Any]) -> bool:
        title = f"{row.get('id')} {row.get('name')}".casefold()
        return any(word in title for word in words)

    return [row for row in hits if named(row)] + [row for row in hits if not named(row)]


def in_category(rows: Sequence[Mapping[str, Any]], category: str) -> list[Any]:
    """The rows of ``category``; :data:`UNCATEGORIZED` (as the session facts count them)
    holds the rows with none."""

    wanted = category.strip().casefold()
    if wanted == UNCATEGORIZED:
        wanted = ""
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


def named_beyond(row: Mapping[str, Any]) -> bool:
    """Whether the session facts name ``row`` past the list (OL-D17): a connector the
    person turned on, or a plugin they installed (every plugin row is one of their
    library). The rest are only counted, so a catalog of hundreds costs a few words."""

    return row.get("kind") != "connector" or bool(row.get("enabled"))


def unlisted(rows: Sequence[Mapping[str, Any]], left_out: set[str]) -> list[dict[str, Any]]:
    """The categories of ``rows`` (the entries the session facts do not list) with their
    counts, and the names of the ones the person turned on or installed and did not leave
    out (:func:`named_beyond`, OL-D17). Every other entry is counted, never named."""

    out = categories(rows)
    for group in out:
        wanted = str(group["category"]).casefold()
        group["names"] = [
            str(row.get("name") or row.get("id"))
            for row in rows
            if str(row.get("category") or "").casefold() == wanted
            and named_beyond(row)
            and entry_key(row) not in left_out
        ]
    return out


def returned(row: Mapping[str, Any], listed: set[str], left_out: set[str]) -> dict[str, Any]:
    """A row as a search returns it, with whether it is listed and whether left out."""

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

    found = list(rows)
    if category.strip():
        found = in_category(found, category)
    words = words_of(search)
    if words:
        found = matching(found, words)
    listed = {entry_key(row) for row in listed_rows}
    shown = [returned(row, listed, left_out) for row in found[:cap]]
    for row, out_row in zip(found[:cap], shown, strict=True):
        if words and (said := mention(row, words)):
            out_row["mentions"] = said
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
        message += f". {left_out_fact(left)}"
    out["message"] = message + "."
    return out


def overview(
    rows: Sequence[Mapping[str, Any]], listed_count: int, left_out: set[str]
) -> dict[str, Any]:
    """``offer_plugin`` with nothing named: the categories, the totals and how to search."""

    left = sum(1 for row in rows if entry_key(row) in left_out)
    return {
        "categories": categories(rows),
        "total": len(rows),
        "listed": listed_count,
        "left_out": left,
        "message": (
            f"{len(rows)} plugins and connectors can be added to this conversation "
            f"({listed_count} in your plugin list in the session facts; {left} not in it, "
            f"by the person's choice in {SETTINGS_PLACE}, which offer_plugin offers the same "
            "way). offer_plugin with search (words) or category finds them."
            if rows
            else "Nothing more can be added to this conversation."
        ),
    }
