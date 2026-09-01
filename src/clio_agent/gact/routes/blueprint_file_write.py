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
    write_blueprint_text_file,
)
from clio_agent.gact.agent_blueprints import (
    runtime_tool_names_for_validation,
    validate_agent_blueprint_path,
)
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
            entry = write_blueprint_text_file(root, path, content)
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
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=_error("write_failed", f"could not write file: {exc}", True),
            ) from exc
        validation = validate_agent_blueprint_path(
            root,
            scope="session" if session_id else "workspace" if workspace_id else "global",
            runtime_tool_names=runtime_tool_names_for_validation(app),
        )
        return {"entry": entry, "validation": validation}


def _error(code: str, message: str, recoverable: bool) -> dict[str, Any]:
    return ErrorEnvelope(
        error=ErrorInfo(error=code, message=message, recoverable=recoverable)
    ).model_dump(exclude_none=True)


def _raise(status_code: int, code: str, message: str, recoverable: bool) -> Never:
    raise HTTPException(status_code=status_code, detail=_error(code, message, recoverable))
