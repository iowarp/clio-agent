"""RO-Crate export — "give me the scripts" (S7 #973, item 3).

Packages a session's (or a single artifact's lineage's) registered artifacts +
TransformRecords into an **RO-Crate**-shaped bundle: a folder carrying
``ro-crate-metadata.json`` (JSON-LD), the artifact bytes under ``data/``, and a
compiled :mod:`reproduce` renderer (``reproduce.py`` + ``reproduce.ipynb``). The
bundle is delivered as a zip by the export routes.

**TransformRecord → schema.org ``CreateAction``** (serialization, not translation —
the record is already CreateAction-shaped, owner decision #966.6): ``{object (the
used inputs), result (the generated outputs), instrument (tool + args, or the
generated script + its hash), agent (the executing model/agent), startTime,
endTime}``. Each artifact **version** is a ``File`` entity carrying its content
identity (``sha256``/``contentSize``) plus the PROV edges ``wasGeneratedBy`` (its
producing activity) and ``wasRevisionOf`` (the version it revises). A **gap**
version (mechanism ``none`` — an undesignated overwrite) is attributed to an
**unknown Agent**, never a false author (owner decision #966.10).

**Export manifests become CAS GC roots** (closing S6's loop, #972): a shipped
bundle registers the content hashes it exported into
``app.state.cas_export_manifest_roots`` so the reachability GC never evicts bytes a
user was handed (:func:`register_export_gc_roots`).

Bounded by construction: only bytes at or under the CAS max-file size are shipped
into ``data/``; a larger referenced version is recorded (identity + a typed
``not-exported`` note) but its bytes are omitted, so one export can never
materialize an unbounded archive.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.cas import CASStore, cas_max_file_bytes
from clio_agent.gact.artifacts.lineage import build_lineage
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry import ArtifactRegistry, get_registry
from clio_agent.gact.artifacts.reproduce import (
    ArtifactNode,
    compile_notebook,
    compile_reproduce,
)
from clio_agent.gact.artifacts.transforms import TransformRecord
from clio_agent.gact.artifacts.wire import mime_for

if TYPE_CHECKING:
    from fastapi import FastAPI

#: RO-Crate 1.1 context + the clio/prov/sha256 vocabulary the crate uses.
#: Both PROV lineage terms are mapped (finding [5]): ``wasGeneratedBy`` is NOT in the
#: RO-Crate 1.1 base context and there is no ``@vocab``, so without this mapping a
#: strict JSON-LD expansion would DROP every File→producing-Activity edge.
_RO_CRATE_CONTEXT: list[Any] = [
    "https://w3id.org/ro/crate/1.1/context",
    {
        "clio": "https://iowarp.github.io/clio-agent/ns#",
        "prov": "http://www.w3.org/ns/prov#",
        "sha256": "https://w3id.org/security#sha256",
        "wasRevisionOf": "prov:wasRevisionOf",
        "wasGeneratedBy": "prov:wasGeneratedBy",
    },
]

#: Default license for the RO-Crate root Dataset (finding [10]). RO-Crate 1.1 marks
#: ``license`` as SHOULD; ``NOASSERTION`` (SPDX) honestly declares "no license
#: asserted" rather than omitting the field. Config-first (#985) via the knob below.
_DEFAULT_EXPORT_LICENSE = "NOASSERTION"


def _export_license() -> str:
    """Resolve the crate root license (finding [10]) from config; default NOASSERTION."""
    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "artifacts.export_license",
        env="CLIO_ARTIFACTS_EXPORT_LICENSE",
        default=_DEFAULT_EXPORT_LICENSE,
        cast=conf.as_str,
    )


def _now_date_iso() -> str:
    """The crate publication date (finding [10]) — ISO 8601 ``YYYY-MM-DD``, UTC."""
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class ExportBundle:
    """An in-memory RO-Crate bundle: crate files + the content hashes it pins."""

    def __init__(self, *, workspace_id: str, name: str) -> None:
        self.workspace_id = workspace_id
        self.name = name
        self.files: dict[str, bytes] = {}
        self.crate_shas: set[str] = set()

    def add_file(self, rel_path: str, data: bytes) -> None:
        self.files[rel_path] = data

    def to_zip(self) -> bytes:
        """Serialize the bundle to a deterministic zip archive."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in sorted(self.files):
                zf.writestr(rel, self.files[rel])
        return buf.getvalue()


