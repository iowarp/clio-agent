"""Native artifact designation tool and its declared presentation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.proposals import (
    parse_proposals,
    promote_proposals,
)

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef

#: Said with each PDF preview, where the question of what to publish arises.
PREVIEW_NOTE = (
    "A version-bound PDF preview of the source for the viewer, kept in its lineage. The "
    "source file is the deliverable: name it, and register or list this PDF only when a "
    "PDF deliverable was asked for."
)
PREVIEW_FAILED_NOTE = "No preview was made; the source artifact is registered unchanged."


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
        pdf_preview: Optional[bool] = None,
        purpose: str = "deliverable",
    ) -> dict[str, Any]:
        """Register a session output as an artifact (model contract lives in the dspy ``desc``).

        ``path`` registers an EXISTING file; ``content`` authors a NEW file WRITTEN AT
        ``name`` — the target path (workspace-relative or absolute), not a display label.

        ``used`` (#1191, OPTIONAL): cites inputs this deliverable was DERIVED FROM
        (paths, artifact ids, an uploaded attachment's ``res_...`` id, and/or exact
        HTTP(S) source URLs). NOT threaded into the promotion below —
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
            purpose=purpose,
        )
        result = promote_proposals(
            app,
            sid,
            proposals,
            workspace_id=workspace_id,
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
            agent_id=agent_id,
        )
        if pdf_preview is not False or any(proposal.pdf_preview is True for proposal in proposals):
            from clio_agent.gact.artifacts.registry import get_registry
            from clio_agent.gact.documents.profiles import document_format
            from clio_agent.gact.documents.renditions import RenditionError, render_pdf

            previews: list[dict[str, Any]] = []
            for proposal, item in zip(proposals, result["artifacts"], strict=False):
                preview_option = (
                    proposal.pdf_preview if proposal.pdf_preview is not None else pdf_preview
                )
                if preview_option is False:
                    continue
                if not item.get("accepted"):
                    continue
                found = get_registry(app).get_by_artifact_id(item["artifact_id"])
                if found is None:
                    continue
                record, version = found
                format_row = document_format(record.name)
                if "pdf" not in format_row.rendition_formats:
                    continue
                if preview_option is None and not format_row.profile.startswith(("ooxml-", "odf-")):
                    continue
                try:
                    rendered = render_pdf(app, sid, record, version)
                except RenditionError as exc:
                    previews.append(
                        {
                            "source_artifact_id": version.artifact_id,
                            "error": str(exc),
                            "note": PREVIEW_FAILED_NOTE,
                        }
                    )
                else:
                    previews.append(
                        {
                            "source_artifact_id": version.artifact_id,
                            "artifact_id": rendered.version.artifact_id,
                            "name": rendered.record.name,
                            "sha256": rendered.version.sha256,
                            "converter": rendered.converter,
                            "path": str(rendered.workspace_path),
                            "note": PREVIEW_NOTE,
                        }
                    )
            result["pdf_previews"] = previews
        return result

    # Declared "chip": normal tool_call/tool_result parts PLUS its resource_link
    # chip, appended at turn finalize — adornment, never a call-row replacement.
    return native_tool(
        create_artifact,
        name="create_artifact",
        presentation="artifact",
        domain="artifacts",
        title="Create Artifact",
        representation="chip",
        desc=(
            "Designate an output you decide is worth keeping (a report, a document you "
            "wrote, a generated file) as an artifact. Register an existing workspace file "
            "with path, or write content to name, which is the target path, not a label. "
            "Only purpose=deliverable outputs appear on the final response; every purpose "
            "stays in lineage. Accepted outputs appear as artifact cards: name them in your "
            "final answer rather than linking local paths or artifact:// URIs. Each item "
            "returns its record or a typed rejection (path_missing, escapes_root, over_cap, "
            "invalid_kind, missing_input) you can correct and retry; check every batch "
            "item's accepted flag. promotion_budget gives this turn's remaining slots. "
            "Nothing is registered unless you call this."
        ),
        args={
            "purpose": {
                "type": "string",
                "enum": ["deliverable", "intermediate", "verification"],
                "default": "deliverable",
                "description": "Response output, working intermediate, or review evidence; all are retained. Batch items may override this default.",
            },
            "pdf_preview": {
                "type": ["boolean", "null"],
                "default": None,
                "description": "Word, Excel, PowerPoint and OpenDocument files get a PDF preview for the viewer automatically, kept in lineage rather than as a deliverable; false omits it, true requests one for another supported format.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Target path the inline content is written to (workspace-relative "
                    "or absolute; directories kept, e.g. 'reports/analysis.md'); when a "
                    "destination is required, the full path. Required for inline content."
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
                "description": "Batch: {name,kind,path|content,annotation,purpose?,pdf_preview?} proposals. Each item's boolean pdf_preview overrides the call default; omitted or null inherits it.",
            },
            "used": {
                "type": "array",
                "description": (
                    "Optional inputs this deliverable was derived from, shown in its "
                    "lineage: workspace paths, artifact ids, an uploaded attachment's res_... "
                    "id or its working-copy path, and/or exact HTTP(S) source URLs. URLs are "
                    "assertion-class evidence; unresolved file or artifact refs are typed, "
                    "never silently dropped."
                ),
            },
        },
    )
