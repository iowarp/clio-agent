"""Files a browser call produces (screenshots, downloads): written to scratch, minted.

They land in the owning session's scratch folder and are minted as session artifacts, the
way ``capture_a2ui_surface`` mints its image, so the transcript and the Evidence view can
show them. The page they came from is declared on the call's result (``page.url``), which
the transform record reads as the file's ``used`` source (BR15).
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from clio_agent.gact.browser.settings import BrowserError
from clio_agent.runtime import trace

__all__ = ["mint", "scratch_folder"]

_COUNTER = itertools.count(1)


def scratch_folder(session_id: str, name: str) -> Path:
    """``<session scratch>/browser/<name>``, created.

    Raises:
        BrowserError: ``browser_scratch_unavailable`` when the session has no scratch.
    """
    from clio_agent.runtime.session_scratch import session_scratch_dir  # noqa: PLC0415

    root = session_scratch_dir(session_id)
    if root is None:
        raise BrowserError("browser_scratch_unavailable", "this session has no scratch folder")
    folder = root / "browser" / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def screenshot_path(session_id: str, page_id: str) -> Path:
    return scratch_folder(session_id, "screenshots") / f"{page_id}-{next(_COUNTER)}.png"


def mint(app: Any, session_id: str, path: Path, *, image: bool, page_url: str) -> dict[str, Any]:
    """Mint ``path`` as a session artifact; ``{"artifact_id": ...}`` or a typed note."""
    if app is None:
        return {"artifact_note": "browser_artifact_no_app"}
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _session_workspace_id,
        _workspace_root,
        artifact_name_for_path,
        mint_artifact,
    )
    from clio_agent.gact.artifacts.observer_bridge import (  # noqa: PLC0415
        note_call_output,
        observer_call_id,
    )
    from clio_agent.gact.artifacts.records import ArtifactKind, Mechanism  # noqa: PLC0415
    from clio_agent.gact.artifacts.storage import ingest_artifact_identity  # noqa: PLC0415

    try:
        workspace_id = _session_workspace_id(app, session_id)
        root = _workspace_root(app, workspace_id)
        ingested = ingest_artifact_identity(app, path, workspace_root=root)
        version = mint_artifact(
            app,
            session_id,
            name=artifact_name_for_path(path),
            workspace_id=workspace_id,
            evidence=ingested.evidence,
            kind=ArtifactKind.IMAGE if image else ArtifactKind.OTHER,
            mechanism=Mechanism.HARNESS,
            producer={
                "designation": "browser_screenshot" if image else "browser_download",
                "session_id": session_id,
                "page_url": page_url,
                "call_id": observer_call_id(),
            },
            custody=ingested.custody,
            path=str(path),
            ingested=ingested,
            not_ingested_size=ingested.not_ingested_size,
        )
    except Exception as exc:  # noqa: BLE001 - the file stays; the note says why it is unminted
        trace.event("BROWSER", "browser_artifact_unminted path=%s error=%s", path, exc)
        return {"artifact_note": f"browser_artifact_unminted: {exc}"[:300]}
    if version is None:
        return {"artifact_note": "browser_artifact_unminted"}
    note_call_output(version)  # the call's transform names it generated, from its page
    return {"artifact_id": version.artifact_id}