def _workspace_root(app: "FastAPI", workspace_id: str) -> Optional[Path]:
    store = getattr(app.state, "workspaces", None)
    if store is None or not workspace_id:
        return None
    try:
        ws = store.get(workspace_id)
    except Exception:  # noqa: BLE001 — an unresolvable workspace yields no root
        return None
    root = str(getattr(ws, "root_path", "") or "") if ws is not None else ""
    return Path(root).expanduser().resolve(strict=False) if root else None


def _session_workspace_id(app: "FastAPI", sid: str) -> str:
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return ""
    session = store.get(sid)
    return str(getattr(session, "workspace_id", "") or "") if session is not None else ""


def _version_bytes(
    app: "FastAPI", root: Optional[Path], version: ArtifactVersion, *, max_bytes: int
) -> Optional[bytes]:
    """The version's bytes (CAS blob first, then the referenced path), or ``None``.

    Bounded: a version whose recorded size exceeds ``max_bytes`` is never read —
    its identity is exported but its bytes are omitted (a typed ``not-exported``
    note rides the crate entity). The CAS blob is preferred (app-owned, survives
    workspace churn); the referenced workspace path is the fallback.
    """
    size = version.size_bytes
    if size is not None and size > max_bytes:
        return None
    from clio_agent.gact.artifacts.storage import resolve_owned_artifact_path  # noqa: PLC0415

    owned = resolve_owned_artifact_path(app, version, workspace_root=root)
    if owned is not None:
        try:
            if owned.stat().st_size <= max_bytes:
                return owned.read_bytes()
        except OSError:
            return None
    receipt = (version.producer or {}).get("storage_receipt")
    if isinstance(receipt, dict) and receipt.get("provider") == "cmf":
        return None
    sha = version.sha256
    if root is not None and sha:
        blob = CASStore(root).blob_path(sha)
        if blob.is_file():
            try:
                data = blob.read_bytes()
            except OSError:
                data = None
            if data is not None and len(data) <= max_bytes:
                return data
    if version.path:
        p = Path(version.path)
        try:
            if p.is_file() and p.stat().st_size <= max_bytes:
                return p.read_bytes()
        except OSError:
            return None
    return None


def _safe_segment(value: str) -> str:
    """Sanitize an id to a single filesystem/@id-safe path segment (no separators)."""
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value))
    return cleaned.strip("._-") or "ws"


def _safe_bundle_name(name: str, version: int, artifact_id: str, workspace_id: str) -> str:
    """A crate-relative ``data/`` path for a version, NAMESPACED by workspace (finding [11]).

    A bundle can union artifacts from several workspaces (a parent orchestrator carries
    its delegates'), where two DIFFERENT records legitimately share a name+version.
    Keying the ``data/`` path on name+version alone silently overwrote one record's
    bytes with another's and minted two File entities with the identical ``@id`` (a
    malformed crate). Prefixing the workspace segment keeps cross-workspace same-name
    artifacts distinct in both the archive and the JSON-LD graph.
    """
    stem = Path(name).name or artifact_id
    suffix = Path(stem).suffix
    base = stem[: len(stem) - len(suffix)] if suffix else stem
    return f"data/{_safe_segment(workspace_id)}/{base}.v{version}{suffix}"


def _build_nodes(
    app: "FastAPI",
    root: Optional[Path],
    records: list[ArtifactRecord],
    bundle: ExportBundle,
) -> dict[str, ArtifactNode]:
    """Resolve every version to an :class:`ArtifactNode`, shipping small bytes to ``data/``."""
    max_bytes = cas_max_file_bytes()
    nodes: dict[str, ArtifactNode] = {}
    for record in records:
        for version in record.versions:
            bundle_path = ""
            data = _version_bytes(app, root, version, max_bytes=max_bytes)
            if data is not None:
                rel = _safe_bundle_name(
                    record.name, version.version, version.artifact_id, record.workspace_id
                )
                bundle.add_file(rel, data)
                bundle_path = rel
                if version.sha256:
                    bundle.crate_shas.add(version.sha256)
            nodes[version.artifact_id] = ArtifactNode(
                artifact_id=version.artifact_id,
                name=record.name,
                version=version.version,
                sha256=version.sha256,
                kind=version.kind.value,
                custody=version.custody.value,
                mechanism=version.mechanism.value,
                path=version.path,
                bundle_path=bundle_path,
            )
    return nodes


