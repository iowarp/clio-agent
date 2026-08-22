# Optional Provenance Providers and Artifact Storage

- **Status:** Implemented first slice with real CMF, Flowcept, NDP, and Spotter verification
- **Date:** 2026-08-21
- **Scope:** `clio-agent` provenance semantics, optional downstream provenance providers,
  artifact identity and custody, and the proposed CMF integration boundary
- **Audience:** CLIO, Flowcept, and HPE CMF collaborators working on the genesis proposal

This document records the conclusions, corrections, rejected alternatives, implementation,
and open questions from the provenance-integration work. It distinguishes the first
implemented slice from later storage and distributed-provider extensions.

External source snapshots inspected during the discussion:

- HPE CMF: `53d9c3e518ab2fde46955f10520d4842c572bf05`
- ORNL Flowcept: `c000b10ea49659af6c5821b61918f3893bd46a92`
- IOWarp Core: `a02bc8e7813b09f81b616a96283d02626ecf1c22`

The external repositories were inspected but not modified. A real Flowcept full-online
Redis/MongoDB profile was deployed on the `homelab` SSH host. A real CMF 0.1.0 and
ML Metadata 1.15.0 runtime was also installed there in an isolated Python 3.9 environment
and exercised against the CLIO CMF worker. Section 14 records the exact implementation and
live-verification boundary.

## Contents

