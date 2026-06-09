"""HDF5 skill bundle loader.

Surfaces the bundled ``SKILL.md`` files as a small in-process API:

- ``list_skills()`` enumerates available skills with their frontmatter.
- ``load_skill(name)`` returns the body of one skill, with a path-traversal
  guard against arbitrary filesystem reads.
- ``match_skills(query)`` scores skills by overlap between the query and
  each skill description's trigger phrases.

The expert never bulk-loads bodies. They flow through the
``hdf5_consult_skill`` MCP tool on demand.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

_SKILLS_ROOT = Path(__file__).resolve().parent
_SKILL_FILE_NAME = "SKILL.md"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# Trigger phrases inside SKILL.md descriptions are double-quoted, comma-separated.
_TRIGGER_PHRASE_RE = re.compile(r'"([^"]+)"')


class SkillSummary(TypedDict):
    """Public, JSON-safe view of one skill's frontmatter."""

    name: str
    description: str
    version: str
    triggers: list[str]


class SkillNotFoundError(KeyError):
    """Raised when ``load_skill`` is asked for a name that doesn't exist."""


def _safe_skill_dir(name: str) -> Path:
    """Resolve ``<name>`` to its skill dir, refusing path-traversal attempts.

    Skill names are required to be flat — no path separators, no ``..``
    components, no leading ``.`` — so that names like
    ``hdf5-chunking/../hdf5-filters`` are rejected even though they
    would resolve to a legitimate skill directory.
    """
    if (
        not name
        or "/" in name
        or "\\" in name
        or ".." in name
        or name.startswith(".")
    ):
        raise SkillNotFoundError(name)
    candidate = (_SKILLS_ROOT / name).resolve()
    try:
        candidate.relative_to(_SKILLS_ROOT)
    except ValueError as exc:
        raise SkillNotFoundError(name) from exc
    if not candidate.is_dir() or not (candidate / _SKILL_FILE_NAME).is_file():
        raise SkillNotFoundError(name)
    return candidate


def _parse_frontmatter(body: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md body into ``(frontmatter dict, remaining body)``."""
    match = _FRONTMATTER_RE.match(body)
    if not match:
        return {}, body
    fm_text = match.group(1)
    fm: dict[str, str] = {}
    current_key: str | None = None
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and current_key:
            fm[current_key] += " " + line.strip()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Only strip surrounding quotes when they form a matched pair around
        # the whole value. Single-line descriptions in the bundled SKILL.md
        # frontmatter contain internal quoted trigger phrases — stripping
        # those leading/trailing quotes piecewise corrupts the trigger list.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        fm[key] = value
        current_key = key
    return fm, body[match.end() :]


def _extract_triggers(description: str) -> list[str]:
    """Pull double-quoted trigger phrases out of a skill description."""
    return [m.lower() for m in _TRIGGER_PHRASE_RE.findall(description)]


@lru_cache(maxsize=1)
def _skill_index() -> dict[str, SkillSummary]:
    """Build the in-memory skill table once; reused for every list/match call."""
    index: dict[str, SkillSummary] = {}
    for child in sorted(_SKILLS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        skill_file = child / _SKILL_FILE_NAME
        if not skill_file.is_file():
            continue
        fm, _body = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        name = fm.get("name") or child.name
        description = fm.get("description", "")
        index[name] = {
            "name": name,
            "description": description,
            "version": fm.get("version", ""),
            "triggers": _extract_triggers(description),
        }
    return index


def list_skills() -> list[SkillSummary]:
    """Return summaries for every bundled skill, sorted by name."""
    return sorted(_skill_index().values(), key=lambda s: s["name"])


def skill_names() -> list[str]:
    """Return just the skill names, sorted."""
    return sorted(_skill_index().keys())


def load_skill(name: str) -> str:
    """Return the full SKILL.md body (including frontmatter) for ``name``.

    Raises ``SkillNotFoundError`` for unknown names or path-traversal attempts.
    """
    skill_dir = _safe_skill_dir(name)
    return (skill_dir / _SKILL_FILE_NAME).read_text(encoding="utf-8")


_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Common English filler. Removed from the query before matching so that
# "I want to rechunk my dataset" reduces to {rechunk, dataset}.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "i", "you", "we", "they", "it", "this", "that", "these", "those",
        "my", "our", "your", "their", "its",
        "to", "of", "in", "on", "at", "for", "with", "by", "from", "as",
        "do", "does", "did", "have", "has", "had", "can", "could", "should",
        "would", "may", "might", "will", "want", "need", "how", "what", "when",
        "where", "why", "which", "who", "whose", "if", "and", "or", "but",
        "so", "than", "then", "use", "using", "make", "making",
    }
)


def _tokenize(text: str) -> set[str]:
    """Lowercase + tokenize on alphanumerics; strip stopwords; light stem.

    Plural-ish endings are stripped so that ``dataset`` and ``datasets``
    collapse to one token. We keep this crude — Porter-style stemming
    would be overkill for a 24-document corpus.
    """
    raw = _TOKEN_RE.findall(text.lower())
    out: set[str] = set()
    for tok in raw:
        if len(tok) <= 2 or tok in _STOPWORDS:
            continue
        if len(tok) > 4 and tok.endswith("ies"):
            tok = tok[:-3] + "y"
        elif len(tok) > 4 and tok.endswith(("es", "ed")):
            tok = tok[:-2]
        elif len(tok) > 4 and tok.endswith(("s", "y")):
            tok = tok[:-1]
        elif len(tok) > 5 and tok.endswith("ing"):
            tok = tok[:-3]
        out.add(tok)
    return out


def match_skills(query: str, *, top_k: int = 5) -> list[tuple[str, float]]:
    """Score skills by token overlap between ``query`` and each skill's
    name + description (which already concatenates the trigger phrases and
    the prose intent).

    Score = ``|query_tokens ∩ description_tokens|``. Higher = better.
    Crude prefix-stemming makes ``dataset`` match ``datasets`` and
    ``compliant`` match ``compliance``. Stopwords are dropped before
    matching so filler like "how do I" doesn't pull noise into the score.

    Returns up to ``top_k`` ``(name, score)`` pairs, highest score first.
    Empty list if nothing relevant matches.
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    scored: list[tuple[str, float]] = []
    for summary in _skill_index().values():
        doc_tokens = _tokenize(summary["name"] + " " + summary["description"])
        hits = query_tokens & doc_tokens
        if hits:
            scored.append((summary["name"], float(len(hits))))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]


__all__ = [
    "SkillNotFoundError",
    "SkillSummary",
    "list_skills",
    "load_skill",
    "match_skills",
    "skill_names",
]