# --------------------------------------------------------------------------- #
# RO-Crate JSON-LD serialization.
# --------------------------------------------------------------------------- #


def _node_entity_id(node: ArtifactNode) -> str:
    """The graph @id a resolved version is referenced by (its data path or a synthetic id)."""
    return node.bundle_path or f"#artifact-{node.artifact_id}"


def _file_entity(
    record: ArtifactRecord, version: ArtifactVersion, nodes: dict[str, ArtifactNode]
) -> dict:
    """One ``File`` graph entity for a version (PROV wasGeneratedBy / wasRevisionOf)."""
    node = nodes[version.artifact_id]
    entity: dict[str, Any] = {
        "@id": _node_entity_id(node),
        "@type": ["File", "prov:Entity"],
        "name": record.name,
        "clio:artifact_id": version.artifact_id,
        "clio:version": version.version,
        "clio:kind": version.kind.value,
        "clio:custody": version.custody.value,
        "clio:mechanism": version.mechanism.value,
        "clio:evidence_class": version.evidence.evidence_class.value,
        "encodingFormat": mime_for(version, record.name),
    }
    if version.sha256:
        entity["sha256"] = version.sha256
    if version.size_bytes is not None:
        entity["contentSize"] = version.size_bytes
    if version.evidence.authority:
        entity["clio:authority"] = version.evidence.authority
    if not node.bundle_path:
        entity["clio:bytes_exported"] = False
        entity["clio:not_exported_reason"] = (
            "over_cas_max_file_bytes" if version.size_bytes else "bytes_unavailable"
        )
    producer_call_id = str(version.producer.get("call_id") or "")
    if producer_call_id:
        entity["wasGeneratedBy"] = {"@id": f"#activity-{producer_call_id}"}
    if version.prior_version is not None:
        prior = next((v for v in record.versions if v.version == version.prior_version), None)
        if prior is not None and prior.artifact_id in nodes:
            entity["wasRevisionOf"] = {"@id": _node_entity_id(nodes[prior.artifact_id])}
    return entity


def _agent_entity(transform: TransformRecord) -> dict:
    """A ``SoftwareAgent`` entity for a transform's executing model/agent."""
    env = transform.environment
    ident = env.model_id or transform.agent_id or "unknown"
    return {
        "@id": f"#agent-{ident}",
        "@type": ["prov:SoftwareAgent", "SoftwareApplication"],
        "name": env.model_id or transform.agent_id or "unknown agent",
        "clio:provider_id": env.provider_id,
        "clio:agent_id": transform.agent_id,
    }


_UNKNOWN_AGENT = {
    "@id": "#agent-unknown",
    "@type": ["prov:SoftwareAgent", "SoftwareApplication"],
    "name": "unknown agent",
    "clio:note": "gap version — no attributable producer (mechanism=none)",
}


def _edge_ref(nodes: dict[str, ArtifactNode], edge: Any) -> Optional[dict]:
    """A crate @id reference for a used/generated edge, or ``None`` for a pure external."""
    node = nodes.get(edge.artifact_id) if edge.artifact_id else None
    if node is not None:
        return {"@id": node.bundle_path or f"#artifact-{node.artifact_id}"}
    ref = edge.external_ref or edge.authority
    return {"@id": ref} if ref else None


