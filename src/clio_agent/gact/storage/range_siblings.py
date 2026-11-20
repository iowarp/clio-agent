"""Files a range-read file names beside itself: a VRT mosaic's tiles, a sidecar it points to.

A GDAL VRT names its tiles by paths relative to itself (``TAHOE_be/he_38119G83.img``
beside ``TAHOE_be.vrt``). Read through a range handle, GDAL asks the handle's URL for
``…/{handle}/TAHOE_be/he_38119G83.img``. The route served only the handle's own file, so
every tile was 404 and a live Tahoe run reported the basin's 2010 lidar unreadable, though
each tile is a resource of the same NDP dataset.

Here the relative path is resolved against the handle file's upstream URL, the way GDAL
resolves it against the original location, and served when a linked NDP source of the
workspace holds a resource at exactly that URL: through that resource's own handle, with
the same sign-in, revision and counting as any range read. A tile that is not linked stays
404, so nothing is fetched that the person has not connected. ``vrt_tiles`` lists what a
VRT names, so the agent can see which tiles to connect.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin

if TYPE_CHECKING:
    from clio_agent.gact.storage.models import SourceRecord
    from clio_agent.gact.storage.range_handles import RangeHandle, RangeHandles
    from clio_agent.gact.storage.service import StorageService

__all__ = ["origin_url", "resolve_sibling", "vrt_note", "vrt_tiles"]

#: Traced when a VRT opened by range could not be read to list its tiles.
VRT_UNREAD = "range_vrt_unread"
#: How much of a VRT is read to list its tiles (a basin mosaic is tens of KB).
VRT_READ_BYTES = 4 * 2**20

_SOURCE = re.compile(r"<SourceFilename([^>]*)>([^<]+)</SourceFilename>")


def _resources(service: StorageService, record: SourceRecord) -> list:
    """The resources of a linked NDP source (each with its private upstream URL)."""
    from clio_agent.gact.storage.ndp_catalog import NDPSelection  # noqa: PLC0415

    selection_id = record.configuration.ndp_selection_id
    if not selection_id:
        return []
    try:
        selection = service.store.get("ndp-selection", selection_id, NDPSelection)
    except (KeyError, FileNotFoundError, ValueError):
        return []
    return list(selection.dataset.resources)


def origin_url(service: StorageService, record: SourceRecord, path: str) -> str:
    """The upstream URL of ``path`` in a linked NDP source; ``""`` for other providers."""
    if record.source.provider != "ndp":
        return ""
    return next((r.url for r in _resources(service, record) if r.path == path), "")


def resolve_sibling(
    service: StorageService, handles: RangeHandles, parent: RangeHandle, relative: str
) -> RangeHandle | None:
    """The handle of the linked file ``relative`` names beside ``parent``'s file, or None."""
    from clio_agent.gact.storage.range_handles import mint  # noqa: PLC0415

    if not parent.origin or not relative or relative.startswith("/"):
        return None
    cached = handles.child(parent.id, relative)
    if cached is not None:
        return cached
    target = urljoin(parent.origin, relative)
    if not target.startswith(parent.origin.rsplit("/", 1)[0] + "/"):
        return None  # only beside or below the file, as a VRT's relative paths are
    for record in service.sources(parent.workspace_id):
        if record.source.provider != "ndp" or not record.connected:
            continue
        match = next((r for r in _resources(service, record) if r.url == target), None)
        if match is not None:
            child = mint(service, record, match.path, session_id=parent.session_id, handles=handles)
            return handles.adopt_child(parent.id, relative, child)
    return None


def vrt_tiles(text: str) -> list[str]:
    """The relative source paths a VRT names, in order, without repeats."""
    seen: dict[str, None] = {}
    for attributes, name in _SOURCE.findall(text):
        if 'relativeToVRT="1"' in attributes.replace("'", '"'):
            seen.setdefault(unquote(name.strip()), None)
    return list(seen)


def _linked_urls(service: StorageService, workspace_id: str) -> set[str]:
    return {
        r.url
        for record in service.sources(workspace_id)
        if record.source.provider == "ndp" and record.connected
        for r in _resources(service, record)
    }


def vrt_note(service: StorageService, handle: RangeHandle) -> dict | None:
    """For a VRT opened by range: how many tiles it names, how many are linked here, and
    how reads reach them; None when ``handle`` is not an NDP VRT or cannot be read."""
    if not handle.origin or not handle.path.lower().endswith(".vrt"):
        return None
    try:
        text = handle.read(0, min(handle.size or VRT_READ_BYTES, VRT_READ_BYTES))
    except (ValueError, PermissionError, OSError) as exc:
        from clio_agent.runtime import trace  # noqa: PLC0415

        trace.event("STORAGE", "%s path=%s error=%s", VRT_UNREAD, handle.path, exc)
        return None
    tiles = vrt_tiles(text.decode("utf-8", errors="replace"))
    if not tiles:
        return None
    linked = _linked_urls(service, handle.workspace_id)
    count = sum(1 for tile in tiles if urljoin(handle.origin, tile) in linked)
    return {
        "tiles": len(tiles),
        "linked": count,
        "note": (
            f"This VRT mosaics {len(tiles)} tile files; {count} are linked in this workspace. "
            "Reads through handle_url reach the linked ones, so a window needs the tiles under "
            "it linked: connected_data_ndp_tiles_covering names them for a region (pass "
            "covering[].id as resource_ids to connected_data_connect)."
        ),
    }
