"""``plugin-asset://<plugin>/<path>``: a file a plugin bundles, shown on a surface.

A plugin can carry images and other media under its ``assets/`` folder (the guide
plugin bundles the docs' captures there). A surface names one as
``plugin-asset://<plugin>/<path>``; a viewer cannot read the plugin's folder, so it
resolves the reference through the service like ``artifact:`` and ``resource:``
(:mod:`clio_agent.gact.artifacts.references`) and reads the bytes from
``GET /v1/sessions/{sid}/plugin-assets/{plugin}/{path}``.

Only a plugin the session's agent resolves is served, and only a file inside that
plugin's ``assets/`` folder. Every failure is a typed
:class:`~clio_agent.gact.artifacts.references.ReferenceResolutionError`: the
reference is malformed (``reference_uri_invalid``), the plugin is not part of this
conversation (``plugin_asset_plugin_unresolved``), or the file is not there
(``reference_not_found``).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from clio_agent.gact.artifacts.references import ReferenceResolutionError

SCHEME = "plugin-asset"
#: The folder of a plugin its assets live in.
ASSETS_DIR = "assets"
#: WebP is not in every platform's mimetypes table.
_MEDIA_TYPES = {".webp": "image/webp", ".svg": "image/svg+xml", ".md": "text/markdown"}


def is_plugin_asset(uri: str) -> bool:
    """Whether ``uri`` uses the ``plugin-asset:`` scheme."""

    return uri.strip().lower().startswith(f"{SCHEME}:")


def parse(uri: str) -> tuple[str, str]:
    """``(plugin, path)`` from ``plugin-asset://<plugin>/<path>``; raises when malformed."""

    parts = urlsplit(uri.strip())
    plugin = parts.netloc
    path = unquote(parts.path.lstrip("/"))
    relative = PurePosixPath(path)
    if (
        parts.scheme.lower() != SCHEME
        or not plugin
        or not path
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in path
        or parts.query
        or parts.fragment
    ):
        raise ReferenceResolutionError(
            422,
            "reference_uri_invalid",
            f"{uri!r} is not plugin-asset://<plugin>/<path inside its assets folder>",
            uri=uri,
        )
    return plugin, relative.as_posix()


def _plugin_root(app: Any, session_id: str, plugin: str) -> Path:
    from clio_agent.gact.agents.resolution import session_resolution  # noqa: PLC0415

    resolution = session_resolution(app, session_id)
    row = next((row for row in resolution.plugins if row.get("id") == plugin), None)
    if row is None or not row.get("root"):
        raise ReferenceResolutionError(
            404,
            "plugin_asset_plugin_unresolved",
            f"the {plugin!r} plugin is not part of this conversation's agent",
            plugin=plugin,
        )
    return Path(str(row["root"]))


def asset_file(app: Any, session_id: str, plugin: str, path: str) -> Path:
    """The file ``path`` under ``plugin``'s ``assets/`` folder, for ``session_id``."""

    _, relative = parse(f"{SCHEME}://{plugin}/{path}")
    folder = (_plugin_root(app, session_id, plugin) / ASSETS_DIR).resolve()
    target = (folder / relative).resolve()
    if not target.is_relative_to(folder) or not target.is_file():
        raise ReferenceResolutionError(
            404,
            "reference_not_found",
            f"the {plugin!r} plugin has no asset {relative!r}",
            plugin=plugin,
            path=relative,
        )
    return target


def media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower()) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )


def resolve(app: Any, session_id: str, uri: str) -> dict[str, Any]:
    """The resolution a viewer reads: metadata and the service-relative ``fetch_path``."""

    plugin, relative = parse(uri)
    target = asset_file(app, session_id, plugin, relative)
    return {
        "uri": uri,
        "kind": "plugin_asset",
        "workspace_id": "",
        "name": target.name,
        "media_type": media_type(target),
        "size_bytes": target.stat().st_size,
        "plugin": plugin,
        "fetch_path": (
            f"/v1/sessions/{quote(session_id, safe='')}/plugin-assets/"
            f"{quote(plugin, safe='')}/{quote(relative)}"
        ),
    }


__all__ = ["SCHEME", "asset_file", "is_plugin_asset", "media_type", "parse", "resolve"]
