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

Adversarial review (coordinator design, 2026-10-01): a probe against both
PR heads together found the revision+fingerprint client design (gact-tui
#513) still cannot tell "this merged slot changed" from "this merged slot
is unchanged" without re-hashing its full content on every reconcile --
exactly the O(surface size) re-stringify cost the fingerprint cache was
built to avoid. The fix is a server-side revision STAMP: `messages` grows a
parallel `revisions` list (`A2UISurfaceRecord.message_revisions`), one
integer per message slot, naming the revision that produced the slot's
CURRENT content. The merged `updateComponents` slot's stamp is the revision
of its most recent change, however small; every other message's stamp is
the revision it was appended at and never changes again. A client then
knows exactly which slots are new since its own `appliedRevision` by
comparing integers, never bytes.

F5 (component cap): `materialize_update_components` also enforces
`MAX_A2UI_COMPONENTS` on the MERGED state, not just on one incoming
message's own component list (`validate_components`'s existing per-message
check) -- a surface that accumulates distinct component ids across many
small updates, never any single oversized one, was otherwise unbounded.
"""

from __future__ import annotations

from typing import Any, Mapping

from clio_agent.gact.a2ui_catalogs.validation import A2UIValidationError


class A2UIComponentLimitExceededError(A2UIValidationError):
    """Raised when materializing a surface's components would exceed the cap.

    Carries `component_count`/`limit` so a caller can attach the typed
    `a2ui_component_limit_exceeded` reason, mirroring
    `A2UICatalogUnknownError`'s `catalog_id` attribute.
    """

    def __init__(self, component_count: int, limit: int) -> None:
        self.component_count = component_count
        self.limit = limit
        super().__init__(
            f"A2UI surface's merged component state would carry {component_count} "
            f"components, exceeding the {limit} limit"
        )


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
    revisions: list[int],
    message: Mapping[str, Any],
    new_revision: int,
    *,
    max_components: int,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Fold `message` (a validated `updateComponents`) into the ONE current
    `updateComponents` message `messages` carries, latest-id-wins, and stamp
    it with `new_revision` (the revision this apply call produces).

    Args:
        messages: The surface's message history BEFORE this message (never
            mutated).
        revisions: `messages`' parallel per-slot revision stamps (same
            length as `messages`; never mutated).
        message: The new, already-validated `updateComponents` envelope
            (`{"version": ..., "updateComponents": {"surfaceId", "components"}}`).
        new_revision: The revision number this message's apply produces --
            stamped on the merged slot, since ITS CONTENT just changed,
            however small the change.
        max_components: F5 -- the merged component count this surface may
            never exceed, cumulative across its whole lifetime (not just
            this one message's own component list, which
            `validate_components` already bounds separately).

    Returns:
        `(new_messages, new_revisions)`, parallel and the same length:
        every non-`updateComponents` message from `messages` (with its
        ORIGINAL stamp, unchanged), in its original relative order, plus
        exactly one `updateComponents` message (stamped `new_revision`)
        positioned right after `createSurface`. An existing component id
        keeps its ORIGINAL position in the merged list (only its
        definition is replaced); a component id `message` introduces for
        the first time is appended at the end.

    Raises:
        A2UIComponentLimitExceededError: If the merged component count
            would exceed `max_components`.
    """

    merged_components: list[dict[str, Any]] = []
    other_messages: list[dict[str, Any]] = []
    other_revisions: list[int] = []
    for existing, existing_revision in zip(messages, revisions, strict=True):
        payload = existing.get("updateComponents")
        if not isinstance(payload, Mapping):
            other_messages.append(existing)
            other_revisions.append(existing_revision)
            continue
        merged_components = upsert_components_by_id(
            merged_components, list(payload.get("components") or [])
        )

    new_payload = message["updateComponents"]
    merged_components = upsert_components_by_id(
        merged_components, list(new_payload.get("components") or [])
    )
    if len(merged_components) > max_components:
        raise A2UIComponentLimitExceededError(len(merged_components), max_components)

    merged_message: dict[str, Any] = {
        "version": message.get("version"),
        "updateComponents": {
            "surfaceId": new_payload.get("surfaceId"),
            "components": merged_components,
        },
    }

    create_messages = [existing for existing in other_messages if "createSurface" in existing]
    create_revisions = [
        revision
        for existing, revision in zip(other_messages, other_revisions, strict=True)
        if "createSurface" in existing
    ]
    rest_messages = [existing for existing in other_messages if "createSurface" not in existing]
    rest_revisions = [
        revision
        for existing, revision in zip(other_messages, other_revisions, strict=True)
        if "createSurface" not in existing
    ]
    new_messages = [*create_messages, merged_message, *rest_messages]
    new_revisions = [*create_revisions, new_revision, *rest_revisions]
    return new_messages, new_revisions
