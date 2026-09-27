"""``download_media``: save an external image, video or audio file into the workspace.

Agent story: a user asks to *see* something that lives on the web -- a photo,
a figure from a paper's site, a short clip. A2UI viewers never auto-load an
external URL (a model-authored URL would make the viewer's machine contact
an arbitrary host), so an ``Image`` whose ``url`` is ``https://...`` renders
as a link naming the host, not as the picture. A workspace path, on the other
hand, is exported as an artifact when the surface is produced and renders for
every viewer, local or remote. So the agent calls ``download_media`` with the
URL, gets back a workspace-relative ``path``, and references that path in its
surface.

The tool is effectful (it contacts a network host and writes a file), so it
passes through the session's ONE permission gate like any other effectful
call, keeps its writes inside the active workspace, runs the destination
through the file policy (``CLIO_ALLOWED_ROOTS`` / ``tools.file_policy``), and
takes its size and media-type bounds from config
(:class:`~clio_agent.tools.media_download.MediaDownloadLimits`). Every
refusal is a typed result ``{ok: False, reason, detail, hint}``; nothing is
raised at the model, and a failed download leaves no partial file behind.
"""

from __future__ import annotations

import dataclasses
import mimetypes
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from clio_agent.gact.a2ui_catalogs.media_sources import MEDIA_DOWNLOAD_TOOL_NAME
from clio_agent.tools.execution import get_active_tool_workspace_root
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError
from clio_agent.tools.media_download import (
    MediaDownloadError,
    MediaDownloadLimits,
    Resolver,
    default_resolve,
    fetch_media,
    validate_media_url,
)

#: The tool's name, as agents declare it and as catalog guidance names it.
MEDIA_DOWNLOAD_TOOL = MEDIA_DOWNLOAD_TOOL_NAME
#: Workspace-relative directory a download lands in when no ``path`` is given.
DEFAULT_DOWNLOAD_DIR = "downloads"

#: Seams the in-memory tests replace (a mock transport, a fake resolver).
_TRANSPORT_FACTORY: Callable[[], Any] = lambda: None  # noqa: E731
_RESOLVE: Resolver | None = None

#: Extensions ``mimetypes`` picks oddly on some platforms (``.jpe``, ``.mp2``).
_PREFERRED_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/webm": ".weba",
}
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _refuse(reason: str, detail: str, hint: str, **details: Any) -> dict[str, Any]:
    return MediaDownloadError(reason, detail, hint=hint, details=details).to_result()


def _workspace_root() -> Path:
    raw = get_active_tool_workspace_root().strip()
    if not raw:
        raise MediaDownloadError(
            "media_download_workspace_unavailable",
            "no workspace is bound to this call, so there is nowhere to save the file",
            hint="this tool only works inside a session with a workspace",
        )
    try:
        return Path(raw).resolve(strict=True)
    except FileNotFoundError as exc:
        raise MediaDownloadError(
            "media_download_workspace_unavailable",
            f"the active workspace does not exist: {raw}",
            hint="this tool only works inside a session with an existing workspace",
        ) from exc