def _create_action_entity(transform: TransformRecord, nodes: dict[str, ArtifactNode]) -> dict:
    """One ``CreateAction`` graph entity for a TransformRecord (serialization)."""
    instrument = transform.instrument
    instrument_entity: dict[str, Any] = {
        "@id": f"#instrument-{transform.call_id}",
        "@type": ["SoftwareApplication", "prov:Plan"],
        "name": instrument.tool or instrument.cmd or "tool",
        "clio:args": instrument.model_dump().get("args", {}),
    }
    if instrument.script_hash:
        instrument_entity["clio:script_sha256"] = instrument.script_hash
        instrument_entity["clio:cmd"] = instrument.cmd
    objects = [ref for e in transform.used if (ref := _edge_ref(nodes, e)) is not None]
    results = [ref for e in transform.generated if (ref := _edge_ref(nodes, e)) is not None]
    agent_id = (
        "#agent-unknown"
        if any(
            nodes.get(e.artifact_id) and nodes[e.artifact_id].mechanism == "none"
            for e in transform.generated
            if e.artifact_id
        )
        else _agent_entity(transform)["@id"]
    )
    entity: dict[str, Any] = {
        "@id": f"#activity-{transform.call_id}",
        "@type": ["CreateAction", "prov:Activity"],
        "name": f"{instrument.tool or instrument.cmd or 'transform'} ({transform.call_id})",
        "instrument": {"@id": instrument_entity["@id"]},
        "object": objects,
        "result": results,
        "agent": {"@id": agent_id},
        "startTime": transform.started_at,
        "endTime": transform.ended_at,
        "actionStatus": (
            "CompletedActionStatus" if transform.status.value == "success" else "FailedActionStatus"
        ),
        "clio:replay": transform.replay.value,
        "clio:replay_reason": transform.replay_reason,
        "clio:environment_tier": transform.environment.tier.value,
        "clio:kind": transform.kind.value,
    }
    entity["_instrument_entity"] = instrument_entity  # popped by the assembler
    return entity


def _ro_crate_metadata(
    *,
    name: str,
    workspace_id: str,
    records: list[ArtifactRecord],
    transforms: list[TransformRecord],
    nodes: dict[str, ArtifactNode],
    lineage_truncated: Optional[dict[str, Any]] = None,
) -> dict:
    """Assemble the ``ro-crate-metadata.json`` JSON-LD graph.

    ``lineage_truncated`` (#1040): when a transitive upstream closure hit the node
    cap, its typed ``{reason, nodes}`` marker rides the crate root as
    ``clio:lineage_truncated`` — an HONEST partial, never a silent full-looking crate.
    """
    graph: list[dict] = [
        {
            "@id": "ro-crate-metadata.json",
            "@type": "CreativeWork",
            "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
            "about": {"@id": "./"},
        }
    ]
    file_ids: list[dict] = []
    file_entities: list[dict] = []
    for record in records:
        for version in record.versions:
            ent = _file_entity(record, version, nodes)
            file_entities.append(ent)
            file_ids.append({"@id": ent["@id"]})

    activity_entities: list[dict] = []
    instrument_entities: list[dict] = []
    agents: dict[str, dict] = {}
    used_unknown = False
    for transform in transforms:
        action = _create_action_entity(transform, nodes)
        instrument_entities.append(action.pop("_instrument_entity"))
        activity_entities.append(action)
        if action["agent"]["@id"] == "#agent-unknown":
            used_unknown = True
        else:
            ent = _agent_entity(transform)
            agents[ent["@id"]] = ent
    if used_unknown:
        agents["#agent-unknown"] = _UNKNOWN_AGENT

    root_entity: dict[str, Any] = {
        "@id": "./",
        "@type": "Dataset",
        "name": name,
        "description": (
            "clio-agent artifact export (RO-Crate). Artifacts are File entities with "
            "PROV lineage; TransformRecords are CreateActions. reproduce.py compiles the "
            "lineage into a deterministic re-run with per-stage sha256 assertions."
        ),
        # RO-Crate 1.1 Root Data Entity recommended fields (finding [10]):
        # ``datePublished`` (ISO 8601, ``date`` granularity) is a MUST; ``license`` is
        # a SHOULD (``NOASSERTION`` when none is configured). Their absence made the
        # crate formally non-conformant to the profile it declares.
        "datePublished": _now_date_iso(),
        "license": _export_license(),
        "clio:workspace_id": workspace_id,
        "hasPart": [
            *file_ids,
            {"@id": "reproduce.py"},
            {"@id": "reproduce.ipynb"},
        ],
        "mentions": [{"@id": a["@id"]} for a in activity_entities],
    }
    if lineage_truncated is not None:
        root_entity["clio:lineage_truncated"] = lineage_truncated
    graph.append(root_entity)
    graph.extend(file_entities)
    graph.extend(activity_entities)
    graph.extend(instrument_entities)
    graph.extend(agents.values())
    graph.append(
        {
            "@id": "reproduce.py",
            "@type": ["File", "SoftwareSourceCode"],
            "name": "reproduce.py",
            "programmingLanguage": "Python",
            "description": "Deterministic reproduction of the lineage (per-stage sha256 asserts).",
        }
    )
    graph.append(
        {
            "@id": "reproduce.ipynb",
            "@type": ["File", "SoftwareSourceCode"],
            "name": "reproduce.ipynb",
            "description": "Notebook-staged reproduction variant.",
        }
    )
    return {"@context": _RO_CRATE_CONTEXT, "@graph": graph}


