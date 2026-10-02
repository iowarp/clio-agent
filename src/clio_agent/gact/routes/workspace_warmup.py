"""Session-free MCP preparation for the temporary entry composer."""

from pathlib import Path

from fastapi import FastAPI, HTTPException

from clio_agent.gact.workspace_warmup import WarmupStatus, start_workspace_warmup


def register_workspace_warmup_route(app: FastAPI) -> None:
    """Register an idempotent, nonblocking request to prepare a workspace fleet."""

    @app.post("/v1/workspaces/{wid}/warmup", status_code=202)
    async def warmup_workspace(wid: str) -> dict[str, WarmupStatus]:
        """Warm the registered root on this service, including remote services."""
        workspace = app.state.workspaces.get(wid)
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"workspace not found: {wid}")
        root = workspace.root_path
        if not root or not Path(root).is_dir():
            raise HTTPException(status_code=409, detail="workspace root is unavailable")
        return {"status": start_workspace_warmup(app, root)}