def _contained(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MediaDownloadError(
            "media_download_outside_workspace",
            f"{raw!r} is outside this session's workspace ({root})",
            hint="pass a workspace-relative path, or omit path to save under downloads/",
        ) from exc
    return resolved


def _policy_checked(policy: FileAccessPolicy, target: Path) -> Path:
    try:
        return policy.validate_write(str(target), field="path", create_parent=True)
    except FilePolicyError as exc:
        raise MediaDownloadError(
            "media_download_file_policy",
            exc.message,
            hint=exc.next_action,
            details={"policy_code": exc.code},
        ) from exc


def _gate(url: str, path: str) -> None:
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.tools.execution import _invoke_permission_gate  # noqa: PLC0415

    app = context.active_app()
    state = getattr(app, "state", None)
    gate = getattr(state, "pending_permission_gate", None)
    if gate is None and state is not None and hasattr(state, "make_permission_gate"):
        gate = state.make_permission_gate()
    if gate is None:
        raise MediaDownloadError(
            "media_download_session_unavailable",
            "this call ran outside a CLIO session, so no permission gate can approve it",
            hint="it cannot succeed standalone; do not retry it here",
        )
    decision = _invoke_permission_gate(gate, MEDIA_DOWNLOAD_TOOL, {"url": url, "path": path}, None)
    if decision != "allow":
        message = str(getattr(decision, "deny_message", "") or "")
        raise MediaDownloadError(
            "media_download_permission_denied",
            message or f"downloading {url} was denied by the permission gate",
            hint="the user or a policy declined this download; do not retry it unchanged",
        )


def _derived_name(url: str, media_type: str) -> str:
    segment = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    safe = _UNSAFE_NAME_CHARS.sub("-", segment).strip(".-") or "media"
    guessed, _ = mimetypes.guess_type(safe)
    if guessed == media_type:
        return safe
    extension = _PREFERRED_EXTENSIONS.get(media_type) or mimetypes.guess_extension(media_type)
    stem = safe.rsplit(".", 1)[0] if "." in safe else safe
    return f"{stem}{extension or ''}"


def _free_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    counter = 1
    while candidate.exists():
        candidate = directory / (f"{stem}-{counter}.{suffix}" if dot else f"{stem}-{counter}")
        counter += 1
    return candidate


def _write_atomically(target: Path, body: bytes) -> None:
    partial = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        partial.write_bytes(body)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def download_media(url: str, path: str = "") -> dict[str, Any]:
    """Download one external media file into the workspace (see the module docstring)."""

    try:
        root = _workspace_root()
        limits = MediaDownloadLimits.from_config()
        validate_media_url(url, limits, resolve=_RESOLVE or default_resolve)
        explicit = _contained(root, path) if path.strip() else None
        if explicit is not None and explicit.exists():
            raise MediaDownloadError(
                "media_download_destination_exists",
                f"{explicit.relative_to(root).as_posix()} already exists",
                hint="pass a new path, or omit path to save under downloads/ with a free name",
            )
        _gate(url, path)
        policy = FileAccessPolicy.from_env()
        limits = dataclasses.replace(
            limits, max_bytes=min(limits.max_bytes, policy.max_file_size_bytes)
        )
        # The destination (or, for a derived name, its directory) clears the
        # file policy BEFORE any byte is fetched; a derived name is chosen once
        # the media type is known and re-checked.
        checked = _policy_checked(policy, explicit or root / DEFAULT_DOWNLOAD_DIR / ".probe")
        fetched = fetch_media(
            url, limits, transport=_TRANSPORT_FACTORY(), resolve=_RESOLVE or default_resolve
        )
        if explicit is not None:
            target = checked
        else:
            name = _derived_name(fetched.final_url, fetched.media_type)
            target = _policy_checked(policy, _free_path(checked.parent, name))
        _write_atomically(target, fetched.body)
    except MediaDownloadError as exc:
        return exc.to_result()
    except OSError as exc:
        return _refuse(
            "media_download_write_failed",
            f"the downloaded file could not be written: {exc}",
            "check that the workspace is writable, then retry",
        )
    relative = target.relative_to(root).as_posix()
    return {
        "ok": True,
        "path": relative,
        "media_type": fetched.media_type,
        "size_bytes": len(fetched.body),
        "sha256": fetched.sha256,
        "source_url": url,
        "final_url": fetched.final_url,
        "host": fetched.host,
        "hint": (
            f"reference {relative!r} (for example as an Image/Video/AudioPlayer url in a "
            "surface); CLIO exports a workspace path as an artifact, so every viewer, "
            "local or remote, can load it"
        ),
    }


def build_download_media_tool() -> Any:
    """Build the native tool that saves one external media file into the workspace."""

    from clio_agent.gact.agents.native_presenters_workspace_file import (  # noqa: PLC0415
        workspace_file_presentation,
    )
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def download_media_tool(url: str, path: str = "") -> dict[str, Any]:
        """Save an external image, video or audio file into the workspace.

        Viewers never load external URLs in a surface (they show a link to the
        host instead), so to SHOW web media, download it here first and
        reference the returned workspace ``path``; CLIO exports a workspace path
        as an artifact that renders for every viewer. Only media types are
        saved, within the configured size limit, and only from public hosts.
        """

        return download_media(url, path)

    return native_tool(
        download_media_tool,
        name=MEDIA_DOWNLOAD_TOOL,
        presentation=workspace_file_presentation,
        domain="workspace",
        desc=download_media_tool.__doc__,
        title="Download media",
        args={
            "url": {
                "type": "string",
                "description": "The http(s) URL of the media file itself (not a page about it).",
            },
            "path": {
                "type": "string",
                "description": (
                    "Optional workspace-relative destination; must not exist yet. Empty "
                    "saves under downloads/ with a name derived from the URL."
                ),
            },
        },
    )


__all__ = [
    "DEFAULT_DOWNLOAD_DIR",
    "MEDIA_DOWNLOAD_TOOL",
    "build_download_media_tool",
    "download_media",
]