# --------------------------------------------------------------------------- #
# Bundle builders.
# --------------------------------------------------------------------------- #


def _assemble_bundle(
    app: "FastAPI",
    *,
    name: str,
    workspace_id: str,
    records: list[ArtifactRecord],
    transforms: list[TransformRecord],
    lineage_truncated: Optional[dict[str, Any]] = None,
) -> ExportBundle:
    """Build the full crate (metadata + bytes + reproduce.py/.ipynb) for a record set."""
    root = _workspace_root(app, workspace_id)
    bundle = ExportBundle(workspace_id=workspace_id, name=name)
    nodes = _build_nodes(app, root, records, bundle)
    metadata = _ro_crate_metadata(
        name=name,
        workspace_id=workspace_id,
        records=records,
        transforms=transforms,
        nodes=nodes,
        lineage_truncated=lineage_truncated,
    )
    bundle.add_file(
        "ro-crate-metadata.json",
        json.dumps(metadata, indent=2, sort_keys=False).encode("utf-8"),
    )
    script = compile_reproduce(transforms, nodes)
    bundle.add_file("reproduce.py", script.text.encode("utf-8"))
    bundle.add_file(
        "reproduce.ipynb",
        json.dumps(compile_notebook(script), indent=1).encode("utf-8"),
    )
    return bundle


def _accumulate_lineage(
    registry: ArtifactRegistry,
    lineage: dict[str, Any],
    *,
    records: list[ArtifactRecord],
    record_keys: set[tuple[str, str]],
    transforms: list[TransformRecord],
    transform_ids: set[str],
) -> None:
    """Re-resolve a lineage graph's WIRE nodes back to registry records + transforms.

    :func:`build_lineage` returns wire dicts (``artifact`` / ``gap`` / ``activity``),
    NOT records. To feed the crate assembler each node id is re-resolved: an
    artifact/gap node → its logical record (:meth:`ArtifactRegistry.get_by_artifact_id`,
    which carries every version); an activity node → its :class:`TransformRecord`
    (:meth:`ArtifactRegistry.get_transform` by ``call_id``). External leaf nodes carry
    no registry record and are skipped. Dedup keys are the caller's ``record_keys``
    (``(workspace_id, name)``) and ``transform_ids`` (``call_id``) so several closures
    merge cleanly into ONE accumulation (the cross-job case, #1040 b). Completeness is
    the crux: every upstream input version the closure surfaced becomes a record here,
    or reproduce silently drops that stage input.
    """
    for node in lineage.get("nodes", []):
        if node.get("type") == "activity":
            call_id = node.get("call_id")
            transform = registry.get_transform(call_id) if call_id else None
            if transform is not None and transform.call_id not in transform_ids:
                transform_ids.add(transform.call_id)
                transforms.append(transform)
            continue
        if node.get("external"):
            continue  # authority-only / external leaf — no registry record to add
        resolved = registry.get_by_artifact_id(node.get("id", ""))
        if resolved is None:
            continue
        record, _version = resolved
        key = (record.workspace_id, record.name)
        if key not in record_keys:
            record_keys.add(key)
            records.append(record)


