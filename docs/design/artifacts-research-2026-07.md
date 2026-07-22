# Artifacts research — prior art survey (2026-07)

Research input to the artifacts campaign (roadmap item b, after agents-creating-agents #948).
This is a survey, not a design. Four sweeps: the Claude ecosystem, other agent products,
protocol/data-engineering systems, and local prior art (clio-relay + clio-agent's current
output pathways).

## The framing under test (owner)

> Artifacts are the collection of outputs from a session — not just AI-created ones: an
> image, a downloaded dataset, etc. On the NDP case: (a) the metadata dataset, (b) the
> station dataset → cleaned → dataset v2, (c) the AI-generated image, (d) the analysis
> report. c and d align with Claude's definition; a and b go beyond. b shows evolution of
> artifacts, provenance. Ideally we reach `b = transform(a)` for full reproducibility —
> give the user the script that downloaded a, the script that cleaned b to b', the script
> that generated d. This expands to mcpui and a2ui support as artifacts.

Second owner framing (same conversation): **application execution** joins the same model —
one artifact is the configuration/Jarvis package, another is the application's output(s) or
the surrogate model trained from them. This is *why* clio-relay was built with artifacts as
a first-class citizen: a relay job IS an application execution whose spec and outputs are
both artifacts.

**Survey verdict up front:** no shipped agent product implements this framing. The pieces
exist — but in *data-engineering* systems (W&B Artifacts, DVC, OpenLineage, W3C PROV), not
in agent products. clio-relay already has the durable record shape; clio-agent's runtime
has none of it (path strings in `workflow_state` only). The gap between "what agents ship"
and "what the owner describes" is exactly where the campaign's novelty lives.

---

## 1. Claude's artifact model

### claude.ai Artifacts
- Model-authored, self-contained content (>~15 lines) rendered in a side panel. Text-renderable
  types only: markdown, code, single-page HTML, SVG, Mermaid, React. No binaries.
- Identity = an `identifier` in `<antArtifact>` tags in model output — a *model-output
  convention*, conversation-scoped, parsed client-side. **No artifact object exists in the
  Messages API.**
- Mutation: `create` / `update` (targeted string replace) / `rewrite` under the same
  identifier → in-place versions with a version picker. User edits don't enter the model's
  memory of the artifact.
- Publish pins one version to a public link; **remix = fork with no recorded ancestry**;
  unpublish is destructive and one-way.

### Claude Code Artifacts
- A published, self-contained web page: identity = the URL; source of truth = an `.html`/`.md`
  file in the project; republish → same URL, each publish is a version (with optional labels,
  choose-which-version sharing, editor roles).
- Cross-session identity is carried **by hand** (paste the URL) — without it a new session
  mints a new artifact.
- The only API-addressable artifact object in the ecosystem is the **Compliance API**
  (`artifact_id` + `version_id`) — governance-scoped, not a creation API.

### API / files layer
- Files API: immutable opaque `file_id`s, no versioning (changed content = new id), no
  provenance beyond filename.
- Code-execution containers: produced files surface as `file_id`s in tool results; 30-day
  container retention vs until-deleted file_ids — a *separate* lifecycle from artifacts.
- Skills outputs (.xlsx/.pptx) are download buttons in the conversation — **not artifacts**:
  no versions, no identity across regenerations.

### Claude's gaps vs the owner framing
1. No machine-readable provenance anywhere (the conversation is implicit, human-only provenance).
2. No transform/lineage record; versions are opaque snapshots; remix severs ancestry.
3. Non-generated outputs (datasets, downloads, intermediates) **cannot be artifacts** — they
   live in the disjoint files world.
4. No dataset evolution under an identity; no content addressing; three unlinked
   stores/retention regimes.

## 2. Other agent products (cross-cutting synthesis)

Surveyed: ChatGPT Canvas (being retired — OpenAI de-reified the artifact object), Gemini
Canvas / AI Studio Build, Manus, Devin, OpenHands, GitHub Copilot coding agent (+ sunset
Copilot Workspace), Code Interpreter / Julius, v0 / Lovable / Bolt.new, Kosmos.

- **Every product reifies exactly one privileged artifact type** (canvas doc, app block, PR,
  file set) and treats everything else as chat exhaust.
- **Version identity is anchored to chat turns** where it exists (v0: model edits create
  versions, *human edits don't*; Lovable edit cards; Bolt rollback-to-message). Histories are
  linear with non-destructive restore; branching is universally outsourced to git export or
  whole-container forking (remix/copy semantics).
- **Provenance is shipped by almost nobody.** Two real exceptions: GitHub Copilot coding
  agent (each commit message links to the producing session logs — artifact→step backlink as
  product) and Kosmos (every claim in the report cites the producing code/data analysis).
  Trajectory-level replay (Manus session replay, Devin session timelines, OpenHands event
  log) exists but no queryable "file ← step ← inputs" record. Three 2025–26 arXiv lines
  (PROV-AGENT/ORNL, execution-lineage, provenance-gap attacks) exist specifically to name
  this gap.
- **Manus is the only product with a tiered file ontology**: user uploads and agent-created
  deliverables are both durable first-class classes (restored across sandbox recycling);
  intermediates are an explicit ephemeral class. Closest shipped match to "not just
  AI-created outputs."
- **Julius versions the procedure, not the outputs** (saved re-runnable Notebook —
  reproducibility-by-recipe); Code Interpreter is the anti-model (all outputs expire by
  design).
- PR-centric agents (Devin/OpenHands/Copilot) get versioning free from git but non-code
  outputs are second-class escape hatches ("upload to cloud storage before the session ends").

## 3. Protocol + data-engineering primitives (the composable picture)

Four orthogonal primitives, each with a best-in-class donor:

### 3.1 Wire/delivery identity — A2A + MCP
- **A2A `Artifact` is first-class protocol surface**: `{artifactId, name, description,
  parts[], metadata, extensions}` on a Task; parts = text | file-by-bytes | **file-by-URI** |
  structured data. Streaming via `TaskArtifactUpdateEvent {artifact, append, lastChunk}` —
  stable identity across chunked delivery. No versioning/lineage — purely a delivery envelope.
- **MCP**: resources = URI-identified, subscribable (`resources/subscribe` →
  `updated` notifications), typed (`mimeType`, `annotations.audience/priority/lastModified`);
  **`resource_link` in tool results** = "tool produced an output, here is its handle" (
  explicitly need not appear in `resources/list`).
- **MCP-UI / A2UI fold in for free**: a UI payload is just a resource/artifact with a special
  URI scheme (`ui://`) or mimeType (`text/html;profile=mcp-app`; A2UI = declarative JSON
  component tree riding A2A/AG-UI parts). AG-UI has no artifact type — state snapshots +
  RFC 6902 deltas are its streaming-update analog. → the owner's "mcpui and a2ui as
  artifacts" is structurally confirmed: **UI is an artifact kind, not a parallel system.**

### 3.2 Version chain (dataset → cleaned → v2) — W&B Artifacts
- Identity `project/name:vN|alias`; log to the same name → **content checksum decides**: new
  version only if bytes changed (file-level dedup via content-hash manifests); mutable
  aliases (`latest`, `production`) over immutable versions; incremental drafts re-index only
  changed files. MLflow = the simpler variant (auto-increment + alias). lakeFS = the fully
  general form (branches/commits/merges, `repo/ref/key` addressing) — likely overkill.

### 3.3 Transform record (`b = transform(a)`) — DVC + OpenLineage + RO-Crate
- **DVC stage is the literal thing**: `{cmd, deps (the script is itself a hashed dep), outs,
  params}`; `dvc.lock` pins content hashes of every dep/out; `dvc repro` re-executes only
  stages whose input hashes changed; run-cache maps (cmd, dep-hashes) → out-hashes. "Give the
  user the script that produced each artifact" = the stage's `cmd` + hashed `deps`, verbatim.
- **OpenLineage** is how to carry it on events: `RunEvent {run, job, inputs[], outputs[]}`
  with facets — the transform script travels as `sourceCode`/`sourceCodeLocation` job facets.
- **RO-Crate** is the export/interchange format: a folder + JSON-LD manifest;
  `CreateAction {object (inputs), result (outputs), instrument (the software), agent,
  startTime}` — literally `result = instrument(object)` per the spec's own example.

### 3.4 Lineage vocabulary + query — W3C PROV + W&B + Pachyderm
- PROV names the edges: Entity/Activity/Agent; `wasGeneratedBy`, `used`, `wasDerivedFrom`
  (subtype **Revision** = the v1→v2 edge), `wasAttributedTo` (which agent/model/user made it).
- W&B's bipartite run↔artifact DAG with four traversal verbs (`logged_by`, `used_by`,
  `used_artifacts`, `logged_artifacts`) is the minimal complete query API.
- Pachyderm adds two ideas worth stealing: **subvenance** (downstream query: what did this
  dataset go on to produce) and **Global ID** (one id stamps the whole causal slice — the
  natural per-turn/per-session grouping). Its lineage is **by construction** (recorded at
  execution), not annotated after the fact.

### 3.5 Sandboxes (the negative lesson)
E2B/Modal/Jupyter: files have **no identity beyond the sandbox/container** — identity must be
minted by the platform **at the export boundary**, the moment a tool result, download, or
sandbox read crosses into the session record. This is why "any origin" works: identity comes
from *registration*, not from *who created it*.

### Composed picture
A2A artifact/parts as the wire envelope + MCP resource_link as the tool-side registration
hook → registry with W&B identity/versioning (name + content-hash dedup + vN + aliases) →
every producing execution logged OpenLineage-shaped (inputs, outputs, source code) with
DVC-style pinned hashes → PROV edge vocabulary + Pachyderm global-id grouping for queries →
DVC-style repro replays the recorded cmd against pinned input versions → RO-Crate as the
"hand the user the whole bundle" export. **No single system fills the gap: A2A/MCP have
delivery but no memory; W&B/MLflow have memory but no replay; DVC has replay but assumes a
git repo, not a live session; sandboxes have execution but no identity.**

## 4. Local prior art

### clio-relay already has the durable record shape
`clio_relay/models.py:607` — `ArtifactRef {artifact_id, job_id, sequence, uri, kind,
size_bytes, sha256, created_at, metadata}`; minted from a real file with sha256+size at
capture (`spool.py:274`, ownership schema `clio-relay.owned-artifact.v1`); persisted three
ways (by id, per-job index, ordered sequence); **`artifact.created` durable event on
creation**; GC'd with its job through typed tombstone phases; `TaskTimelineEvent.artifact_refs`
links steps→artifacts; surfaced over CLI/HTTP/MCP (`relay_list_artifacts`). This is the
reference shape — and #948's task handle already reserves `artifact_ref` to be
relay-compatible.

**Application execution is already spec'd there too.** `JarvisRunSpec` (models.py:312) is a
relay job whose specification is exactly-one-of `pipeline_yaml | pipeline_path |
pipeline_name | command` (+ `package`, `workdir`, `env`, `timeout`), alongside
`RemoteAgentTaskSpec` (remote agent run) and `McpCallSpec` — the latter with
`expected_server_artifact_digest` (a sha256 pinning the executing server's identity, i.e.
the *executor* is already content-addressed). Mapped onto the lineage vocabulary this is a
complete PROV triple: **Activity** = the relay job (application execution), **used** = the
configuration/Jarvis-package artifact, **generated** = the application outputs / surrogate
model artifacts. The Jarvis package plays exactly DVC's "the script is itself a hashed dep"
role — the transform's *specification* is an artifact of its own, versionable like any
dataset (config v1 → tuned config v2), and `b = transform(a)` reads as
`outputs = run(jarvis_package, inputs)`. Surrogate-model-as-output matches W&B's
model-artifact semantics (type-tagged artifact versions with producer-run lineage: dataset
→ training run → model vN).

The producer-only lineage gap was filed as **iowarp/clio-relay#58** and **FIXED by relay
PR #59** (verified 2026-07-16): `ArtifactUse {artifact_id, sha256}` content-pinned pairs on
`RelayJob.used_artifact_refs` (unique, canonically sorted, ≤1000); durable `UsedArtifactRef`
records — self-described as "a W3C-PROV-style `used` edge" — written immutably into forward
(`used_artifacts_by_job`), reverse (`artifact_users`), per-consumer, and ordered-sequence
indexes; submit-time validation (artifact must exist, be content-addressed, digest must
match — typed `QueueConflictError`s; the edge set is immutable per job); paged queries both
directions over CLI/HTTP/MCP; and a typed GC guard (`artifact_used_by_retained_job`) that
blocks purging an artifact still referenced by a retained consumer. The Jarvis pipeline spec
also gets artifact identity at execution (`endpoint.py:605`, kinds `jarvis_pipeline` /
`jarvis_pipeline_reference`). Relay's record model is now bidirectional and is the
convergence target as-is.

### clio-agent runtime has NO artifact record today
Output-producing pathways and what provenance exists (anchors verified 2026-07-16):

| pathway | produces | provenance today |
|---|---|---|
| MCP tool writes deliverable (plot/CSV) — output-path grounding, `tools/execution.py:1176` | PNG/CSV/PDF in workspace | grounded path in the tool result, then **discarded** |
| `workflow_state` artifact fields (`workflow_state/schema.py:88`) | typed `(section,key)` path strings | path string only; never linked to the producing tool call |
| evidence grounding (`gact/evidence.py:96`) | corrects fabricated paths in the answer | read-only; emits nothing |
| `fs_propose_edit` diffs (`turn_finalize.py:451`) | file_diff Part + pending diff row | **the ONE runtime provenance event**: `artifact.proposed` (file-diffs only) |
| policy-enforced file writes (`tools/fs_write.py`) | any file | `{path,size,ok}` returned; no session/tool record |
| workspace file GET (`routes/workspaces.py:406`) | serves bytes to client | stateless; nothing knows which files a session created |
| test harness scan (`tests/test_real_cases/clio_sut.py:722`) | grader artifact list | **the only tool-call→file linkage anywhere — test-only** |

Notable: a generated plot PNG **never becomes an outbound wire part** — the client must know
the path and GET it. `structured_outputs.artifacts` is declared inert (`builders.py:1083`).
ARC has no typed "file created" event kind to anchor a provenance edge (`arc/schema.py`).

### The NDP four, mapped
| output | materializes as | provenance today |
|---|---|---|
| (a) metadata dataset | `acquisition.metadata_path`/`catalog` in workflow_state | path string |
| (b) station dataset → cleaned → v2 | `ndp_stage_resource`/`pandas_*` writes; `acquisition.local_path` | path string; **no version chain — v2 overwrites or sits beside v1 with no edge** |
| (c) AI-generated image | `plot_plot_timeseries` grounded `output_path`; `visualization.plot_path` | path string; no wire identity |
| (d) analysis report | final answer prose (evidence-grounded) | not even a file |

None of the four emits an artifact event; nothing records the producing tool call; the
"script that produced it" exists transiently as tool-call args in the trajectory but is never
bound to the output.

## 4b. Companion: deterministic capture design (Cowork session, 2026-07-16)

A second research thread (owner + Cowork) produced `artifact-provenance-design.md` — the
capture-mechanics companion to this survey: LLM never load-bearing in the chain of custody;
confidence tiers (exact / traced / correlated / declared / gap) with per-record evidence
and permanent labels; designated-not-discovered artifacts; managed-CAS vs referenced
custody; permission boundary = provenance boundary; read-only-external as the keystone
mode; universal hash+snapshot-diff floor; PROV/RO-Crate export. Key research finding:
declared provenance (DVC/W&B) historically beats observed provenance (syscall/AST tracing
died of noise+cost); CWLProv retreated from full PROV to flat CreateAction.

**Superseded by `docs/design/artifact-provenance-design.md` (v2, 2026-07-16)** — the revised
design resolving the adversarial review (env field, lease-scoped attribution, tool-declared
mechanism, evidence classes, custody tiers, tracing demoted) and recording the scope
decision: Campaign A = artifacts + provenance floor; Campaign B = sandboxing + provenance
upgrade. Original review adaptations kept below for the record: (1) MCP tool calls are NOT `exact` tier (harness
sees declared args, not actual I/O) — add a `tool-declared` tier, minted at the
`execution.py:1176` grounding hook; (2) CLIO v1 = the universal floor + curated-tool tiers
(no container/srt dependency); (3) add `authority-asserted` identity for referenced
scientific data (DOI/registry checksum/ETag) alongside hash-at-use; (4) TransformRecords
are a projection over the existing event log (RULE 4/#737), only CAS bytes are new storage
(#930 budget discipline applies); (5) relay #59's `ArtifactUse` = the pinned `object[]`,
relay jobs = Activities; relay lacks tier/evidence fields (can ride `metadata`).

## 5. Implications for the campaign (directions, not a plan)

1. **Register at the export boundary.** The output-path grounding hook
   (`execution.py:1176`) already sees every produced file at the exact moment identity should
   be minted — it grounds the path and throws the association away. That is the single
   choke-point for lineage-by-construction (Pachyderm's lesson: record at execution, never
   annotate after).
2. **The transform record is nearly free.** The producing tool call (id, tool, args — which
   ARE the script/params) + input artifact ids + output artifact id is an OpenLineage-shaped
   record clio-agent already has in hand at pathway-A time. `b = transform(a)` = replaying
   that record; the "give the user the scripts" deliverable is an RO-Crate-style export of
   the chain.
3. **Adopt clio-relay's `ArtifactRef` shape** (relay-compatible ids, sha256+size at capture,
   creation event, job/session-scoped index, GC with owner) — the federation campaign then
   swaps storage behind the same record, as #948 already does for task records.
4. **Version chain = W&B semantics on the same name** (content-hash dedup, vN, aliases);
   PROV `wasDerivedFrom/Revision` as the edge vocabulary; per-turn global-id grouping.
5. **UI payloads are an artifact kind** (`ui://` / `text/html;profile=mcp-app` /A2UI JSON) —
   the mcpui/a2ui campaign consumes the artifact surface rather than adding a parallel one.
   Likewise **transform specifications are an artifact kind**: a Jarvis package / pipeline
   yaml / generated cleaning script is itself a registered, versioned artifact, so an
   application execution is just a transform record whose `used` set includes its own spec.
   One taxonomy covers data, images, reports, UI payloads, configs, and models — `kind` +
   mimeType distinguish them, the record shape does not change.
6. **Wire delivery**: A2A's artifact/parts (+ append/lastChunk) is the proven envelope shape
   for streaming artifacts to the TUI/web — and fixes "plots have no outbound identity."
7. **Store discipline**: RULE 4 / #737 — artifact records must be a projection over an
   existing store (semantic events + session metadata), not a fifth store.

Field-wide differentiation check: recording "which operation produced this object, from which
inputs, with which script" would exceed every general-purpose agent product surveyed (only
Copilot's commit→session-log link and Kosmos's claim citations ship anything comparable).

## Sources

Claude: support.claude.com/en/articles/9487310, /9547008, /12111783; code.claude.com/docs/en/artifacts;
docs.anthropic.com files + code-execution tool docs. Products: OpenAI canvas help + retirement notes;
Gemini Canvas help; manus.im blog (sandbox, projects, sharing); docs.devin.ai; docs.openhands.dev +
OpenHands SDK (arXiv 2511.03690); GitHub Copilot coding-agent docs; v0.app/docs; Lovable versioning 2.0;
Bolt rollback docs; Kosmos (arXiv 2511.02824); PROV-AGENT (arXiv 2508.02866); execution-lineage
(arXiv 2605.06365). Protocols/data: a2a-protocol.org spec; modelcontextprotocol.io 2025-06-18 resources
+ tools; mcpui.dev; github.com/google/A2UI; docs.ag-ui.com; docs.wandb.ai artifacts; mlflow.org model
registry; dvc.org pipelines; docs.pachyderm.com provenance + global-id; docs.lakefs.io model;
openlineage.io object model; w3.org/TR/prov-dm; researchobject.org RO-Crate 1.1; e2b.dev docs;
modal.com volumes; nbformat docs. Local: projects/clio-relay `models.py`/`spool.py`/`core_queue.py`;
clio-agent anchors as cited inline.