1. [Executive conclusion](#1-executive-conclusion)
2. [Vocabulary and ownership](#2-vocabulary-and-ownership)
3. [Current CLIO provenance semantics](#3-current-clio-provenance-semantics)
4. [Target provenance stream and provider architecture](#4-target-provenance-stream-and-provider-architecture)
5. [Artifact identity and provider-scoped storage](#5-artifact-identity-and-provider-scoped-storage)
6. [CMF analysis and final design](#6-cmf-analysis-and-final-design)
7. [Spotter-AI MCP and the CLIO UI](#7-spotter-ai-mcp-and-the-clio-ui)
8. [Flowcept analysis and final role](#8-flowcept-analysis-and-final-role)
9. [Decision ledger](#9-decision-ledger)
10. [Suggested implementation sequence](#10-suggested-implementation-sequence)
11. [Acceptance criteria](#11-acceptance-criteria)
12. [Questions for the HPE CMF team](#12-questions-for-the-hpe-cmf-team)
13. [Source evidence index](#13-source-evidence-index)
14. [Implemented slice and live verification](#14-implemented-slice-and-live-verification)

## 1. Executive conclusion

ARC is the source of one semantic-event highway. That entire highway is the **agentic
provenance stream**. Artifact provenance is not a second source or a disjoint branch: it
is a selected, extensible **substream** of the agentic stream.

The two stream contracts have different providers:

1. **Agentic provenance providers** receive the whole highway by default. The native
   JSONL provider is the default; a queryable CLIO memory/disk pool and Flowcept are
   additional providers. A provider may deliberately filter, sample, or reduce events
   internally. For example, Flowcept may discard token deltas or thinking events without
   changing central routing.
2. **Artifact provenance providers** receive the artifact-provenance substream. The
   native CLIO provider and CMF are alternative implementations of this specialized
   contract. Artifact storage is configured *inside* the selected artifact provider:
   native CLIO can use filesystem CAS or clio-core; CMF can use its MLMD metadata stores
   and DVC local or remote artifact stores.

The resulting architecture is:

```text
ARC semantic-event highway = agentic provenance stream
|
+-- agentic provenance providers (configured set; JSONL default)
|   +-- jsonl
|   +-- clio                 # separate queryable memory/disk pool
|   `-- flowcept             # optional; filters/maps internally
|
`-- artifact provenance substream (selected from the same events)
    +-- native
    |   `-- storage: file | clio_core
    `-- cmf
        +-- metadata: local MLMD/SQLite | PostgreSQL/server
        `-- artifacts: DVC local | MinIO | S3 | SSH | OSDF/other supported remotes
```

An event such as `artifact.created` exists once on the ARC highway. Agentic providers may
retain or project it, and the artifact selector also places it on the artifact substream.
There is no duplicate emission and no hard, mutually exclusive bifurcation.

The important consequences are:

- ARC is not a selectable provenance backend. It is the live context/memory substrate and
  the source of the semantic-event highway.
- JSONL is the native default agentic-provenance provider, not special logic outside the
  provider protocol.
- Flowcept is an optional agentic-provenance provider. CMF is an optional artifact-
  provenance provider. They are not parallel implementations of one flat adapter
  contract and must not become required dependencies.
- The current `trace.backend=none|file|factory` surface should become a compatibility input
  to the new agentic-provider configuration rather than a second, competing architecture.
- The CLIO UI and query surfaces must use the selected provider interfaces rather than
  depend directly on one provider's files, database, UI, or storage layout.
- CMF can be supported without deploying any CMF infrastructure. CMF's local artifact
  backend is a DVC-backed filesystem content-addressed store.
- No new homelab containers are required for the initial CMF integration.
- Flowcept does not require decorator instrumentation for this integration: it supports
  explicit programmatic tasks and a common message/consumer path. CLIO should feed it at
  the existing semantic-event boundary and keep Flowcept-specific filtering in the
  Flowcept provider.

## 2. Vocabulary and ownership

The terms below are intentionally narrow.

### 2.1 Canonical event

A `SemanticEvent` accepted by ARC. It carries event, session, workspace, trace, turn and
span identity; status and time; actor, subject, blueprint and provider provenance; and an
event-specific payload. The full event is the semantic source. Redacted SSE, hooks, JSONL,
CMF, and Flowcept records are projections of it.

### 2.2 Agentic provenance stream

The complete ARC semantic-event highway. It contains agent, model, tool, delegation,
workflow, artifact, telemetry, governance, and runtime observations. An agentic provider
receives this parent stream by default and decides internally which events it can use.

### 2.3 Artifact provenance substream

An overlapping selection from the agentic stream, initially including artifact creation,
version, alias, use, and transform facts. The selection is explicit and extensible; it is
not a new event source, a second highway, a hard domain enum, or necessarily every event
whose name begins with `artifact.`.

### 2.4 Provenance provider

A downstream implementation of either the agentic or artifact provenance contract. A
provider maps, stores, indexes, or transports already-recorded ARC semantic facts. It does
not own ARC live context or rewrite ARC history. Provider-specific filtering belongs in
the provider and must distinguish deliberate filtering from delivery failure.

### 2.5 Artifact identity

The claim that a recorded artifact version corresponds to particular bytes. Today this is
usually CLIO SHA-256 evidence, but large files may carry weaker stat-pinned identity.
Identity evidence and storage location are different facts.

### 2.6 Artifact custody

The system responsible for retrieving the bytes later. Examples are CLIO CAS, a workspace
path, CTE, and a CMF/DVC local or remote repository.

### 2.7 Provider receipt

Evidence that a downstream projection was accepted, filtered, retried, or failed. A
receipt should contain the ARC event id, provider and mapping version, downstream
identifiers, disposition, timestamp, and a typed reason or error.

### 2.8 Storage receipt

Evidence returned by an artifact provider's storage implementation after accepting bytes.
It should contain the provider, storage backend, stable object URI/key, byte count,
algorithm-qualified digest set, and any backend-native version or custody metadata.

## 3. Current CLIO provenance semantics

### 3.1 ARC is the source of the semantic highway

The current runtime explicitly implements ARC-as-source:

```text
_emit_semantic_event
    -> ARC.record_semantic_event(event)
         1. persist/fold under ARC's reserved _events family
         2. call the injected highway sink
              -> durable trace backend
              -> selected SSE projection
              -> hooks
```

`src/clio_agent/arc/memory.py::record_semantic_event` records the event before invoking the
downstream highway sink. `src/clio_agent/gact/app.py` intentionally gives
`SemanticEventSink` no ARC live consumer, preventing recursion or double folding.

This ordering is the non-negotiable integration boundary:

> An optional provider receives an event only after ARC has had the opportunity to record
> it. No provider replaces ARC.

ARC's `_events` family is also the source from which live conversations, invocations,
artifact records, and other projections are rebuilt. It is excluded from normal prompt
rendering and plain-text search. ARC may release its live `_events` materialization when a
durable trace preserves the history; when durable tracing is disabled, it retains the log
to avoid destroying the only copy.

### 3.2 SemanticEvent is the integration envelope

The current schema version is `clio.semantic_event.v1`. Its envelope includes:

- `event_id` / `span_id` and `parent_span_id`
- `event_type`
- `session_id`, `workspace_id`, `trace_id`, and `turn_id`
- `status`, `summary`, `live_observed`, `detail_level`, and `occurred_at`
- structured `actor`, `subject`, `blueprint`, `provider`, and `payload` bodies

CLIO captures the full event and projects it per consumer. SSE and ordinary hooks can
receive redacted representations; the durable trace receives the full representation.
That projection discipline must remain provider-specific. A Flowcept agentic provider or
CMF artifact provider must not change what ARC captured merely because its target accepts
fewer fields.

### 3.3 The current JSONL backend and factory

`src/clio_agent/gact/semantic_events.py` currently defines a small
`SemanticTraceBackend` protocol with `name` and `emit(event)`, plus:

- `NoopSemanticTraceBackend`
- `FileSemanticTraceBackend`
- a dynamic Python factory selected by `trace.backend=factory`

The file backend appends full-event JSONL from a shared writer thread. The configuration is
currently:

```yaml
trace.backend: none       # current default
# trace.path:
# trace.semantic_factory:
# trace.semantic_config:
```

This existing factory proves that arbitrary downstream sinks are possible, but it has four
limitations for the genesis goal:

1. It selects one backend rather than deliver to a configured agentic provider set.
2. JSONL is an implementation class rather than a named peer in a provenance package.
3. The protocol does not describe capabilities, health, lag, receipts, flush semantics, or
   bounded failure behavior.
4. Mapping and transport concerns are not separated.

There is also a current implementation gate: durable JSONL defaults to `none` because
enabling the writer by default exposed request-loop turn cancellation under the test
harness. Therefore the agreed target default of `provenance.agentic.providers: [jsonl]`
must not be
implemented as a blind configuration flip. The turn-lifecycle issue must be repaired and
the default-on JSONL path must pass the existing semantic-event and turn-completion gates.

### 3.4 Artifact provenance is an ARC-derived graph

Artifact records are not a separate authoritative database. The in-memory
`ArtifactRegistry` folds these semantic event types:

- `artifact.created`
- `artifact.version.added`
- `artifact.alias.moved`
- `artifact.transform.recorded`
- `artifact.used`
- `artifact.enriched`

The fold is idempotent by event id and artifact-version identity. Same-content replays are
no-ops; conflicting digests keep the first record and create a typed fold conflict.

The lineage graph combines:

- immutable `ArtifactVersion` nodes;
- `revision_of` version relationships;
- `TransformRecord` activity nodes;
- `used` and `generated` `ProvEdge` relationships;
- evidence, mechanism, custody, agent, environment, and replay metadata; and
- explicit gap nodes where CLIO knows that a version exists but cannot attribute it.

`src/clio_agent/gact/artifacts/lineage.py` builds this graph from the registry and transform
records. CAS is not the graph source. CAS supplies bytes and custody.

### 3.5 Other traces are supporting mechanisms, not competing provenance stores

Several mechanisms contain trace-like data but should not be promoted into alternative
authorities:

- `RunTrace` is a per-request in-memory structure for route, tool, and expert-handoff
  observations used by the harness and ARC.
- `runtime.trace` is diagnostic logging with event/routing/high-frequency verbosity.
- stream audit records timing and delivery diagnostics.
- SSE is a live, redacted UI projection with a deliberately restricted event subset.
- hook delivery is an observability integration surface.

These can contribute observations to `SemanticEvent`, but the semantic-event highway is
the normalized provenance boundary.

## 4. Target provenance stream and provider architecture

### 4.1 One parent stream and one overlapping substream

The existing ARC highway is the agentic-provenance stream. It is not divided into
mutually exclusive domains. Every configured agentic provider receives the full stream by
default. A selector derives the artifact-provenance substream from those same events and
delivers it to the selected artifact provider.

```text
ARC semantic-event highway
|
+-- agentic provider set receives all events
|   +-- jsonl (default)
|   +-- clio
|   `-- flowcept
|
`-- artifact selector derives an overlapping substream
    `-- artifact provider: native | cmf
```

`artifact.created` therefore appears once on the ARC highway, is visible to the agentic
providers, and is also selected for artifact provenance. This is overlapping routing, not
duplicate emission.

The default artifact selector should use the event constants already owned by the artifact
package rather than introduce a second `EventDomain` classification. Its initial set is:

```text
artifact.created
artifact.version.added
artifact.alias.moved
artifact.used
artifact.transform.recorded
```

The selector must be easy to extend with exact event types or documented patterns. It may
later admit document, dataset, or other artifact-bearing events that do not use the
`artifact.*` prefix. Conversely, a bare `artifact.*` match is too broad because CAS GC and
policy-violation events are operational facts, not necessarily artifact lineage.

### 4.2 Proposed package boundaries

The provider contracts should reflect the stream hierarchy rather than force Flowcept and
CMF behind one flat interface:

```text
src/clio_agent/gact/provenance/
|-- protocol.py       # AgenticProvenanceProvider, health, receipts
|-- dispatcher.py     # bounded delivery of the parent stream
|-- factory.py        # configured provider set, lazy imports, compatibility
|-- jsonl.py          # native default provider
|-- clio.py           # queryable CLIO memory/disk provider
`-- flowcept.py       # Flowcept mapping, filtering, and transport

src/clio_agent/gact/artifacts/provenance/
|-- protocol.py       # ArtifactProvenanceProvider contract
|-- selector.py       # extensible selection from the parent stream
|-- factory.py        # native | cmf
|-- native.py         # current CLIO graph/version implementation
`-- cmf.py            # CMF mapping plus provider-specific storage coordination
```

The exact file count may change, but Flowcept-specific logic belongs in the agentic
provider and CMF-specific logic belongs in the artifact provider. Neither package is
imported unless selected.

### 4.3 Agentic provider protocol and internal filtering

Conceptually:

```python
class AgenticProvenanceProvider(Protocol):
    name: str

    def record(self, event: SemanticEvent) -> ProviderReceipt: ...
    def health(self) -> ProviderHealth: ...
    def flush(self, timeout_s: float | None = None) -> None: ...
    def close(self) -> None: ...
```

The central dispatcher does not contain Flowcept-specific event policy. It offers the full
parent stream to every configured agentic provider. The provider may then map, filter,
sample, or reduce it. For example:

```python
if event.event_type in {"lm.token.delta", "provider.thinking.redacted"}:
    return ProviderReceipt.filtered(reason="flowcept_high_volume_event")
```

A deliberate filter is a successful, observable disposition, not a dropped delivery or an
error. This keeps future changes local: Flowcept can adjust which task, model, telemetry,
or artifact-related events it represents without changing ARC or the shared dispatcher.

### 4.4 Artifact provider protocol

The artifact contract combines specialized provenance operations with access to the
storage selected inside that provider:

```python
class ArtifactProvenanceProvider(Protocol):
    name: str

    def record(self, event: SemanticEvent) -> ProviderReceipt: ...
    def lineage(self, artifact_id: str, *, direction: str, depth: int) -> LineageGraph: ...
    def store(self, source: BinaryIO, identity: ArtifactIdentity) -> StorageReceipt: ...
    def open(self, receipt: StorageReceipt) -> BinaryIO: ...
    def verify(self, receipt: StorageReceipt) -> VerificationResult: ...
```

This is an interface sketch, not committed code. The final contract must also cover
version lookup, use/transform records, listing, export, reproduction, garbage collection,
and provider-native identifiers without weakening CLIO correlation.

### 4.5 Shared delivery semantics

Both provider contracts require:

- **ARC correlation:** target records carry the ARC event id and mapping version.
- **Idempotency:** retry identity derives from ARC event id plus provider/mapping version.
- **Ordering:** preserve per-session order where supported and disclose weaker ordering.
- **No semantic promotion:** provider-generated hashes, ids, and timestamps are additional
  evidence, not permission to rewrite the event already recorded by ARC.
- **Typed mapping loss:** unsupported fields are namespaced or reported, never silently
  dropped.
- **Bounded work:** remote I/O and queues cannot block turns indefinitely or grow without
  limit.
- **Observable filtering and failure:** accepted, filtered, retried, and failed are distinct
  receipt dispositions.
- **Explicit data policy:** provider projections must not leak secrets merely because a
  target accepts arbitrary properties.

### 4.6 Configuration

The target configuration reflects the two contracts:

```yaml
provenance:
  agentic:
    providers: [jsonl]

    jsonl:
      path: null

    clio:
      pool: null

    flowcept:
      failure_policy: best_effort
      exclude: []

  artifacts:
    provider: native
    include_events:
      - artifact.created
      - artifact.version.added
      - artifact.alias.moved
      - artifact.used
      - artifact.transform.recorded

    native:
      storage: file          # file | clio_core

    cmf:
      metadata_store: sqlite # sqlite | postgres/server
      artifact_store: local  # local | minio | s3 | ssh | osdf
```

Rules:

- `agentic.providers: [jsonl]` is the intended default after the current lifecycle gate
  is fixed.
- `artifacts.provider: native` is the default.
- Flowcept and CMF are absent by default.
- Listing/selecting a provider is sufficient to enable it; separate `enabled` flags must
  not create contradictory states.
- Existing `trace.backend=file` translates to the JSONL agentic provider for compatibility.
- Existing `trace.backend=factory` may translate to a legacy provider during migration.

## 5. Artifact identity and provider-scoped storage

### 5.1 Storage belongs below the artifact provider

Artifact storage is not selected by the agentic provenance dispatcher and is not one
global list beside JSONL, Flowcept, and CMF. It is a child capability of the selected
`ArtifactProvenanceProvider`:

```text
ArtifactProvenanceProvider
+-- native
|   `-- storage: file | clio_core
`-- cmf
    +-- metadata: SQLite | PostgreSQL/server
    `-- artifacts: DVC local | MinIO | S3 | SSH | OSDF/other supported remotes
```

The native provider owns CLIO's current version graph, transform records, custody
semantics, and byte access. Its filesystem implementation is today's CLIO CAS; clio-core
is the alternative tiered storage implementation.

The CMF provider owns the CMF mapping and delegates storage through CMF's supported
interfaces. CMF actually has two storage concerns that must remain named separately:

- MLMD lineage metadata in a local SQLite file or PostgreSQL/server deployment;
- artifact bytes in DVC's local cache or a configured remote.

The common provider contract lets the CLIO UI, export, preview, download, verification,
and reproduction work without knowing which provider-specific storage layout is active.
Today those paths primarily understand CLIO CAS and workspace paths, so selecting CMF
before re-sourcing those reads through the provider would leave holes.

### 5.2 Custody, references, and mirrors

The default is one provider and one expected primary copy. A workspace reference is not a
storage provider: it is a non-custodial artifact state in which CLIO records the locator
and available identity evidence without ingesting the bytes.

Mirroring may be added explicitly inside an artifact provider when the user asks for
redundancy, offline operation, migration safety, or different retention domains. Permanent
duplication is not a correctness requirement and must never happen merely because two
provenance projections are enabled.

Every storage implementation must stream rather than require a whole artifact in memory.
It must return an algorithm-qualified `StorageReceipt`; garbage collection and deletion
need explicit provider policy because immutable version records may outlive local custody.

### 5.3 Current size policy

The current defaults are configurable in `src/clio_agent/config.defaults.yaml`:

| Artifact size | Identity behavior | Custody behavior |
| --- | --- | --- |
| Up to 16 MiB | Stream SHA-256 | Ingest into CLIO CAS |
| Over 16 MiB through 64 MiB | Stream SHA-256 | Workspace-referenced |
| Over 64 MiB | Size and mtime only | Stat-pinned workspace reference |

The relevant settings are:

```yaml
artifacts.cas_budget_bytes: 536870912
artifacts.cas_max_file_bytes: 16777216
artifacts.hash_max_file_bytes: 67108864
artifacts.hash_stat_cache: false
```

The thresholds are not actually immutable hardcoded policy: configuration resolution
already exists. The design problem is the meaning of the 64 MiB ceiling. It currently
means "do not synchronously read the whole file," but also leaves the version without a
content hash.

A better policy vocabulary would be:

```yaml
artifacts:
  identity:
    max_sync_hash_bytes: 67108864
    large_file_policy: async_ingest   # async_ingest | authority_checksum | stat_only
```

Then 64 MiB means "do not block the turn," not "never establish content identity."

An asynchronous lifecycle may require new semantic events such as:

```text
artifact.discovered
artifact.ingest.started
artifact.identity.resolved
artifact.ingest.failed
```

Until that schema exists, stat-pinned identity must remain honestly weaker. A downstream
CMF/DVC digest is additional evidence and should not silently replace the CLIO version's
recorded identity.

### 5.4 IOWarp Core/CTE for large artifacts

IOWarp Core's Context Transfer Engine supports multiple storage targets, block/page-level
placement, offsets, and asynchronous blob operations. That makes it a promising large-file
artifact store or ingest/tiering layer.

It should be implemented as a new `ClioCoreArtifactStore`, not by reusing ARC's current
`ClioCoreStore`:

- ARC records and artifact bytes need separate namespaces, pools, and budgets.
- An artifact ingest should stream chunks to CTE while computing CLIO SHA-256 in the same
  pass.
- The receipt should record the CTE tag/blob identity and CLIO digest.
- Large artifacts must not evict or starve live context memory.
- CTE currently provides blob storage and placement, not an observed native content-hash
  receipt. CLIO still owns its SHA-256 identity unless CTE adds such a contract.
- Current CLIO documentation says cross-restart recovery of CTE's file tier is still work
  in progress; guaranteed archival durability must be proven before CTE becomes the sole
  long-term store.

## 6. CMF analysis and final design

### 6.1 What `cmflib` is

`cmflib` is CMF's primary Python authoring and client library, not a minor helper. It owns:

- pipeline, stage/context, and execution creation;
- dataset, model, metrics, and dataslice logging;
- input/output events and attributions;
- local MLMD SQLite and server-side PostgreSQL access;
- DVC and Git operations;
- metadata query and graph projection;
- artifact push/pull and server federation.

Avoiding `cmflib` entirely would mean reimplementing a substantial portion of CMF. The
integration should prefer a supported CMF API or a safe library seam, not mimic CMF's
private storage formats.

### 6.2 Native semantic mapping

CMF has useful native lineage semantics. CLIO should use them before falling back to custom
properties.

| CLIO concept | CMF projection | Fidelity |
| --- | --- | --- |
| Workspace / provenance domain | Pipeline plus stable CLIO identifiers | Native container plus properties |
| Agent stage or relevant context | CMF Context / pipeline stage | Native when the stage mapping is meaningful |
| `TransformRecord` | Execution | Native |
| `ProvEdge(role=used)` | MLMD `Event.INPUT` | Native |
| `ProvEdge(role=generated)` | MLMD `Event.OUTPUT` | Native |
| `ArtifactVersion` | Dataset/model/metrics/general artifact | Native artifact plus CLIO properties |
| Tool/instrument and bounded arguments | Execution properties | Mostly native properties |
| Environment identity | Execution properties | Mostly native properties |
| `revision_of` and aliases | Version/custom relationship properties | No exact universal CMF equivalent established |
| Evidence class and mechanism | Namespaced CLIO properties | CLIO-specific |
| Custody and custody gap | Namespaced CLIO properties | CLIO-specific |
| Replay contract | Namespaced CLIO properties | CLIO-specific |
| ARC event/session/turn/span ids | Namespaced correlation properties | Lossless correlation |
| Agent/expert identity | Context/execution properties or namespaced properties | Mapping depends on CMF ontology choice |

Custom properties are for residual CLIO semantics and lossless round trips, not an excuse
to flatten native CMF artifacts, executions, contexts, and input/output edges into opaque
JSON.

### 6.3 CMF graph and CLIO visualization

CMF has two graph-related facilities:

1. Standard lineage derived from MLMD artifacts, executions, and input/output events. Its
   server UI provides artifact, execution, and artifact-execution trees.
2. An optional Neo4j graph layer activated with `graph=True`.

Neither CMF UI is required for the genesis goal. The desired product surface is the
`clio-agent` UI. Today it renders the native provider's `ArtifactVersion` and
`TransformRecord` projection; the target UI must instead query the common
`ArtifactProvenanceProvider` interface so a selected CMF provider can return equivalent
lineage without making CMF's web UI part of CLIO.

If importing provenance authored outside CLIO becomes a goal, it should be a separate
`CMFLineageReader` capability. Possible inputs are:

- a local MLMD SQLite file;
- a supported CMF export;
- a CMF HTTP query API.

Only the HTTP option implies a CMF server. A local CMF provider can read its local MLMD
store directly through the provider contract.

### 6.4 CMF transport choices

The stable internal design is:

```text
ARC agentic stream
    -> artifact selector
         -> artifact-provenance substream
              -> CMFArtifactProvenanceProvider
                   +-> metadata: local cmflib/MLMD | CMF HTTP/server
                   `-> artifacts: DVC local | configured remote
```

The artifact factory selects `CMFArtifactProvenanceProvider`; metadata transport and DVC
storage remain internal CMF-provider concerns.

#### Local library transport

This uses the real CMF object model and MLMD writer, but the current `Cmf` constructor:

- changes the process working directory;
- performs prechecks;
- opens SQLite in normal client mode;
- checks out a Git branch; and
- optionally opens Neo4j.

Those process-global side effects are unsafe inside an automatic artifact provider. A
good HPE contribution would be a side-effect-free writer mode that does not change cwd or
run Git/DVC unless artifact custody is explicitly requested.

#### HTTP transport

HTTP provides dependency and process isolation. The current CMF server exposes
`POST /mlmd_push`, but it accepts a batch/federation JSON representation and feeds the CMF
merger. It is not clearly a stable per-semantic-event ingestion API.

Before relying on it, ask HPE to do one of the following:

1. confirm and version `/mlmd_push` as a supported integration contract;
2. add an official pipeline/context/execution/artifact/event ingestion API; or
3. provide a supported serializer that produces the accepted batch schema.

Generating CMF's complete merger JSON directly from CLIO without a supported schema would
be fragile.

#### Subprocess transport

The implemented first slice uses a configured CMF-compatible Python subprocess. It keeps
`cmflib`/MLMD out of CLIO's Python 3.12 dependency graph and avoids the process-global
`Cmf` client lifecycle. The worker uses CMF's SQLite store and metadata helpers to create
native MLMD contexts, executions, artifacts, and input/output events. This is a small,
reviewable compatibility seam, not a claim that CMF's current lower-level helpers are a
versioned third-party API. If CMF supports Python 3.12 and offers a side-effect-free writer,
the subprocess can collapse back into an ordinary lazy import.

### 6.5 Python compatibility

At the inspected snapshot:

- `clio-agent` targets Python 3.12.
- CMF declares `requires-python = ">=3.9,<3.12"`.
- CMF pins `ml-metadata==1.15.0` along with DVC, Ray, Neo4j, protobuf, and other substantial
  dependencies.

A Python 3.12 `uv pip install --dry-run` was unsatisfiable because ML Metadata 1.15.0 has no
compatible CPython 3.12 wheel. Current ML Metadata releases also have platform-wheel
constraints that require qualification. In contrast, all 70 inspected `cmflib` Python
files compiled as Python 3.12 source without syntax failures.

The correct conclusion is limited:

- CMF source is not obviously Python-language-incompatible with 3.12.
- CMF's declared range and compiled dependency pins currently block an ordinary 3.12
  installation.
- HPE may be able to resolve this by upgrading MLMD/dependencies and running the real CMF
  suite on Python 3.12.
- Source compilation is not runtime compatibility evidence.

CMF must remain an optional extra or transport integration so none of these dependencies
enter the default CLIO environment.

### 6.6 CMF hashing and artifact custody

CMF normally calls `dvc add` or `dvc import-url`, derives the DVC object hash, stages the
`.dvc` file in Git, and links the resulting artifact to an execution. CMF therefore detects
artifact changes when CMF/DVC logging is invoked; it does not continuously watch arbitrary
workspace files.

CMF also provides `log_dataset_with_version`, which accepts a caller-supplied version/hash
and skips `commit_output`. That is useful for metadata-only projection, but it does not copy
or archive the bytes.

Digest ownership must be explicit:

- CLIO SHA-256 remains the canonical CLIO identity when CLIO performed the read.
- A CMF/DVC store may compute its native digest when it takes custody.
- Never label CLIO SHA-256 as CMF's native DVC MD5.
- Store a crosswalk receipt with algorithm-qualified digests, for example:

```json
{
  "clio": {"algorithm": "sha256", "digest": "..."},
  "cmf_dvc": {"algorithm": "md5", "digest": "...", "uri": "..."}
}
```

The CMF artifact provider may be configured for metadata plus referenced external custody,
or for metadata plus CMF/DVC custody. In the latter mode its DVC backend owns retrieval and
CLIO's export/reproduction paths must go through the common artifact-provider interface.

### 6.7 CMF artifact storage does not imply a CMF server

CMF delegates artifact storage to DVC. Its local backend is a filesystem
content-addressed store with paths such as:

```text
<repository>/files/md5/<first-two>/<remainder>
```

Therefore:

- the first `local` storage slice writes and verifies the DVC MD5 object layout directly,
  without invoking DVC or changing Git state in the CLIO workspace;
- this is a DVC-compatible local object store, but HPE should confirm the supported ingest
  seam before it is described as a general CMF/DVC repository integration;
- a CMF SSH remote requires an SSH-accessible filesystem, not the CMF metadata server;
- a CMF S3/MinIO remote requires object storage;
- a hosted object store may provide external durability;
- CMF server/PostgreSQL store collaborative metadata, not the DVC artifact bytes.

This is CAS behavior, but not automatically a hardened archive. Durability, redundancy,
access control, and backup quality come from the selected DVC backend.

### 6.8 CMF infrastructure decision

The final scope decisions are:

- Shared CMF metadata is not a goal.
- CMF's web lineage UI is not required; lineage belongs in the CLIO UI.
- CMF's MCP server is not required; Spotter-AI can expose CLIO provenance query tools.
- CMF artifact custody is optional and can use a local DVC CAS without a service.

Consequently, the initial CMF integration requires **no homelab infrastructure**.

For completeness, a read-only homelab inspection on 2026-08-21 found Docker 29.2.0,
Compose 5.0.2, 48 GiB free disk, about 3.3 GiB available RAM, all 9 GiB swap occupied, and
65 active containers. The stock CMF server Compose stack would add PostgreSQL, CMF server,
UI, TensorBoard, MCP, and Nginx. It has no resource limits; builds a TensorBoard image from
`tensorflow/tensorflow:latest`; exposes an API with permissive CORS and no observed
application authentication; and maps port 443 although its supplied Nginx configuration
listens only on HTTP port 80. The example MinIO stack uses an old 2021 image and default
credentials. If collaborative CMF services are ever requested, use a lean, pinned,
authenticated deployment rather than the examples unchanged.

## 7. Spotter-AI MCP and the CLIO UI

Agent-side provenance queries should operate over the configured agentic and artifact
provider interfaces. A Spotter-AI MCP integration can expose tools such as:

- `provenance_get_lineage`
- `provenance_find_artifacts`
- `provenance_get_execution`
- `provenance_explain_derivation`
- `provenance_get_provider_receipts`
- `provenance_verify_artifact`

This avoids requiring CMF's MCP service and gives every agent a consistent CLIO interface
regardless of which optional providers are selected.

The CLIO UI should likewise render through those provider interfaces:

- artifact, activity, revision, used, generated, and gap nodes/edges from ARC;
- identity and custody evidence;
- provider projection status and external ids as annotations; and
- storage availability and verification receipts.

It must not depend on CMF's own UI. When CMF is the selected artifact provider, CLIO may
query it through `ArtifactProvenanceProvider`; when native is selected, no CMF query is
needed.

## 8. Flowcept analysis and final role

The initial decorator concern was too narrow. At the inspected snapshot Flowcept supports:

- non-intrusive observability adapters for external services and frameworks;
- direct decorators and context managers;
- explicit programmatic `FlowceptTask` creation and `send()`;
- a common message schema, buffering, MQ publication, and custom consumers;
- workflow, task, agent, telemetry, used/generated, and object records;
- offline JSONL operation as well as LMDB or MongoDB-backed persistence; and
- a read-only REST API, UI, and MCP/query layer over persisted provenance.

CLIO therefore does not need to decorate individual functions. Its Flowcept provider can
receive the already-emitted ARC semantic events at coarse boundaries and create explicit
Flowcept workflow/task records.

Flowcept implements the **agentic provenance provider** contract. It receives the parent
stream, then applies Flowcept-specific policy internally:

```text
ARC agentic-provenance stream
    -> FlowceptAgenticProvenanceProvider
         -> filter / sample / project
              -> explicit Flowcept workflow/task messages
                   -> offline buffer/JSONL or MQ/consumer/database
```

For example, Flowcept may filter `lm.token.delta`, thinking/redaction bookkeeping,
high-volume ARC operations, or other events that do not warrant a Flowcept task. Those
choices must stay inside `flowcept.py`, be configurable, and return a `filtered` receipt
rather than look like unexplained loss.

Artifact events remain part of the parent stream, so Flowcept may represent useful
artifact creation or transform summaries as task `used`/`generated` references. That does
not make Flowcept the artifact-provenance provider and does not give it custody of CLIO
artifact bytes. Flowcept's own MongoDB/GridFS blob facility is internal Flowcept
functionality, not a proposed CLIO artifact-storage backend in this design.

Flowcept and CMF are consequently not sisters behind one adapter protocol and neither is a
child of the other:

- Flowcept consumes the parent agentic-provenance stream.
- CMF implements the specialized artifact-provenance provider over the derived substream.
- A Flowcept-to-CMF consumer could be a separate ecosystem interoperability project, but
  CLIO-originated events should not traverse Flowcept in order to reach CMF.

### 8.1 Reassessment of the Flowcept integration concerns

Flowcept already owns the capture lifecycle and the path from captured records to its
configured buffer, MQ, consumer, and database. CLIO does not need to select or implement any
of those internal routes. Its responsibility is limited to:

1. load Flowcept only when the optional provider is enabled;
2. configure Flowcept using the selected Flowcept profile/settings;
3. map and filter ARC events into Flowcept's documented workflow, agent, and task semantics;
4. hand those records to Flowcept through its public capture API; and
5. start, flush, and stop Flowcept according to its documented lifecycle.

CLIO must not call MongoDB, Redis, an MQ client, a Flowcept consumer, or a Flowcept DAO
directly. Earlier exploration of direct database and interceptor methods established how
Flowcept works internally, but those are not separate CLIO architecture choices.

Flowcept's use of active workflow/campaign state is consequently a validation concern rather
than a request for a new Flowcept publication contract. The initial gold target is one local
agent/process. Later concurrent and distributed tests should follow Flowcept's documented
practice of supplying explicit workflow and campaign identifiers across producers.

CLIO also does not need to wrap its functions with `agent_flowcept_task`: ARC has already
observed the execution and established its outcome. The provider hands the resulting semantic
record to Flowcept without allowing provenance capture to change the agent/tool result.

### 8.2 Flowcept is configured, not reimplemented

The `flowcept.py` integration is a thin optional bridge from ARC semantics to Flowcept
semantics. Storage and query behavior are selected using ordinary Flowcept configuration.
Changing from offline JSONL to the `full-online` profile must not require a different CLIO
mapper or direct knowledge of Flowcept's Redis/MongoDB implementation.

### 8.3 Content and privacy projection

Flowcept receives the parent agentic-provenance stream, but that does not imply that every
event payload is exported verbatim. Prompt text, model responses, tool arguments/results,
reasoning or thinking content, environment data, and file content can contain secrets,
personal data, unpublished research, or simply high-volume noise.

The Flowcept provider therefore needs an event export controller applied before constructing
Flowcept records. It uses the existing ARC event types to decide whether an event is included
or filtered, followed by field-level projection for included events. It does not require a
new event-domain taxonomy.

The content modes below are useful presets for that controller rather than three hard-coded
provider implementations:

- **metadata** (recommended default): identifiers, relationships, event types, timings,
  status, model/tool names, counts, hashes, and artifact references, but no raw prompt,
  response, reasoning, or tool payload content;
- **redacted semantic content**: bounded prompt/response/tool fields after CLIO secret and
  policy redaction, with truncation and omission recorded; and
- **full permitted content**: explicit opt-in for the maximum content allowed by CLIO's
  policy, still subject to mandatory secret handling and size limits.

Thinking/token streams and other especially sensitive or high-volume event classes may be
discarded by `FlowceptAgenticProvenanceProvider` regardless of whether ARC retained them.
Filtering, redaction, truncation, and successful publication must produce distinguishable
receipts. CLIO must perform this projection itself; it should not assume that downstream
Flowcept storage will retroactively enforce CLIO's export policy.

### 8.4 Flowcept deployment profiles and the initial gold target

The CLIO boundary ends after `flowcept.py` maps an ARC event and submits the resulting record
through Flowcept's supported ingestion path. From that point onward, **Flowcept decides where
the provenance is buffered, transported, persisted, and queried**. JSONL, MQ, LMDB, and
MongoDB are Flowcept deployment choices; they are not new CLIO provider protocols or peer
backends in CLIO's dispatcher.

The practical Flowcept support profiles are:

| Profile | Flowcept destination | Query semantics | CLIO support priority |
|---|---|---|---|
| Local capture | Offline JSONL | Load/consolidate later; no live rich queries | Supported fallback |
| Lightweight local persistence | MQ plus LMDB | Basic/programmatic access; limited rich querying | Secondary |
| **Online query profile** | **Flowcept `full-online`: Redis MQ/KV plus MongoDB** | **Live and historical Flowcept queries, UI, workflow cards, and agent chat** | **Initial gold deployment** |
| Distributed | Shared Redis, RabbitMQ, Kafka, or Mofka plus a persistence consumer and normally MongoDB | Consolidated cross-process/node queries | Later scale profile |

The initial deployment is deliberately the third row because the acceptance target requires
live, persistent Flowcept queries. The first workload used to validate that deployment is one
local CLIO agent/process. The infrastructure choice follows the query requirement, not the
number of agents. Its path is:

```text
ARC semantic events
    -> CLIO Flowcept mapper/filter
         -> supported Flowcept message ingress
              -> Redis MQ/KV
                   -> Flowcept persistence consumer
                        -> MongoDB
                             -> Flowcept query/UI surfaces
```

This profile exercises the useful Flowcept semantics rather than merely proving that a JSONL
file can be written. The first mapping goldens should cover one CLIO agent, one session/run,
its model and tool activity, parent/child relationships, status/timing, and selected artifact
references. JSONL remains valuable for zero-infrastructure and disconnected operation, but it
is not the primary acceptance environment because it does not provide the target live query
experience.

Flowcept's multi-process and multi-node design remains important, but it does not require a
different CLIO mapping. Later distributed validation can run the same provider on several
producers, carry explicit shared `workflow_id`/`campaign_id` values, and select the appropriate
Flowcept MQ/storage deployment. It is a scale-out profile after the single-agent mapping and
query behavior are correct.

For this gold profile, MongoDB is the provenance **storage and query database**. Redis is not a
second provenance archive: Flowcept uses it as the message queue and key/value coordination
layer between capture and persistence. The official `full-online` profile enables Redis MQ,
Redis KV, MongoDB, and online flushing, so both services are part of that Flowcept profile and
serve different purposes. Redis can later be replaced by another supported MQ for a different
deployment; MongoDB is selected here because Flowcept documents it for rich online and
historical queries.

For a local or homelab deployment, Flowcept already provides
`deployment/compose-mongo.yml`, which starts the Redis and MongoDB dependencies with a
persistent MongoDB volume. Its `make services-mongo` target is the common development path.
If the Flowcept webservice/UI itself should also run on the homelab, Flowcept separately
provides `deployment/compose-service.yml`. CLIO only needs the corresponding Flowcept
configuration and service endpoints.

The initial dependency stack was deployed on the `homelab` SSH host under
`/home/jcernuda/compose/flowcept/docker-compose.yml`, where Dockge can manage it. It runs
`redis:7-alpine` and `mongo:8.0`; MongoDB data is held in the named
`flowcept_flowcept_mongo_data` volume. Both services bind only to homelab loopback rather than
the LAN because this exploratory stack does not enable Redis or MongoDB authentication. A
CLIO development process reaches them through an SSH tunnel, for example:

```text
ssh -N \
  -L 6379:127.0.0.1:6379 \
  -L 27017:127.0.0.1:27017 \
  homelab
```

The deployed stack contains only Flowcept's Redis/MongoDB dependencies. A Flowcept service,
web UI, or CLIO integration process must still be configured separately.

There is no remaining CLIO storage-routing or publication-facade question. CLIO configures
Flowcept's `full-online` profile and hands it mapped events through Flowcept's capture runtime.
Flowcept manages the buffer, Redis publication, persistence consumer, MongoDB writes, and
query surfaces. The first single-agent mapping, event export controller, optional dependency,
and provider-neutral query surface are implemented and were proved against that profile.

## 9. Decision ledger

### 9.1 Accepted

- ARC is mandatory and first in the write order.
- The ARC semantic-event highway is the parent agentic-provenance stream.
- Artifact provenance is an overlapping, extensible substream selected from that parent;
  it is not a second source or a hard mutually exclusive branch.
- Agentic and artifact provenance require different provider contracts.
- JSONL is the intended default agentic provider; a queryable CLIO memory/disk pool and
  Flowcept are additional agentic providers.
- Agentic providers receive the full parent stream by default. Provider-specific filtering,
  sampling, and mapping happen inside the provider and produce typed dispositions.
- The existing event types and artifact-owned constants are sufficient for substream
  selection; no new `EventDomain` enum is required.
- Native CLIO and CMF are artifact-provenance providers.
- Artifact storage is provider-scoped: native supports filesystem/clio-core; CMF supports
  its MLMD metadata stores and DVC local/remotes.
- CMF and Flowcept use lazy imports and optional dependencies/transports.
- One selected artifact provider and one expected primary copy are the default; mirrors
  are explicit.
- CLIO owns CLIO artifact version history and algorithm-qualified identity evidence.
- Provider-native hashes are additional evidence recorded in storage receipts.
- CLIO UI and Spotter-AI MCP query common provider interfaces rather than provider-specific
  UIs or storage layouts.
- CMF native entities/edges should be used where they fit; namespaced properties preserve
  residual CLIO semantics.
- CMF can be a local DVC-backed artifact store without CMF server infrastructure.
- Flowcept supports explicit programmatic tasks; CLIO does not need pervasive decorators.
- After CLIO submits mapped records through Flowcept's supported ingress, Flowcept owns the
  configured JSONL/MQ/database destination.
- The initial Flowcept gold deployment is the `full-online` Redis-plus-MongoDB profile because
  it provides live persistent queries; its first acceptance workload is one local CLIO agent.
- Flowcept's MongoDB/GridFS blob API is not a CLIO artifact-storage provider in this design.
- No homelab deployment is required for the first CMF integration.

### 9.2 Rejected

- Treating ARC, JSONL, CMF, and Flowcept as interchangeable or flat parallel backends.
- Treating Flowcept and CMF as sisters behind one universal adapter protocol.
- Treating agentic and artifact provenance as disjoint event highways.
- Hiding provider-specific filters in the shared router.
- Adding a redundant `EventDomain` classification when existing event types already route.
- Leaving JSONL outside the agentic provider protocol.
- Making CMF or Flowcept a required dependency.
- Selecting artifact storage from the agentic provider configuration.
- Treating Flowcept's internal GridFS object feature as a CLIO artifact-storage backend.
- Permanently duplicating every artifact into both CLIO CAS and CMF by default.
- Allowing CMF to independently rewrite CLIO's version history.
- Presenting CLIO SHA-256 as a CMF/DVC MD5.
- Requiring CMF's UI, Neo4j, MCP server, PostgreSQL, or metadata server for CLIO lineage.
- Making a Python 3.11 bridge the primary long-term CMF design.
- Deploying the stock CMF or MinIO examples to the homelab unchanged.
- Treating a successful Python source compile as CMF Python 3.12 compatibility proof.

### 9.3 Deferred

- Importing externally-authored CMF lineage into CLIO.
- A CMF server deployment for team collaboration.
- CMF/MinIO or another remote object store on the homelab.
- CTE as the sole durable artifact archive until cross-restart recovery is proven.
- Exact provider queue/spool overflow policy.
- Exact receipt persistence representation and recursion guard.
- The final large-file asynchronous identity event schema.
- Whether agentic provenance supports multiple simultaneous non-JSONL providers by default
  or requires explicit fan-out policy.

## 10. Implementation sequence and status

This sequence minimizes semantic risk and preserves optionality. Steps 1-7 and the
provider-neutral read surfaces in step 9 are implemented in the first slice. The native
`clio_core` artifact store, CMF remotes, and broader distributed validation remain follow-up
work.

1. **Freeze stream semantics.** Test ARC-first ordering, the full parent-stream shape,
   existing artifact event constants/fold inputs, and JSONL compatibility.
2. **Extract the agentic provider contract.** Move JSONL behind it without changing bytes;
   translate the old trace configuration and repair the lifecycle gate before default-on.
3. **Add the agentic dispatcher and factory.** Deliver the full stream to configured
   providers with bounded queues, health, receipts, and explicit filtered dispositions.
4. **Add the artifact selector.** Derive the overlapping artifact substream from existing
   event types and prove an artifact event is emitted once while reaching both projections.
5. **Extract the native artifact provider.** Put the current graph, CAS reads, export,
   preview/download, verification, reproduction, and GC behind the common interface;
   support filesystem and clio-core storage implementations.
6. **Add the Flowcept agentic provider.** Use explicit Flowcept tasks, keep filtering and
   transport local to `flowcept.py`, and require no Flowcept dependency unless selected.
7. **Add the CMF artifact provider.** Golden-test executions, input/output artifacts,
   version/custody properties, and provider queries without requiring `cmflib` in the base
   environment.
8. **Select CMF metadata and storage seams with HPE.** Prefer a side-effect-free Python or
   supported HTTP interface; begin with local MLMD plus local DVC, then add explicit remotes.
9. **Expose provider evidence.** Add CLIO API/UI and Spotter-AI MCP surfaces over the common
   agentic/artifact provider contracts and their receipts.

## 11. Acceptance criteria

### 11.1 Optionality

- A normal CLIO installation imports neither CMF nor Flowcept.
- CLIO starts and runs with only JSONL configured.
- Missing optional packages produce a typed configuration/health result only when their
  provider is selected.
- Packaging keeps provider dependencies in optional extras.

### 11.2 Provenance correctness

- ARC records before any provider receives the event.
- Every provider record correlates to the ARC event id.
- JSONL remains byte/schema compatible through extraction.
- Per-session ordering and retry/idempotency behavior are tested.
- Mapping loss and downstream failures are typed and queryable.
- A deliberate provider filter is distinguishable from delivery failure.
- Provider failures cannot silently erase or mutate ARC provenance.
- Artifact events remain in the parent agentic stream and are selected into the artifact
  substream without duplicate emission.

### 11.3 Artifact correctness

- Exactly one artifact provider owns expected retrieval unless mirrors are configured.
- Store receipts use algorithm-qualified digest names.
- Export and reproduction work through every selected artifact provider and its supported
  storage modes.
- Large inputs stream with bounded memory.
- A primary-store failure cannot be mislabeled as successful custody.
- Stat-pinned identity remains visibly weaker until asynchronously resolved.
- Store-native verification never silently rewrites an existing artifact version.

### 11.4 CMF-specific

- `TransformRecord`, used/generated edges, and artifact versions map to native CMF
  executions, input/output events, and artifacts.
- CLIO-specific evidence, custody, replay, agent, revision, and ARC correlation data survive
  in namespaced properties where CMF lacks an exact equivalent.
- The CMF provider does not change cwd or Git branches in the CLIO process.
- CMF metadata plus referenced custody does not invoke DVC or copy bytes.
- CMF/DVC custody returns a DVC-native digest plus CLIO identity crosswalk.
- No CMF service is required for local MLMD or local DVC modes.

### 11.5 Flowcept-specific

- No CLIO function requires a Flowcept decorator.
- The provider hands mapped workflow/agent/task records to Flowcept through its public capture
  API and lets Flowcept manage buffering, transport, persistence, and querying.
- Flowcept-specific filtering is local, configurable, and reported as filtered rather than
  failed or silently dropped.
- The `full-online` Redis-plus-MongoDB gold deployment proves mapped records from one local
  CLIO agent are queryable through Flowcept; offline JSONL is tested separately as a
  capture-only fallback.
- Distributed validation reuses the same mapping with explicit workflow/campaign identifiers
  after the single-agent query semantics are proven.
- Flowcept never takes custody of CLIO artifact bytes in the initial integration.

### 11.6 UI and MCP

- CLIO lineage renders through the selected artifact provider.
- Native and CMF providers return a normalized CLIO lineage/query shape.
- Spotter-AI can query normalized provenance without CMF's MCP server.

## 12. Questions for the HPE CMF team

1. Is `/mlmd_push` intended as a versioned third-party ingestion contract, or only as CMF
   server federation/batch synchronization?
2. Is there an official API for creating pipelines, contexts, executions, artifacts, and
   input/output events one transaction or event at a time?
3. Can `cmflib` offer a side-effect-free writer that does not change cwd, check out Git, or
   run DVC unless requested?
4. What is the Python 3.12 plan, particularly for `ml-metadata` and platform wheels?
5. Is `log_dataset_with_version` the supported metadata-only path for an externally
   established digest? Can it carry an algorithm label rather than assuming one hash
   namespace?
6. How should a non-DVC SHA-256 identity be represented alongside a DVC-native digest?
7. Does CMF have a native revision/derivation relationship beyond execution input/output
   events, or should CLIO revision edges remain custom properties/Neo4j relationships?
8. What schema/version guarantees exist for MLMD JSON export/import?
9. Which CMF UI relationships require Neo4j, and which are derived entirely from MLMD?
10. Would HPE review and help refine a small CLIO mapper/transport module and its mapping
    goldens?
11. Is direct, atomic insertion into a configured DVC object store a supported custody seam,
    or should CMF expose a side-effect-free artifact-ingest API that returns the native digest
    and object URI without staging `.dvc` files in the caller's Git workspace?

## 13. Source evidence index

### 13.1 CLIO

- ARC-first highway wiring: `src/clio_agent/arc/memory.py`,
  `src/clio_agent/gact/runtime/globals.py`, `src/clio_agent/gact/app.py`
- Semantic envelope, projections, JSONL and current factory:
  `src/clio_agent/gact/semantic_events.py`
- Current trace configuration: `src/clio_agent/config.defaults.yaml`,
  `docs/SEMANTIC_EXECUTION_TRACES.md`
- Artifact fold source: `src/clio_agent/gact/artifacts/registry.py`,
  `src/clio_agent/gact/artifacts/registry_boot.py`
- Artifact identity and CAS: `src/clio_agent/gact/artifacts/minting.py`,
  `src/clio_agent/gact/artifacts/cas.py`
- Version decisions and re-hash-on-use: `src/clio_agent/gact/artifacts/versions.py`,
  `src/clio_agent/gact/artifacts/transform_edges.py`
- Lineage graph: `src/clio_agent/gact/artifacts/lineage.py`
- Current byte lookup in export: `src/clio_agent/gact/artifacts/export.py`
- Current ARC CTE wrapper and durability caveats: `src/clio_agent/arc/storage.py`,
  `src/clio_agent/arc/clio_core_config.py`, `docs/ARC_MEMORY_LAYER.md`
- Existing design context: `docs/design/unified-arc-highway.md`,
  `docs/design/artifact-provenance-design.md`,
  `docs/design/artifacts-campaign-2026-07.md`

### 13.2 CMF at the inspected commit

- [Package and Python constraints](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/pyproject.toml)
- [`Cmf` constructor lifecycle](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/cmflib/cmf.py#L142-L202)
- [Ordinary dataset logging and DVC identity](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/cmflib/cmf.py#L760-L815)
- [`log_dataset_with_version`](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/cmflib/cmf_server.py#L291-L358)
- [DVC/Git operations](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/cmflib/dvc_wrapper.py#L241-L282)
- [CMF server Compose stack](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/docker-compose-server.yml)
- [Server batch ingestion endpoint](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/server/app/main.py#L270-L299)
- [Lineage UI semantics](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/docs/ui/lineage.md)
- [Local artifact backend](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/cmflib/storage_backends/local_artifacts.py)
- [Example MinIO stack](https://github.com/HewlettPackard/cmf/blob/53d9c3e518ab2fde46955f10520d4842c572bf05/examples/example-get-started/docker-compose.yml)

### 13.3 IOWarp Core at the inspected commit

- [CTE overview and APIs](https://github.com/iowarp/core/blob/a02bc8e7813b09f81b616a96283d02626ecf1c22/context-transfer-engine/docs/cte.md)
- [CTE storage-target configuration](https://github.com/iowarp/core/blob/a02bc8e7813b09f81b616a96283d02626ecf1c22/context-transfer-engine/docs/config.md)
- [Python `PutBlob`/`GetBlob` bindings](https://github.com/iowarp/core/blob/a02bc8e7813b09f81b616a96283d02626ecf1c22/context-transfer-engine/wrapper/python/core_bindings.cc#L203-L242)
- [Page-based filesystem adapter](https://github.com/iowarp/core/blob/a02bc8e7813b09f81b616a96283d02626ecf1c22/context-transfer-engine/adapter/filesystem/filesystem.h#L170-L250)

### 13.4 Flowcept at the inspected commit

- [Architecture and capture mechanisms](https://github.com/ORNL/flowcept/blob/c000b10ea49659af6c5821b61918f3893bd46a92/docs/architecture.rst)
- [Explicit `FlowceptTask`, decorators, loops, and observability adapters](https://github.com/ORNL/flowcept/blob/c000b10ea49659af6c5821b61918f3893bd46a92/docs/prov_capture.rst)
- [Message queue, JSONL, LMDB, MongoDB, and custom consumers](https://github.com/ORNL/flowcept/blob/c000b10ea49659af6c5821b61918f3893bd46a92/docs/prov_storage.rst)
- [Workflow, task, object, and PROV-AGENT schema](https://github.com/ORNL/flowcept/blob/c000b10ea49659af6c5821b61918f3893bd46a92/docs/schemas.rst)
- [Flowcept blob and version storage](https://github.com/ORNL/flowcept/blob/c000b10ea49659af6c5821b61918f3893bd46a92/docs/blob_data.rst)
- [Explicit task implementation](https://github.com/ORNL/flowcept/blob/c000b10ea49659af6c5821b61918f3893bd46a92/src/flowcept/instrumentation/task_capture.py)

## 14. Implemented slice and live verification

### 14.1 Code boundary

The first slice implements two distinct provider axes:

- agentic provenance remains the ARC-sourced parent stream, with `jsonl` enabled by
  default and Flowcept lazily enabled through `provenance.agentic.providers`;
- artifact provenance is an overlapping selected substream, with `native` as the default
  provider and `cmf` selected through `provenance.artifacts.provider`.

The artifact provider owns its store. Native currently implements the existing filesystem
SHA-256 CAS. CMF currently implements `reference` and DVC-compatible `local` custody. Selecting
CMF does not also write the CLIO CAS. Minting, pinning, download, export, and lineage use the
selected store/provider interface, while the HTTP lineage response retains CLIO's common graph
shape. The native `clio_core` store and CMF remotes remain explicit follow-up implementations.

CMF and Flowcept remain optional. The base runtime imports neither package. Flowcept is an
optional extra; CMF runs through an explicitly configured isolated interpreter because its
current dependency range is incompatible with CLIO's Python 3.12 environment.

### 14.2 Real CMF qualification

On 2026-08-21 the worker was exercised on `ssh homelab` using Python 3.9.2, real
`cmflib==0.1.0`, and real `ml-metadata==1.15.0`. A fresh SQLite/MLMD database received two
artifact records and one transform execution. Querying the output artifact returned three
nodes and the expected edges:

```text
artifact_cmf_input --used--> activity:call_cmf_live
activity:call_cmf_live --generated--> artifact_cmf_output
```

The response preserved CLIO artifact ids, SHA-256 identities, session/turn ids, tool, status,
and ARC event correlations in namespaced CMF properties. The fresh MLMD SQLite file was 245760
bytes. The two local custody objects were also present under `files/md5/<xx>/<rest>` and their
computed MD5 values matched their object names.

This qualifies the implemented mapping and local object layout against the real packages. It
does not yet qualify a CMF remote, the CMF server, or a Windows-native MLMD wheel. The current
worker uses CMF's lower-level SQLite/metadata helpers specifically to avoid the `Cmf` client's
cwd/Git/DVC side effects; that seam is intentionally isolated for HPE review.

### 14.3 Real Flowcept, NDP, and Spotter qualification

The homelab Flowcept profile ran Redis 7 and MongoDB 8 with loopback-only ports reached through
an SSH tunnel. CLIO ran from the feature worktree with JSONL plus Flowcept and used the Codex
provider with `gpt-5.6-luna`; Claude was not used.

A terminal NDP run invoked the real `ndp_search_datasets` tool and returned the EarthScope
Stations Dataset (`811f0bcc-99e5-455c-bcf6-7c63c2634f41`) and resource
`a420cc30-2262-423a-8c63-3ad8d91f2a8f`. Under `spotter-ai`, a second terminal NDP run invoked
the same live tool, then woke the standing Spotter watcher. The watcher itself ran with an
explicit session overlay selecting Codex Luna, invoked the real `spotter_campaign_health` MCP
tool, and completed. Leaving `spotter-ai` transitioned its standing task to
`status=cancelled, live_state=cancelled`, which is the intended terminal disarm state.

CLIO's provider-neutral query returned the same three terminal turns and the same three
completed tool spans from native JSONL and Flowcept for the combined parent/child session.
Direct Flowcept Mongo queries returned 103 records for the NDP parent and 54 for the Spotter
child; Flowcept reported its KV, MongoDB, and MQ services healthy, with zero CLIO dispatcher
failures or overflows.

Two failed probes remain useful negative evidence rather than acceptance results: the generic
NDP benchmark runner did not activate the EarthScope blueprint and therefore lacked the NDP
tool, and a simple built-in-main Spotter trigger produced streamed text but the blueprint
wrapper classified the answer as empty. The corrected blueprint-aware NDP plus Spotter run is
the terminal green path described above.

The first live query exposed a provider-neutral normalization defect: one-shot running token
samples were treated as open lifecycle spans, while expert and LLM start/end records use
different event names. The read model now opens spans from lifecycle event types, pairs the
expert and LLM families explicitly, and coalesces duplicate starts with the same correlation.
Replaying the combined parent/child JSONL through the corrected normalizer reports
`complete=true`, three terminal turns, and no truncation. The same normalizer is used for
Flowcept records.

### 14.4 Verification gates and boundary

The no-infrastructure provider tests passed 21/21. The broader artifact, export, semantic-
event, and session-export selection passed 360 tests; its two environment-skipped relay shape
checks were then rerun against the real sibling `clio-relay` models and both passed. Ruff passed
for every changed Python file, and Mypy reported no issues in the 16 selected provider, mint,
export, route, and event modules. The UI's focused Vitest suites passed 2 core-client and 3 web-observability tests, and
both TypeScript projects passed `tsc --noEmit`.

The repository-wide non-integration command was also attempted, but it is not recorded as
green: on managed Windows it reached roughly 65 percent and timed out after 20 minutes with
unrelated failures and environment-gated skips. One failure was reproduced independently:
`test_set_app_arc_sets_and_wires` hardcodes `/tmp/_arc_set_app_arc_test` and fails with
`WinError 5` before exercising its ARC wiring assertion. The focused gates above are the
acceptance evidence for this slice; the incomplete broad command is retained as non-green
evidence rather than being silently omitted.
