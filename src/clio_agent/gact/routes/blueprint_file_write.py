"""Editable Agent Blueprint file route."""

from __future__ import annotations

from typing import Any, Never, Optional

from fastapi import FastAPI, HTTPException

from clio_agent.gact.agent_blueprint_files import (
    _BLUEPRINT_TEXT_FILE_LIMIT_BYTES,
    BlueprintFileNotTextError,
    BlueprintFileTooLargeError,
    BlueprintPathEscapesRootError,
    resolve_agent_blueprint_root,
)
from clio_agent.gact.agent_blueprints import (
    runtime_tool_names_for_validation,
)
from clio_agent.gact.blueprint_drafts import authoring_state, publish_draft, read_draft, save_draft
from clio_agent.gact.events import Event
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def _too_large_message() -> str:
    """Render the 413 prose FROM the limit the writer actually enforces.

    The size is never restated as a literal here: a change to
    :data:`~clio_agent.gact.agent_blueprint_files._BLUEPRINT_TEXT_FILE_LIMIT_BYTES`
    moves the refusal and this message together.
    """

    return (
        "blueprint text files are limited to "
        f"{_BLUEPRINT_TEXT_FILE_LIMIT_BYTES // (1024 * 1024)} MiB"
    )


def register_blueprint_file_write_route(app: FastAPI) -> None:
    """Register the explicit text-file write endpoint."""

    @app.get("/v1/agent-blueprints/{blueprint_id}/authoring")
    async def describe_blueprint_authoring(
        blueprint_id: str, workspace_id: str = "", session_id: str = ""
    ) -> dict[str, Any]:
        """Return draft/source state for the connected CLIO's editor."""
        root = resolve_agent_blueprint_root(
            app, blueprint_id, workspace_id=workspace_id, session_id=session_id
        )
        if root is None:
            _raise(404, "not_found", f"agent blueprint not found: {blueprint_id}", False)
        return await run_off_loop(lambda: authoring_state(root))

    @app.put("/v1/agent-blueprints/{blueprint_id}/files/write")
    async def write_agent_blueprint_file(
        blueprint_id: str,
        path: str,
        req: dict[str, Any],
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        root = resolve_agent_blueprint_root(
            app, blueprint_id, workspace_id=workspace_id or "", session_id=session_id or ""
        )
        if root is None:
            _raise(404, "not_found", f"agent blueprint not found: {blueprint_id}", False)
        content = req.get("content")
        if not isinstance(content, str):
            _raise(400, "validation_error", "content must be a string", True)
        try:
            result = await run_off_loop(
                lambda: save_draft(
                    root,
                    path,
                    content,
                    expected_hash=req.get("expected_hash"),
                    runtime_tool_names=runtime_tool_names_for_validation(app),
                )
            )
        except BlueprintPathEscapesRootError:
            _raise(400, "path_outside_blueprint", f"path escapes blueprint root: {path}", False)
        except FileNotFoundError:
            _raise(404, "not_found", f"file not found: {path}", False)
        except BlueprintFileNotTextError:
            _raise(
                415,
                "unsupported_media_type",
                f"blueprint file is not editable text: {path}",
                False,
            )
        except BlueprintFileTooLargeError:
            _raise(413, "content_too_large", _too_large_message(), True)
        except ValueError as exc:
            _raise(409, "draft_conflict", str(exc), True)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=_error("write_failed", f"could not write file: {exc}", True),
            ) from exc
        app.state.bus.publish(
            Event(
                type="blueprint.authoring.changed",
                session_id="",
                payload={"identity": blueprint_id},
            )
        )
        return result

    @app.get("/v1/agent-blueprints/{blueprint_id}/draft")
    async def read_blueprint_draft(
        blueprint_id: str, path: str, workspace_id: str = "", session_id: str = ""
    ) -> dict[str, str]:
        """Read a saved draft or the applied file before the first draft save."""
        root = resolve_agent_blueprint_root(
            app, blueprint_id, workspace_id=workspace_id, session_id=session_id
        )
        if root is None:
            _raise(404, "not_found", f"agent blueprint not found: {blueprint_id}", False)
        try:
            return await run_off_loop(lambda: read_draft(root, path))
        except BlueprintPathEscapesRootError:
            _raise(400, "path_outside_blueprint", "path escapes blueprint root", False)
        except FileNotFoundError:
            _raise(404, "not_found", f"file not found: {path}", False)
        except (BlueprintFileNotTextError, UnicodeDecodeError):
            _raise(415, "unsupported_media_type", "file is not editable UTF-8 text", False)
        except BlueprintFileTooLargeError:
            _raise(413, "content_too_large", _too_large_message(), False)

    @app.post("/v1/agent-blueprints/{blueprint_id}/publish")
    async def publish_agent_blueprint_draft(
        blueprint_id: str,
        req: dict[str, Any],
        workspace_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Publish a saved draft to its authoring source, leaving runtime unchanged."""
        root = resolve_agent_blueprint_root(
            app, blueprint_id, workspace_id=workspace_id, session_id=session_id
        )
        if root is None:
            _raise(404, "not_found", f"agent blueprint not found: {blueprint_id}", False)
        try:
            result = await run_off_loop(
                lambda: publish_draft(
                    root,
                    checkout=str(req.get("checkout") or ""),
                    commit_message=str(req.get("commit_message") or ""),
                    push=bool(req.get("push", False)),
                    runtime_tool_names=runtime_tool_names_for_validation(app),
                )
            )
            app.state.bus.publish(
                Event(
                    type="blueprint.authoring.changed",
                    session_id="",
                    payload={"identity": blueprint_id},
                )
            )
            return result
        except (ValueError, BlueprintPathEscapesRootError) as exc:
            _raise(409, "publish_conflict", str(exc), True)
        except OSError as exc:
            _raise(500, "publish_failed", str(exc), True)


def _error(code: str, message: str, recoverable: bool) -> dict[str, Any]:
    return ErrorEnvelope(
        error=ErrorInfo(error=code, message=message, recoverable=recoverable)
    ).model_dump(exclude_none=True)


def _raise(status_code: int, code: str, message: str, recoverable: bool) -> Never:
    raise HTTPException(status_code=status_code, detail=_error(code, message, recoverable))
