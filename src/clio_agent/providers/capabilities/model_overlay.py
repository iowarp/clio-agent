"""The real model overlay source (model-capabilities brief Part 8; P6).

Replaces :class:`~clio_agent.providers.capabilities.model_sources.EmptyOverlaySource`
(brief 5.1 layer 2). Three overlay ROOTS are consulted, in this fixed order
(brief Part 8.6, "clio-coder's order"):

1. **fetched** -- the compiled community catalog
   (:mod:`clio_agent.providers.model_discovery.model_overlay_catalog`), read
   disk-cache/bundled-only (never a blocking network call from a per-model
   handshake lookup -- see :func:`_fetched_entries`).
2. **user** -- ``<user config dir>/model-catalog.d/*.yaml``.
3. **project** -- ``<cwd>/.clio/model-catalog.d/*.yaml``.

Within one root, entries are the clio-coder knowledge-base shape: ``family``,
``matchPatterns`` (case-insensitive substrings), ``capabilities``, optional
``quirks``. **Longest matching pattern wins overall; a tie is broken by the
LATER root** (brief Part 8.6) -- a project override beats a user override
beats the community catalog for the exact same pattern length. This is the
same substring-longest-match rule clio-coder's own ``FileKnowledgeBase.lookup``
implements (``needle.includes(pattern)``, longest ``pattern`` wins), extended
with the multi-root tie-break clio-agent adds.

The SAME matcher (:func:`best_overlay_match`) backs two call sites:

* :func:`overlay_match_for_link` -- brief 5.4 rule 3, wired into
  :func:`clio_agent.providers.capabilities.link.link_model` at every
  handshake adapter's ``deployment_model_key_fact`` call.
* :class:`CatalogOverlaySource` -- brief 5.1 layer 2, wired as the default
  ``overlay`` in
  :func:`clio_agent.providers.capabilities.model_sources.resolve_model_capabilities`.

A deployment link (rule 3) resolves a wire id to the matched entry's
``family`` name as its ``model_key``; the overlay-facts lookup then matches
that SAME family name against the SAME patterns (a family's own name is
always one of its own ``matchPatterns`` in every seed entry), so the two call
sites compose without a second, different key space.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from clio_agent import paths
from clio_agent.providers.capabilities.records import (
    DOMAINS,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    is_task,
    task_fact,
    unknown,
)
from clio_agent.providers.model_discovery.model_overlay_catalog import (
    ModelOverlayCatalogError,
    cached_model_overlay_entries,
)

logger = logging.getLogger(__name__)

#: Root name -> precedence rank. Higher wins a same-length-match tie (brief
#: Part 8.6: "for equal matches the later root wins"). "fetched" is listed
#: first in the load order but ranks LOWEST -- a user/project file is always
#: someone deliberately overriding the community catalog for their box.
_ROOT_RANK: dict[str, int] = {"fetched": 0, "user": 1, "project": 2}

#: clio-coder's own mechanism vocabulary (its YAML entries' literal strings) ->
#: clio-agent's :data:`~clio_agent.providers.capabilities.records.ThinkingMechanism`.
_MECHANISM_MAP: dict[str, str] = {
    "none": "none",
    "always-on": "always_on",
    "on-off": "on_off",
    "effort-levels": "effort_levels",
    "budget-tokens": "budget_tokens",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OverlayEntry:
    """One overlay row plus which root it was loaded from."""

    family: str
    match_patterns: tuple[str, ...]
    capabilities: dict[str, Any]
    quirks: dict[str, Any]
    root: str


def user_overlay_dir() -> Path:
    """``<user config dir>/model-catalog.d`` -- root 2 (brief Part 8.6)."""
    return paths.user_config_dir() / "model-catalog.d"


def project_overlay_dir(cwd: "str | Path | None" = None) -> Path:
    """``<cwd>/.clio/model-catalog.d`` -- root 3 (brief Part 8.6)."""
    return (Path(cwd) if cwd is not None else Path.cwd()) / ".clio" / "model-catalog.d"


def _entry_from_raw(raw: Any, *, root: str, source_name: str) -> OverlayEntry | None:
    """Lightweight structural check for one local-overlay entry.

    Full JSON-Schema validation (``catalogs/models/overlay.schema.json``) is a
    CI-time contract for the SEED files; that schema is not packaged (it lives
    at the repo root, not under ``src/clio_agent``), so a hand-authored local
    override gets this cheaper duck-typed check instead. A malformed entry is
    skipped with a logged reason -- never silently dropped, never fatal to the
    rest of the file (brief cleanup-program ground rule: no silent fallback).
    """

    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("family"), str)
        or not raw["family"].strip()
        or not isinstance(raw.get("matchPatterns"), list)
        or not raw["matchPatterns"]
        or not all(isinstance(p, str) and p for p in raw["matchPatterns"])
        or not isinstance(raw.get("capabilities"), dict)
    ):
        logger.warning(
            "model_overlay: reason=malformed_local_entry root=%s source=%s entry=%r",
            root,
            source_name,
            raw,
        )
        return None
    quirks = raw.get("quirks")
    return OverlayEntry(
        family=raw["family"].strip(),
        match_patterns=tuple(raw["matchPatterns"]),
        capabilities=dict(raw["capabilities"]),
        quirks=dict(quirks) if isinstance(quirks, dict) else {},
        root=root,
    )


def _entries_from_dir(directory: Path, *, root: str) -> list[OverlayEntry]:
    if not directory.is_dir():
        return []
    entries: list[OverlayEntry] = []
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "model_overlay: reason=local_overlay_unreadable root=%s path=%s: %s",
                root,
                path,
                exc,
            )
            continue
        if not isinstance(raw, list):
            logger.warning(
                "model_overlay: reason=local_overlay_not_a_list root=%s path=%s",
                root,
                path,
            )
            continue
        for item in raw:
            entry = _entry_from_raw(item, root=root, source_name=str(path))
            if entry is not None:
                entries.append(entry)
    return entries


def _fetched_entries() -> list[OverlayEntry]:
    """Root 1: the compiled community catalog, disk-cache/bundled only.

    Never performs a blocking network fetch (:func:`cached_model_overlay_entries`
    passes ``allow_fetch=False``) -- a per-model handshake lookup must not stall
    on a synchronous HTTP call, and the packaged bundled copy already makes a
    fresh install correct offline. A missing/corrupt catalog degrades to no
    entries from this root (logged), never raises -- the overlay is one of
    several model-record layers (brief 5.1), never load-bearing on its own.
    """

    entries, error = cached_model_overlay_entries()
    if entries is None:
        logger.warning("model_overlay: reason=fetched_catalog_unavailable detail=%s", error)
        return []
    result: list[OverlayEntry] = []
    for raw in entries:
        parsed = _entry_from_raw(raw, root="fetched", source_name="model-overlay.json")
        if parsed is not None:
            result.append(parsed)
    return result


def load_overlay_entries(*, cwd: "str | Path | None" = None) -> list[OverlayEntry]:
    """All overlay entries from every root, in load order (fetched, user, project).

    Load order is NOT precedence order by itself -- :func:`best_overlay_match`
    is what applies the longest-match/later-root-wins rule across the combined
    list.
    """

    return [
        *_fetched_entries(),
        *_entries_from_dir(user_overlay_dir(), root="user"),
        *_entries_from_dir(project_overlay_dir(cwd), root="project"),
    ]


@dataclass(frozen=True)
class OverlayMatch:
    """One winning :func:`best_overlay_match` result."""

    entry: OverlayEntry
    pattern: str


def best_overlay_match(candidate: str, entries: list[OverlayEntry]) -> OverlayMatch | None:
    """The longest-matchPatterns-wins entry for ``candidate`` across ``entries``.

    Matching is case-insensitive substring containment (``pattern in
    candidate``), exactly clio-coder's own ``FileKnowledgeBase.lookup``
    algorithm. Ties (same pattern length) are broken by the higher
    :data:`_ROOT_RANK` (brief Part 8.6: "the later root wins"); a further tie
    (same length, same root) keeps the first entry encountered, which is
    deterministic because :func:`load_overlay_entries` always loads in the
    same (family-sorted for "fetched", sorted-filename for local dirs) order.
    """

    if not candidate:
        return None
    needle = candidate.lower()
    best: OverlayMatch | None = None
    best_rank = -1
    for entry in entries:
        for pattern in entry.match_patterns:
            if not pattern or pattern.lower() not in needle:
                continue
            rank = _ROOT_RANK.get(entry.root, 0)
            if (
                best is None
                or len(pattern) > len(best.pattern)
                or (len(pattern) == len(best.pattern) and rank > best_rank)
            ):
                best = OverlayMatch(entry=entry, pattern=pattern)
                best_rank = rank
    return best


def overlay_match_for_link(candidate: str, *, cwd: "str | Path | None" = None) -> str | None:
    """The overlay's brief-5.4-rule-3 callable: candidate wire id/filename -> ``model_key``.

    Wired as ``overlay_match=`` at every
    :func:`clio_agent.providers.capabilities.link.deployment_model_key_fact`
    call site. Returns the matched entry's ``family`` -- never a partial guess
    (brief: "Never guess a link from a partial name outside these rules" --
    the substring match IS the rule here, not a guess on top of it).
    """

    match = best_overlay_match(candidate, load_overlay_entries(cwd=cwd))
    return match.entry.family if match is not None else None


def _modalities_fact(capabilities: dict[str, Any], *, observed_at: str, detail: str) -> Fact:
    if "vision" not in capabilities and "audio" not in capabilities:
        return unknown()
    modalities = {"text"}
    if capabilities.get("vision"):
        modalities.add("image")
    if capabilities.get("audio"):
        modalities.add("audio")
    return Fact(
        value=frozenset(modalities), source="overlay", observed_at=observed_at, detail=detail
    )


def _task_fact(capabilities: dict[str, Any], *, observed_at: str, detail: str) -> Fact:
    """The task the entry states: an explicit ``task``, else its flags.

    An explicit ``task`` (a Hub ``pipeline_tag`` id or a ``clio:<id>``, e.g.
    ``clio:weather-emulation`` for a scientific surrogate) is the entry saying
    exactly what the model does, so it wins. Otherwise clio-coder's flags are
    independent booleans; the one that names what the model PRODUCES decides:
    an embedding or rerank model is that surrogate task even if a chat flag were
    also set. No task and no flag leaves the task unknown.
    """
    explicit = capabilities.get("task")
    if explicit is not None:
        if is_task(explicit):
            return task_fact(explicit, source="overlay", observed_at=observed_at, detail=detail)
        logger.warning("model_overlay: reason=unknown_task task=%r %s", explicit, detail)
    if capabilities.get("embeddings") is True:
        value: str | None = "feature-extraction"
    elif capabilities.get("rerank") is True:
        value = "text-ranking"
    elif capabilities.get("chat") is True:
        value = "text-generation"
    else:
        value = None
    return task_fact(value, source="overlay", observed_at=observed_at, detail=detail)


def _domains_fact(capabilities: dict[str, Any], *, observed_at: str, detail: str) -> Fact:
    """The subject domains the entry's ``domains`` list states (closed :data:`DOMAINS`).

    A value outside the closed list is logged and left out, never mapped to a
    near match; an entry with no ``domains`` key states nothing.
    """
    raw = capabilities.get("domains")
    if raw is None:
        return unknown()
    values = raw if isinstance(raw, list) else [raw]
    domains = frozenset(value for value in values if value in DOMAINS)
    rejected = [value for value in values if value not in DOMAINS]
    if rejected:
        logger.warning("model_overlay: reason=unknown_domain domains=%r %s", rejected, detail)
    if not domains:
        return unknown()
    return Fact(value=domains, source="overlay", observed_at=observed_at, detail=detail)


def _bool_fact(capabilities: dict[str, Any], key: str, *, observed_at: str, detail: str) -> Fact:
    value = capabilities.get(key)
    if not isinstance(value, bool):
        return unknown()
    return Fact(value=value, source="overlay", observed_at=observed_at, detail=detail)


def _structured_output_fact(capabilities: dict[str, Any], *, observed_at: str, detail: str) -> Fact:
    if "structuredOutputs" not in capabilities:
        return unknown()
    return Fact(
        value=bool(capabilities.get("structuredOutputs")),
        source="overlay",
        observed_at=observed_at,
        detail=detail,
    )


def _int_fact(capabilities: dict[str, Any], key: str, *, observed_at: str, detail: str) -> Fact:
    value = capabilities.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return unknown()
    return Fact(value=value, source="overlay", observed_at=observed_at, detail=detail)


def _first_template_kwarg(*sources: Any) -> str | None:
    for source in sources:
        if isinstance(source, dict) and source:
            first_key = next(iter(source), None)
            if isinstance(first_key, str) and first_key:
                return first_key
    return None


def _thinking_fact(entry: OverlayEntry, *, observed_at: str, detail: str) -> Fact:
    reasoning = entry.capabilities.get("reasoning")
    if not isinstance(reasoning, bool):
        return unknown()
    if not reasoning:
        return Fact(
            value=ThinkingSpec(mechanism="none"),
            source="overlay",
            observed_at=observed_at,
            detail=detail,
        )
    thinking_quirks = entry.quirks.get("thinking")
    if not isinstance(thinking_quirks, dict):
        # reasoning=true but no quirks.thinking block -- a real gap this
        # overlay entry has, not "no thinking": still honest unknown.
        return unknown()
    raw_mechanism = thinking_quirks.get("mechanism")
    mechanism = _MECHANISM_MAP.get(str(raw_mechanism), "none")
    effort_by_level = thinking_quirks.get("effortByLevel")
    budget_by_level = thinking_quirks.get("budgetByLevel")
    levels: tuple[str, ...] = ()
    budget_range: tuple[int, int] | None = None
    if isinstance(effort_by_level, dict) and effort_by_level:
        levels = tuple(str(k) for k in effort_by_level)
    elif isinstance(budget_by_level, dict) and budget_by_level:
        levels = tuple(str(k) for k in budget_by_level)
        values = [v for v in budget_by_level.values() if isinstance(v, int)]
        if values:
            budget_range = (min(values), max(values))
    template_kwarg = _first_template_kwarg(
        thinking_quirks.get("chatTemplateKwargs"),
        entry.quirks.get("llamaCpp", {}).get("chatTemplateKwargs")
        if isinstance(entry.quirks.get("llamaCpp"), dict)
        else None,
    )
    spec = ThinkingSpec(
        mechanism=mechanism,  # type: ignore[arg-type]
        levels=levels,
        effort_by_level={str(k): str(v) for k, v in effort_by_level.items()}
        if isinstance(effort_by_level, dict)
        else {},
        budget_range=budget_range,
        template_kwarg=template_kwarg,
    )
    return Fact(value=spec, source="overlay", observed_at=observed_at, detail=detail)


def _sampling_fact(entry: OverlayEntry, mode: str, *, observed_at: str, detail: str) -> Fact:
    sampling = entry.quirks.get("sampling")
    profile = sampling.get(mode) if isinstance(sampling, dict) else None
    if not isinstance(profile, dict) or not profile:
        return unknown()
    return Fact(value=dict(profile), source="overlay", observed_at=observed_at, detail=detail)


def _observed_at(entry: OverlayEntry) -> str:
    measured = entry.quirks.get("measuredUnder")
    if isinstance(measured, dict):
        date = measured.get("date")
        if isinstance(date, str) and date.strip():
            return date.strip()
    return _now_iso()


def _detail_for(entry: OverlayEntry, pattern: str) -> str:
    detail = f"family={entry.family} matchPattern={pattern!r} root={entry.root}"
    measured = entry.quirks.get("measuredUnder")
    if isinstance(measured, dict) and measured:
        detail += f" measuredUnder={measured!r}"
    return detail


def entry_to_model_capabilities(
    model_key: str, entry: OverlayEntry, pattern: str
) -> ModelCapabilities:
    """Map one matched :class:`OverlayEntry` onto a :class:`ModelCapabilities`.

    Every populated field carries ``source="overlay"`` and a ``detail`` naming
    the family, the matched pattern, the loaded root, and (when the entry
    carries one) ``quirks.measuredUnder`` as provenance -- never deployment
    data (brief Part 8.4 last line). A capability the entry's own schema does
    not carry (``parallel_tool_calls``, ``forbidden_params`` -- clio-coder's
    format has no equivalent) stays an honest ``unknown()``, never a guess.
    """

    observed_at = _observed_at(entry)
    detail = _detail_for(entry, pattern)
    capabilities = entry.capabilities
    return ModelCapabilities(
        model_key=model_key,
        task=_task_fact(capabilities, observed_at=observed_at, detail=detail),
        context_max=_int_fact(
            capabilities, "contextWindow", observed_at=observed_at, detail=detail
        ),
        output_max=_int_fact(capabilities, "maxTokens", observed_at=observed_at, detail=detail),
        input_modalities=_modalities_fact(capabilities, observed_at=observed_at, detail=detail),
        domains=_domains_fact(capabilities, observed_at=observed_at, detail=detail),
        tools=_bool_fact(capabilities, "tools", observed_at=observed_at, detail=detail),
        parallel_tool_calls=unknown(),
        structured_output=_structured_output_fact(
            capabilities, observed_at=observed_at, detail=detail
        ),
        thinking=_thinking_fact(entry, observed_at=observed_at, detail=detail),
        forbidden_params=unknown(),
        sampling_thinking=_sampling_fact(entry, "thinking", observed_at=observed_at, detail=detail),
        sampling_instruct=_sampling_fact(entry, "instruct", observed_at=observed_at, detail=detail),
    )


class CatalogOverlaySource:
    """The real :class:`~clio_agent.providers.capabilities.model_sources.OverlaySource`.

    ``facts(model_key)`` first looks ``model_key`` up as a family name (a
    deployment rule 3 already linked is keyed by it), then matches it against
    every loaded entry's ``matchPatterns`` (:func:`best_overlay_match`) the same
    way :func:`overlay_match_for_link` does.
    """

    def __init__(self, *, cwd: "str | Path | None" = None) -> None:
        self._cwd = cwd

    def facts(self, model_key: str) -> ModelCapabilities | None:
        try:
            entries = load_overlay_entries(cwd=self._cwd)
        except (
            ModelOverlayCatalogError
        ) as exc:  # pragma: no cover - defensive; see module docstring
            logger.warning("model_overlay: reason=overlay_load_failed error=%s", exc)
            return None
        match = _family_match(model_key, entries) or best_overlay_match(model_key, entries)
        if match is None:
            return None
        return entry_to_model_capabilities(model_key, match.entry, match.pattern)


def _family_match(model_key: str, entries: list[OverlayEntry]) -> OverlayMatch | None:
    """The entry whose ``family`` IS ``model_key`` (a deployment rule 3 already linked).

    A linked deployment is keyed by the family name, which need not contain any
    of its own ``matchPatterns`` (``gemma-3`` vs ``gemma-3-27b-it``), so the
    family is looked up by name before any pattern match. Several roots may
    carry the same family; the highest-ranked root wins, as for a pattern tie.
    """
    best: OverlayMatch | None = None
    best_rank = -1
    for entry in entries:
        if entry.family != model_key:
            continue
        rank = _ROOT_RANK.get(entry.root, 0)
        if rank > best_rank:
            best, best_rank = OverlayMatch(entry=entry, pattern=entry.family), rank
    return best


_DEFAULT_SOURCE = CatalogOverlaySource()


def default_overlay_source() -> CatalogOverlaySource:
    """The process-wide default overlay source (brief 5.1 layer 2's default)."""
    return _DEFAULT_SOURCE


__all__ = [
    "CatalogOverlaySource",
    "OverlayEntry",
    "OverlayMatch",
    "best_overlay_match",
    "default_overlay_source",
    "entry_to_model_capabilities",
    "load_overlay_entries",
    "overlay_match_for_link",
    "project_overlay_dir",
    "user_overlay_dir",
]
