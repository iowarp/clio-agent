"""Plugin tab surface revisions that keep rising across re-reads (host semantics HS3).

A tab's surfaces are folded afresh from each ``resources/read``. A client renders a
surface by its revision, so a surface whose content changed must take a newer revision
than the one it already holds, and an unchanged one must keep it.
"""

from __future__ import annotations

from dataclasses import replace

from clio_agent.gact.a2ui import A2UISurfaceRecord

Surfaces = dict[tuple[str, str], A2UISurfaceRecord]


def carry_revisions(before: Surfaces, after: Surfaces) -> Surfaces:
    """Keep each surface's revision increasing across re-reads of the tab.

    A re-read folds the resource from scratch, so a fresh record's revision is only its
    message count. A surface the tab already held keeps its record when the content is
    the same, and otherwise takes the next revision; a slot whose message is unchanged
    keeps its stamp, the same way :func:`clio_agent.gact.a2ui.apply_batch` stamps.
    """

    carried: Surfaces = {}
    for key, record in after.items():
        old = before.get(key)
        if old is None:
            carried[key] = record
            continue
        if (old.messages, old.state, old.catalog_id, old.owner, old.error) == (
            record.messages,
            record.state,
            record.catalog_id,
            record.owner,
            record.error,
        ):
            carried[key] = old
            continue
        revision = old.revision + 1
        stamps = [
            old.message_revisions[index]
            if index < len(old.messages)
            and index < len(old.message_revisions)
            and old.messages[index] == message
            else revision
            for index, message in enumerate(record.messages)
        ]
        created = old.created_at if old.state != "deleted" else record.created_at
        carried[key] = replace(
            record, revision=revision, message_revisions=stamps, created_at=created
        )
    return carried


__all__ = ["Surfaces", "carry_revisions"]
