"""Native artifact designation tool and its declared presentation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.proposals import parse_proposals, promote_proposals

if TYPE_CHECKING:
    from clio_agent.gact.agents.types import AgentDef


def build_create_artifact_tool(agent_def: "AgentDef") -> Any:
    """The ``create_artifact`` DSPy tool (auto-attached runtime infrastructure).

    Attached to EVERY react expert alongside ``load_skill`` (NOT children-gated,
    NOT part of the 5-7 curated domain-tool budget). Register an existing workspace
    file by ``path``, or author a deliverable in-context and pass it as ``content``
    (it lands as a workspace file + record). Batch via ``artifacts=[{...}, ...]``.
    Returns the typed record on acceptance or a typed rejection the model can react
    to. The harness computes every hash — any ``sha256`` in the args is ignored.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    agent_id = str(getattr(agent_def, "id", "") or "")

    def create_artifact(
        name: str = "",
        kind: str = "",
        path: str = "",
        content: str = "",
        annotation: str = "",
        artifacts: Optional[list[dict[str, Any]]] = None,
        used: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Register a session output as an artifact (model contract lives in the dspy ``desc``).

        ``path`` registers an EXISTING file; ``content`` authors a NEW file WRITTEN AT
        ``name`` — the target path (workspace-relative or absolute), not a display label.

        ``used`` (#1191, OPTIONAL): cites inputs this deliverable was DERIVED FROM
        (paths, artifact ids, and/or exact HTTP(S) source URLs). NOT threaded into the promotion below —
        the mint decision is unaffected. The tool-observer transform seam
        (``declared_used_edges.detect_declared_used_edges``, fired AFTER this call
        returns) reads it from this call's own args and records real ``used`` PROV
        edges on the producing activity; an unresolvable ref is typed, never
        fabricated; omitted/blank leaves the mint exactly as it is today.
        """
        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        if app is None or not sid:
            return {
                "artifacts": [],
                "created": 0,
                "deduplicated": 0,
                "rejected": 0,
                "error": "create_artifact called outside an active session",
            }
        from clio_agent.gact.artifacts.minting import _session_workspace_id  # noqa: PLC0415

        workspace_id = _session_workspace_id(app, sid)
        proposals = parse_proposals(
            name=name,
            kind=kind,
            path=path,
            content=content,
            annotation=annotation,
            artifacts=artifacts,
        )
        return promote_proposals(
            app,
            sid,
            proposals,
            workspace_id=workspace_id,
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
            agent_id=agent_id,
        )

    # Declared "chip": normal tool_call/tool_result parts PLUS its resource_link
    # chip, appended at turn finalize — adornment, never a call-row replacement.
    return native_tool(
        create_artifact,
        name="create_artifact",
        presentation="artifact",
        title="Create Artifact",
        representation="chip",
        desc=(
            "Designate a deliverable as a first-class artifact — YOU decide what is "
            "worth keeping (a report, a document you wrote, a generated file). "
            "Register an existing workspace file with path=<workspace path>, OR "
            "author content in this turn and pass it as content=<text> with "
            "name=<target path> — the file is WRITTEN AT name (workspace-relative "
            "or absolute; directories kept, e.g. '.clio/plans/my-plan.md'), so when "
            "a specific destination is required, name must be that full path, not a "
            "bare filename. kind is one of "
            "dataset|image|report|script|config|model|ui_payload|other. Put your "
            "intent (why it matters, deliverable vs scratch) in annotation. To "
            "designate several at once pass artifacts=[{name,kind,path|content,"
            "annotation}, ...]. OPTIONAL: cite what this deliverable was DERIVED "
            "FROM via used=[...] (paths, artifact ids, and/or exact source URLs) so its lineage graph "
            "shows its real inputs. Returns each record on acceptance, or a typed rejection reason "
            "(path_missing, escapes_root, over_cap, invalid_kind, missing_input) you "
            "can correct and retry. Nothing is auto-registered; the artifact exists "
            "only because you called this."
        ),
        args={
            "name": {
                "type": "string",
                "description": (
                    "Target path the inline content is written to (workspace-relative "
                    "or absolute; directories kept); required for inline content."
                ),
            },
            "kind": {
                "type": "string",
                "description": "One of dataset|image|report|script|config|model|ui_payload|other.",
            },
            "path": {
                "type": "string",
                "description": "Existing workspace file to register (mutually exclusive with content).",
            },
            "content": {
                "type": "string",
                "description": "Inline content to write as a workspace file, then register.",
            },
            "annotation": {
                "type": "string",
                "description": "Your intent/why (quarantined; never trusted as evidence).",
            },
            "artifacts": {
                "type": "array",
                "description": "Batch: a list of {name,kind,path|content,annotation} proposals.",
            },
            "used": {
                "type": "array",
                "description": (
                    "OPTIONAL: workspace paths, artifact ids, and/or exact HTTP(S) source URLs "
                    "this deliverable was derived from. URLs are assertion-class evidence; "
                    "unresolved file or artifact refs are typed, never silently dropped."
                ),
            },
        },
    )
