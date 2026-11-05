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
    revisions: list[int],
    message: Mapping[str, Any],
    new_revision: int,
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

    Returns:
        `(new_messages, new_revisions)`, parallel and the same length:
        every non-`updateComponents` message from `messages` (with its
        ORIGINAL stamp, unchanged), in its original relative order, plus
        exactly one `updateComponents` message (stamped `new_revision`)
        positioned right after `createSurface`. An existing component id
        keeps its ORIGINAL position in the merged list (only its
        definition is replaced); a component id `message` introduces for
        the first time is appended at the end.
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


#: A part's metadata key: the lowest revision its surfaces may have after it is folded.
REVISION_FLOOR_KEY = "a2ui_revision_floor"


def is_whole_model_update(message: Mapping[str, Any]) -> bool:
    """True for an ``updateDataModel`` that replaces the whole model (path ``/``)."""
    update = message.get("updateDataModel")
    return isinstance(update, Mapping) and str(update.get("path") or "/") == "/"


def drop_superseded_data_model(
    messages: list[dict[str, Any]], revisions: list[int], message: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[int]]:
    """Before a whole-model update is appended, the data-model slots it replaces go.

    A model set at ``/`` makes every earlier ``updateDataModel`` dead, so a surface fed
    by a background script keeps its structure and one current model instead of
    filling its message slots (taskable run_code CT10). The surface's revision is a
    separate counter and still rises.
    """
    if not is_whole_model_update(message):
        return messages, revisions
    kept = [i for i, row in enumerate(messages) if is_structural_message(row)]
    return [messages[i] for i in kept], [revisions[i] for i in kept]


def apply_revision_floor(
    surfaces: Mapping[Any, Any], keys: Any, metadata: Mapping[str, Any]
) -> None:
    """Raise the surfaces a part touched to the part's revision floor (a checkpoint of a
    live feed records the revision viewers had already seen, so a later replay of the
    shorter checkpoint never moves a surface backwards)."""
    floor = metadata.get(REVISION_FLOOR_KEY)
    if not isinstance(floor, int):
        return
    for key in keys:
        record = surfaces.get(key)
        if record is not None and record.revision < floor:
            record.revision = floor
