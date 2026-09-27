"""Export boundary for A2UI producer tools: workspace file paths become artifacts.

A surface is rendered by a viewer that may run on another machine than this
service (a desktop or web client connected to a remote CLIO), so a URL-keyed
component property (``url`` / ``uri`` / ``dataUri``) that names a file on the
service's disk can never be fetched as-is. Rendering a workspace file in a UI
is *exporting* it, and CLIO mints artifact identity at the export boundary
(``docs/design/artifacts-research-2026-07.md``: registration, not authorship).

So before a producer tool validates its components, every URL-keyed literal
that is a filesystem path (relative, absolute, a Windows drive path, or a
``file:`` URL) and names an existing file inside the session's workspace is
registered through the SAME harness mint funnel the user-pin channel uses
(content-addressed: re-exporting unchanged bytes dedups onto the existing
version) and replaced by its ``artifact://<artifact-id>`` reference. Every
export is reported back in the tool result (``exported_artifacts``) so the
model sees exactly which path became which artifact -- nothing is rewritten
silently. A path that names no file in the workspace is a typed refusal that
says what a viewer can fetch; a value with any other scheme is left untouched
for the catalog validator to judge.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from clio_agent.gact.a2ui_catalogs.validation import A2UI_URL_KEYS, is_allowed_a2ui_url
from clio_agent.gact.a2ui_producer._refusal import refusal

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The producer designation stamped on artifacts minted at this boundary.
A2UI_EXPORT_DESIGNATION = "a2ui-export"


def _path_candidate(value: str) -> str | None:
    """Return the filesystem path ``value`` denotes, or ``None`` if it is a URL.

    A value with no scheme, a Windows drive path (``D:\\x`` parses as scheme
    ``d``) or a ``file:`` URL is a path; anything else carries a real scheme
    and is the validator's to judge.
    """

    stripped = value.strip()
    if not stripped:
        return None
    parts = urlsplit(stripped)
    scheme = parts.scheme.lower()
    if scheme == "file":
        return url2pathname(parts.path)
    if not scheme:
        return stripped
    if len(scheme) == 1 and stripped[1:3] in (":\\", ":/"):
        return stripped
    return None


class _Exporter:
    """Resolve and mint path literals for ONE producer call (memoized per path)."""

    def __init__(self, app: "FastAPI", session_id: str) -> None:
        from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
            _session_workspace_id,
            _workspace_root,
        )

        self.app = app
        self.session_id = session_id
        self.workspace_id = _session_workspace_id(app, session_id)
        self.root = _workspace_root(app, self.workspace_id)
        self.exported: list[dict[str, Any]] = []
        self._minted: dict[Path, dict[str, Any]] = {}

    def resolve(self, raw: str, path: str) -> Path | dict[str, Any]:
        """Return the contained existing file for ``path``, or a typed refusal."""

        from clio_agent.gact.artifacts.minting import _contained  # noqa: PLC0415

        if self.root is not None:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = self.root / candidate
            if _contained(candidate, self.root):
                resolved = candidate.resolve(strict=False)
                if resolved.is_file():
                    return resolved
            where = f"this session's workspace ({self.root})"
        else:
            where = "this session's workspace (none is bound, so nothing can be exported)"
        return refusal(
            "a2ui_url_unresolved",
            detail=(
                f"{raw!r} is neither a URL the viewer can fetch nor an existing file "
                f"inside {where}. A surface is rendered by a viewer that may run on "
                "another machine, so it cannot read paths on this service's disk: a "
                "file inside the workspace is exported as an artifact when referenced "
                "by its path, and anything else needs an https:, artifact: or "
                "resource: URL."
            ),
        )

    def mint(
        self, resolved: Path, *, raw: str, component_id: str, prop: str
    ) -> str | dict[str, Any]:
        """Mint (or dedup onto) the artifact for ``resolved``; return its reference."""

        from clio_agent.gact.artifacts.designation import kind_for_path  # noqa: PLC0415
        from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
            artifact_name_for_path,
            mint_artifact,
        )
        from clio_agent.gact.artifacts.model_identity import artifact_id_uri  # noqa: PLC0415
        from clio_agent.gact.artifacts.records import Mechanism  # noqa: PLC0415
        from clio_agent.gact.artifacts.storage import ingest_artifact_identity  # noqa: PLC0415

        cached = self._minted.get(resolved)
        if cached is None:
            try:
                ingested = ingest_artifact_identity(self.app, resolved, workspace_root=self.root)
                version = mint_artifact(
                    self.app,
                    self.session_id,
                    name=artifact_name_for_path(resolved),
                    workspace_id=self.workspace_id,
                    evidence=ingested.evidence,
                    kind=kind_for_path(resolved),
                    mechanism=Mechanism.HARNESS,
                    producer={
                        "designation": A2UI_EXPORT_DESIGNATION,
                        "session_id": self.session_id,
                    },
                    custody=ingested.custody,
                    path=str(resolved),
                    ingested=ingested,
                    not_ingested_size=ingested.not_ingested_size,
                )
            except (OSError, ValueError) as exc:
                return refusal(
                    "a2ui_url_export_failed",
                    detail=f"{raw!r} could not be registered as an artifact: {exc}",
                )
            if version is None:
                return refusal(
                    "a2ui_url_export_failed",
                    detail=f"{raw!r} could not be registered as an artifact",
                )
            cached = {
                "artifact_id": version.artifact_id,
                "uri": artifact_id_uri(version.artifact_id),
                "name": artifact_name_for_path(resolved),
                "version": version.version,
            }
            self._minted[resolved] = cached
        self.exported.append(
            {"component_id": component_id, "property": prop, "path": raw, **cached}
        )
        return str(cached["uri"])

    def walk(self, value: Any, component_id: str) -> dict[str, Any] | None:
        """Rewrite path literals in ``value`` in place; return a refusal to stop."""

        if isinstance(value, list):
            for item in value:
                stop = self.walk(item, component_id)
                if stop is not None:
                    return stop
            return None
        if not isinstance(value, dict):
            return None
        for key, item in value.items():
            if isinstance(item, str) and key.lower() in A2UI_URL_KEYS:
                if is_allowed_a2ui_url(item):
                    continue
                path = _path_candidate(item)
                if path is None:
                    continue
                resolved = self.resolve(item, path)
                if isinstance(resolved, dict):
                    return resolved
                reference = self.mint(resolved, raw=item, component_id=component_id, prop=key)
                if isinstance(reference, dict):
                    return reference
                value[key] = reference
            else:
                stop = self.walk(item, component_id)
                if stop is not None:
                    return stop
        return None


def export_workspace_paths(
    app: "FastAPI", session_id: str, components: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | dict[str, Any]:
    """Export workspace file paths in URL-keyed props as artifacts (see module doc).

    Args:
        app: The GACT app.
        session_id: The producing session (its workspace bounds what is exported).
        components: The producer call's components; never mutated.

    Returns:
        ``(components, exported)`` -- a rewritten deep copy plus one entry per
        exported property ``{component_id, property, path, artifact_id, uri,
        name, version}`` -- or a typed refusal dict.
    """

    rewritten = copy.deepcopy(components)
    exporter = _Exporter(app, session_id)
    for component in rewritten:
        component_id = str(component.get("id") or "") if isinstance(component, dict) else ""
        stop = exporter.walk(component, component_id)
        if stop is not None:
            return stop
    return rewritten, exporter.exported


__all__ = ["A2UI_EXPORT_DESIGNATION", "export_workspace_paths"]
