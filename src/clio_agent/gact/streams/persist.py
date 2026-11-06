"""A stream's rows written as its artifact's next version (Parquet, harness-minted)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.streams.store import Stream


def rows_with_columns(rows: list[dict[str, Any]], columns: list[str]) -> Any:
    """The rows as an Arrow table holding every declared column (empty ones as text)."""
    import pyarrow as pa  # noqa: PLC0415

    from clio_agent.gact.science_log.sources import rows_table  # noqa: PLC0415

    table = rows_table(rows) if rows else pa.table({})
    for column in columns:
        if column not in table.column_names:
            table = table.append_column(column, pa.array([None] * table.num_rows, pa.string()))
    return table


def write_version(
    app: Any, stream: "Stream", rows: list[dict[str, Any]], columns: list[str]
) -> str:
    """Write the rows to the workspace's state folder and mint them; the version's id."""
    import pyarrow.parquet as pq  # noqa: PLC0415

    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _contained,
        _workspace_root,
        mint_artifact_outcome,
    )
    from clio_agent.gact.artifacts.records import ArtifactKind, Mechanism  # noqa: PLC0415
    from clio_agent.gact.artifacts.storage import ingest_artifact_identity  # noqa: PLC0415
    from clio_agent.gact.streams.store import StreamError  # noqa: PLC0415

    root = _workspace_root(app, stream.workspace_id)
    if root is None:
        raise StreamError("stream_workspace_missing", "The stream's workspace is not bound.")
    target = (paths.workspace_state_dir(root) / "streams" / stream.artifact_name).resolve()
    if not _contained(target, root) and not target.is_relative_to(paths.user_state_dir()):
        raise StreamError("stream_storage_outside", "The stream folder resolves elsewhere.")
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(rows_with_columns(rows, columns), target)
    ingested = ingest_artifact_identity(app, target, workspace_root=root)
    outcome = mint_artifact_outcome(
        app,
        stream.session_id,
        name=stream.artifact_name,
        workspace_id=stream.workspace_id,
        evidence=ingested.evidence,
        kind=ArtifactKind.DATASET,
        mechanism=Mechanism.HARNESS,
        turn_id=context.active_turn_id() or "",
        trace_id=context.active_trace_id() or "",
        producing=False,  # the stream persisting itself, not an output of the turn
        producer={
            "designation": "stream",
            "stream_id": stream.id,
            "session_id": stream.session_id,
            "state": stream.state,
            "writes": stream.seq,
        },
        custody=ingested.custody,
        path=str(target),
        ingested=ingested,
        not_ingested_size=ingested.not_ingested_size,
    )
    version = getattr(outcome, "version", None)
    return str(getattr(version, "artifact_id", "") or "")


__all__ = ["rows_with_columns", "write_version"]
