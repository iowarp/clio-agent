"""Materialize a surface's CURRENT component definitions, one message.

Owner module for the `updateComponents` half of `gact/a2ui.py`'s message
fold (`_apply_staged_message`) -- split out so the fix does not grow that
file (`scripts/check_file_size.py`'s ratchet).

G2 merge-gate finding (iowarp/gact-tui#513 comment 5937313752, reproduced
twice against a real server): the OLD compaction rule dropped an earlier
`updateComponents` only when its component-id set was a SUBSET of a later
one's -- lossless for the common case of literally resending the exact same
ids, but wrong whenever a later message fixes FEWER ids than an earlier one
touched. A `GET /v1/sessions/{sid}/a2ui/surfaces` against the real server
for this bug showed all three messages (`createSurface`, a bad
`updateComponents` defining `root`, and a fix re-`updateComponents`-ing
ONLY `root`) present together forever: `{root}` is not a subset of
`{root}`... except the real failure was the whole-dashboard case, `{root,
a, b}` is NOT a subset of `{root}`, so the bad message was never dropped,
and the client's `applyPendingMessages` (gact-tui's own fix,
`processor-store.ts`) kept re-throwing on it every single reconcile,
forever, with the fix's `updateComponents` right there in the stream,
unreachable.

The fix: stop trying to drop whole PAST MESSAGES by a subset check, and
instead fold every `updateComponents` message, past and present, into ONE
current definition per component id -- latest write wins, same semantics
`@a2ui/web_core`'s own `processUpdateComponentsMessage` already applies to
a live model. The stored stream becomes `[createSurface, one merged
updateComponents, ...the updateDataModel messages, in their original
order]`: a later fix for one component can never again be stranded next to
the bad definition it replaces, because there is no "next to" -- there is
only the current merged message.

:func:`upsert_components_by_id` is the ONE shared primitive behind both this
module's fold and `a2ui_producer/_common.py`'s `current_surface_components`/
`merged_surface_components` (the surface-definition-artifact mint path,
#1533 S4), which previously carried its own private duplicate of the exact
same algorithm. It lives here, not there, because `a2ui.py` cannot depend on
`a2ui_producer` (which already depends on `a2ui.py`) -- `_common.py` now
imports it from here instead.
"""

from __future__ import annotations

from typing import Any, Mapping


def upsert_components_by_id(
    previous: list[dict[str, Any]], upserted: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Upsert `upserted` into `previous` by `id`: an existing id is REPLACED
    in place (its original position kept); a new id is appended. Mirrors the
    `updateComponents` wire message's own upsert contract -- there is no
    per-component delete, only a whole-surface one.
    """

    by_id: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    for component in [*previous, *upserted]:
        if not isinstance(component, dict):
            continue
        component_id = component.get("id")
        if component_id not in by_id:
            order.append(component_id)
        by_id[component_id] = component
    return [by_id[component_id] for component_id in order]


def is_structural_message(message: Mapping[str, Any]) -> bool:
    """True for a message `max_a2ui_messages` eviction must never drop.

    `createSurface` (always first, required for the surface to exist at
    all) and the ONE merged `updateComponents` (the surface's entire
    current component tree, post-materialization -- evicting it would blank
    the rendered surface, not just trim history) are both structural;
    only `updateDataModel` messages are genuinely re-derivable scratch.
    """

    return "createSurface" in message or "updateComponents" in message


def materialize_update_components(
    messages: list[dict[str, Any]],
    message: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Fold `message` (a validated `updateComponents`) into the ONE current
    `updateComponents` message `messages` carries, latest-id-wins.

    Args:
        messages: The surface's message history BEFORE this message (never
            mutated).
        message: The new, already-validated `updateComponents` envelope
            (`{"version": ..., "updateComponents": {"surfaceId", "components"}}`).

    Returns:
        A new message list: every non-`updateComponents` message from
        `messages`, in its original relative order, plus exactly one
        `updateComponents` message positioned right after `createSurface`.
        An existing component id keeps its ORIGINAL position in the merged
        list (only its definition is replaced); a component id `message`
        introduces for the first time is appended at the end.
    """

    merged_components: list[dict[str, Any]] = []
    other_messages: list[dict[str, Any]] = []
    for existing in messages:
        payload = existing.get("updateComponents")
        if not isinstance(payload, Mapping):
            other_messages.append(existing)
            continue
        merged_components = upsert_components_by_id(
            merged_components, list(payload.get("components") or [])
        )

    new_payload = message["updateComponents"]
    merged_components = upsert_components_by_id(
        merged_components, list(new_payload.get("components") or [])
    )

    merged_message: dict[str, Any] = {
        "version": message.get("version"),
        "updateComponents": {
            "surfaceId": new_payload.get("surfaceId"),
            "components": merged_components,
        },
    }

    create_messages = [existing for existing in other_messages if "createSurface" in existing]
    rest_messages = [existing for existing in other_messages if "createSurface" not in existing]
    return [*create_messages, merged_message, *rest_messages]
