"""How CLIO viewers load a URL-valued component property -- the one wording.

A surface is drawn by a viewer that may run on another machine than this
service (``docs/gact/a2ui-binding.md``, *Content references*). That makes a
URL-valued property (``url`` / ``uri`` / ``dataUri``,
:data:`~clio_agent.gact.a2ui_catalogs.validation.A2UI_URL_KEYS`) one of:

* an external ``https:`` URL -- never loaded automatically; the component
  shows the host's name and a link;
* a workspace file path -- exported as an artifact when the surface is
  produced, so it renders for every viewer, local or remote;
* an ``artifact://`` / ``resource://`` reference -- resolved through the
  service, the same way.

This is a property of CLIO's runtime (the producer's export boundary and the
clients' loaders), not of any one catalog, so it is stated HERE and rendered
into every generated catalog skill that has such a property
(:mod:`clio_agent.gact.a2ui_catalogs.skills`) and into the producer tool
result that admits an external URL (:mod:`clio_agent.gact.a2ui_producer.
_export`). It is grounding -- what the state means -- so the model can pick
its own next step; nothing here decides for it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from clio_agent.gact.a2ui_catalogs.validation import A2UI_URL_KEYS

#: The native tool that saves an external media file into the workspace
#: (:mod:`clio_agent.gact.media_download_tool`); named here, not imported, so
#: this leaf module stays import-light.
MEDIA_DOWNLOAD_TOOL_NAME = "download_media"


def _collect(schema: Any, found: set[str]) -> None:
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            found.update(str(key) for key in properties if str(key).lower() in A2UI_URL_KEYS)
        for value in schema.values():
            _collect(value, found)
    elif isinstance(schema, list):
        for value in schema:
            _collect(value, found)


def url_properties(file: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return ``(component, property)`` for every URL-valued property a catalog declares."""

    components = file.get("components")
    if not isinstance(components, Mapping):
        return []
    pairs: list[tuple[str, str]] = []
    for name in sorted(components):
        found: set[str] = set()
        _collect(components[name], found)
        pairs.extend((str(name), prop) for prop in sorted(found))
    return pairs


def media_source_lines(file: Mapping[str, Any]) -> list[str]:
    """The ``## Media and file sources`` skill section, or ``[]`` when not applicable."""

    pairs = url_properties(file)
    if not pairs:
        return []
    listed = ", ".join(f"`{component}` (`{prop}`)" for component, prop in pairs)
    return [
        "",
        "## Media and file sources",
        (
            f"URL-valued properties in this catalog: {listed}. A surface is drawn by "
            "a viewer that may run on another machine than this service, so these "
            "values load as follows:"
        ),
        (
            "- An external `https:` URL is never loaded by viewers: the component "
            "shows the host's name and a link to open it, not the image, video or "
            "audio itself."
        ),
        (
            "- A path to a file inside this session's workspace is exported as an "
            "artifact when the surface is created or updated (the tool result lists "
            "it under `exported_artifacts`), so it renders for every viewer, local "
            "or remote."
        ),
        "- An `artifact://` reference a tool returned renders the same way.",
        (
            "To show media that lives on the web, first save it into the workspace "
            f"(`{MEDIA_DOWNLOAD_TOOL_NAME}` does this and returns the workspace "
            "path), then reference that path."
        ),
    ]


def external_url_notice(entries: Iterable[Mapping[str, Any]]) -> str:
    """The producer-result notice for external URLs a surface now carries."""

    rows = list(entries)
    hosts = sorted({str(row.get("host") or "") for row in rows} - {""})
    components = sorted({f"{row.get('component_id')}.{row.get('property')}" for row in rows})
    return (
        f"Viewers never load external URLs: {', '.join(components)} will show a link to "
        f"{', '.join(hosts) or 'the host'} instead of the media. To show the file itself, "
        f"save it into the workspace with {MEDIA_DOWNLOAD_TOOL_NAME} and reference the "
        "returned workspace path; CLIO exports a workspace path as an artifact that "
        "renders for every viewer, local or remote."
    )


def insecure_url_detail(raw: str) -> str:
    """The refusal detail for an ``http:`` media URL (never admitted by the validator)."""

    return (
        f"{raw!r} is an insecure http: URL, which a surface never admits and no viewer "
        f"loads. Save the file into this session's workspace with "
        f"{MEDIA_DOWNLOAD_TOOL_NAME} and reference the returned workspace path; CLIO "
        "exports it as an artifact that renders for every viewer."
    )


__all__ = [
    "MEDIA_DOWNLOAD_TOOL_NAME",
    "external_url_notice",
    "insecure_url_detail",
    "media_source_lines",
    "url_properties",
]