def build_session_bundle(
    app: "FastAPI", sid: str, *, include_children: bool = True
) -> Optional[ExportBundle]:
    """Build the RO-Crate bundle for a session's artifacts + transforms.

    ``include_children`` unions the descendant child sessions' workspaces (the same
    reach the ``?include_children`` artifact listing uses) so a parent orchestrator's
    export carries its delegates' outputs. Returns ``None`` when the session is
    unknown.
    """
    workspace_id = _session_workspace_id(app, sid)
    if not workspace_id:
        return None
    registry = get_registry(app)
    workspaces = [workspace_id]
    session_ids = [sid]
    if include_children:
        from clio_agent.gact.agent_tasks import descendant_session_ids  # noqa: PLC0415

        for child in descendant_session_ids(app, sid):
            session_ids.append(child)
            child_ws = _session_workspace_id(app, child)
            if child_ws and child_ws not in workspaces:
                workspaces.append(child_ws)
    records: list[ArtifactRecord] = []
    seen: set[tuple[str, str]] = set()
    for ws in workspaces:
        for record in registry.list_for_workspace(ws):
            key = (record.workspace_id, record.name)
            if key not in seen:
                seen.add(key)
                records.append(record)
    transforms: list[TransformRecord] = []
    transform_ids: set[str] = set()
    for child_sid in session_ids:
        for transform in registry.transforms_for_session(child_sid):
            if transform.call_id not in transform_ids:
                transform_ids.add(transform.call_id)
                transforms.append(transform)
    cross_job_truncated = _close_cross_job_inputs(
        app,
        registry,
        workspaces=workspaces,
        records=records,
        record_keys=seen,
        transforms=transforms,
        transform_ids=transform_ids,
    )
    return _assemble_bundle(
        app,
        name=f"clio session {sid} artifacts",
        workspace_id=workspace_id,
        records=records,
        transforms=transforms,
        # A cross-job closure that hit the node cap surfaces its typed truncation into
        # the session crate too — an honest partial, never a silent full-looking crate.
        lineage_truncated=cross_job_truncated,
    )


def _close_cross_job_inputs(
    app: "FastAPI",
    registry: ArtifactRegistry,
    *,
    workspaces: list[str],
    records: list[ArtifactRecord],
    record_keys: set[tuple[str, str]],
    transforms: list[TransformRecord],
    transform_ids: set[str],
) -> Optional[dict[str, Any]]:
    """Gather non-descendant sibling jobs that PRODUCED a consumed input (#1040 b).

    The descendant union (``include_children``) only reaches child workspaces. A
    consumed input can instead be produced by a NON-descendant sibling job in another
    workspace (resolvable fleet-wide via #1038's ``get_by_artifact_id``). For each
    ``transform.used`` edge whose producing record lives OUTSIDE the already-included
    workspaces, run the SAME complete upstream closure over that input and merge its
    full producing chain — records + transforms — reusing the caller's dedup sets. A
    sibling's ENTIRE producing chain lands, not just its terminal record, so a
    cross-job reproduce rebuilds every upstream stage.
    """
    truncated: Optional[dict[str, Any]] = None
    closed_inputs: set[str] = set()
    for transform in list(transforms):
        for edge in transform.used:
            input_id = edge.artifact_id
            if not input_id or input_id in closed_inputs:
                continue
            closed_inputs.add(input_id)
            resolved = registry.get_by_artifact_id(input_id)
            if resolved is None:
                continue
            in_rec, _in_ver = resolved
            if in_rec.workspace_id in workspaces:
                continue  # already covered by the descendant workspace union
            lineage = _provider_lineage(
                app,
                registry,
                input_id,
                direction="upstream",
                complete=True,
            )
            if lineage is None:
                continue
            # Retain the FIRST cross-job closure that hit the node cap, so the session
            # crate surfaces a typed truncation (no silent partial — the caller threads
            # this into clio:lineage_truncated on the crate root).
            if truncated is None and lineage.get("truncated"):
                truncated = lineage["truncated"]
            _accumulate_lineage(
                registry,
                lineage,
                records=records,
                record_keys=record_keys,
                transforms=transforms,
                transform_ids=transform_ids,
            )
    return truncated


