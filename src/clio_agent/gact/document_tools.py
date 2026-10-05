"""Document runtime and bounded preparation tools declared by the default agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.runtime.document_runtime import prepare_document_runtime, run_document_helper
from clio_agent.tools.execution import get_active_tool_workspace_root
from clio_agent.tools.file_policy import FileAccessPolicy


def _workspace() -> Path:
    raw = get_active_tool_workspace_root().strip()
    if not raw:
        raise ValueError("Document tools require an active workspace")
    return Path(raw).resolve(strict=True)


def build_prepare_document_runtime_tool(*, execution: bool = False) -> Any:
    """Build the runtime-preparation tool with execution-host inventory."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    def prepare_runtime() -> dict[str, Any]:
        """Prepare Clio's locked document runtime and report verified executable paths,
        packages, native converter availability, fonts, skill locations and workspace.

        Use before Python or JavaScript work. It selects bundled Python/uv and
        Node/pnpm, or automatically prepares their locked local equivalents.
        The shell receives these tools after preparation. Use returned commands
        for standalone scripts; preserve project-owned dependency environments.
        """
        return prepare_document_runtime(_workspace())

    return native_tool(
        prepare_runtime,
        name="prepare_execution_runtime" if execution else "prepare_document_runtime",
        domain="workspace",
        title="Prepare execution runtime" if execution else "Prepare document tools",
        desc=prepare_runtime.__doc__,
        args={},
        presentation="fields:status,os,architecture,python_version,node_version",
    )


def build_prepare_execution_runtime_tool() -> Any:
    """Expose managed execution separately from document authoring guidance."""
    return build_prepare_document_runtime_tool(execution=True)


def build_prepare_document_tool() -> Any:
    """Build bounded extraction, rendering and spreadsheet-recalculation access."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    def prepare_document(
        path: str,
        action: str = "inspect",
        pages: str = "",
        sheet: str = "",
        cell_range: str = "",
        dpi: int = 120,
    ) -> dict[str, Any]:
        """Inspect or render a workspace document, or recalculate an XLSX copy.

        Source files remain unchanged. Read the returned manifest and bounded content
        derivative. Rendering produces PNG pages but does not perform visual review:
        use view_image or view_pdf when available. Long PDFs/decks require pages such
        as '1-5,8'; large workbooks require a sheet and cell_range such as 'A1:D20'.
        Recalculation checks cached formulas and errors, not business correctness.
        """
        if action not in {"inspect", "render", "recalculate"}:
            raise ValueError("action must be inspect, render or recalculate")
        if not 72 <= dpi <= 200:
            raise ValueError("dpi must be between 72 and 200")
        root = _workspace()
        requested = Path(path)
        candidate = requested if requested.is_absolute() else root / requested
        source = FileAccessPolicy.from_env().validate_read(str(candidate), field="path")
        if not source.is_relative_to(root):
            raise ValueError("Document must be inside the active workspace")
        runtime = prepare_document_runtime(root)
        args = [
            action,
            str(source),
            runtime["output_directory"],
            "--pages",
            pages,
            "--sheet",
            sheet,
            "--cell-range",
            cell_range,
            "--dpi",
            str(dpi),
        ]
        return run_document_helper(runtime, args, cwd=root)

    return native_tool(
        prepare_document,
        name="prepare_document",
        domain="workspace",
        title="Prepare document",
        desc=prepare_document.__doc__,
        args={
            "path": {"type": "string", "description": "Path to an existing workspace document."},
            "action": {
                "type": "string",
                "enum": ["inspect", "render", "recalculate"],
                "default": "inspect",
            },
            "pages": {
                "type": "string",
                "default": "",
                "description": "1-based page/slide selection, at most 40.",
            },
            "sheet": {"type": "string", "default": ""},
            "cell_range": {"type": "string", "default": ""},
            "dpi": {"type": "integer", "minimum": 72, "maximum": 200, "default": 120},
        },
        presentation="fields:status,action,manifest,error",
    )