def build_artifact_bundle(app: "FastAPI", artifact_id: str) -> Optional[ExportBundle]:
    """Build the RO-Crate bundle for one artifact's TRANSITIVE upstream lineage.

    Drives off the COMPLETE upstream closure (:func:`build_lineage` with
    ``complete=True``, #1040): the logical record owning ``artifact_id`` plus EVERY
    transitive upstream producing record and TransformRecord — not merely the one-hop
    inputs the earlier loop pulled (a 2-hops-up producer was never discovered, so
    reproduce silently dropped that stage's input at ``reproduce.py`` guard
    ``if e.artifact_id in nodes``). Bounded: the closure's node cap + the per-version
    byte cap both hold; a capped closure surfaces its typed ``truncated`` marker into
    the crate root (``clio:lineage_truncated``) — an honest partial, never a silent
    full-looking crate. Returns ``None`` when the artifact id is unknown.
    """
    registry = get_registry(app)
    found = registry.get_by_artifact_id(artifact_id)
    if found is None:
        return None
    record, _version = found
    records: list[ArtifactRecord] = []
    record_keys: set[tuple[str, str]] = set()
    transforms: list[TransformRecord] = []
    transform_ids: set[str] = set()
    lineage = _provider_lineage(
        app,
        registry,
        artifact_id,
        direction="upstream",
        complete=True,
    )
    if lineage is not None:
        _accumulate_lineage(
            registry,
            lineage,
            records=records,
            record_keys=record_keys,
            transforms=transforms,
            transform_ids=transform_ids,
        )
    # The root record anchors the crate even if the closure resolved nothing else.
    root_key = (record.workspace_id, record.name)
    if root_key not in record_keys:
        record_keys.add(root_key)
        records.append(record)
    return _assemble_bundle(
        app,
        name=f"clio artifact {record.name} lineage",
        workspace_id=record.workspace_id,
        records=records,
        transforms=transforms,
        lineage_truncated=lineage.get("truncated") if lineage is not None else None,
    )


def _provider_lineage(
    app: "FastAPI",
    registry: ArtifactRegistry,
    artifact_id: str,
    *,
    direction: str,
    complete: bool,
) -> Optional[dict[str, Any]]:
    """Query the selected artifact provider, falling back for legacy test apps."""
    backend = getattr(app.state, "artifact_provenance_backend", None)
    lineage = getattr(backend, "lineage", None)
    if callable(lineage):
        return lineage(
            artifact_id,
            direction=direction,
            depth=0,
            complete=complete,
        )
    return build_lineage(
        registry,
        artifact_id,
        direction=direction,
        complete=complete,
    )


# --------------------------------------------------------------------------- #
# GC-root registration (closing S6's loop).
# --------------------------------------------------------------------------- #


def register_export_gc_roots(app: "FastAPI", workspace_id: str, shas: set[str]) -> None:
    """Register a bundle's content hashes as CAS GC roots (S6 #972, closing the loop).

    A shipped export pins the blobs it exported: the reachability GC
    (:func:`clio_agent.gact.artifacts.cas_gc.export_manifest_shas`) reads
    ``app.state.cas_export_manifest_roots[workspace_id]`` so a user is never handed a
    bundle whose bytes the GC later evicts. Idempotent (a set union); a no-sha
    export is a no-op.
    """
    if not shas:
        return
    roots = getattr(app.state, "cas_export_manifest_roots", None)
    if not isinstance(roots, dict):
        roots = {}
        app.state.cas_export_manifest_roots = roots
    existing = roots.get(workspace_id)
    if not isinstance(existing, set):
        existing = set(existing) if existing else set()
        roots[workspace_id] = existing
    existing.update(shas)


def registry_snapshot(app: "FastAPI") -> ArtifactRegistry:
    """Convenience accessor used by the routes/tests (the app's registry)."""
    return get_registry(app)


__all__ = [
    "ExportBundle",
    "build_artifact_bundle",
    "build_session_bundle",
    "register_export_gc_roots",
    "registry_snapshot",
]
