# Unified Spotter provenance MCP

Status: implemented and live-qualified against Flowcept plus CMF
Date: August 2026

## Purpose

Spotter provides one purpose-built, read-only MCP for investigating agentic execution and
artifact lineage. It is our interface, designed from scratch after inspecting the Flowcept and
CMF query surfaces. It is not a proxy for either upstream MCP and it does not call clio-agent.

The producer and query paths are intentionally different:

```text
gact-tui -> stable REST resources -> clio-agent -> configured provenance provider

agent -> purpose-specific MCP tools -> Spotter MCP -> provider stores and services
```

The UI path has a small, stable wire contract. The agent path has more, narrower tools so a model
can select the right operation. gact-tui never embeds or invokes Spotter MCP.

## System boundary

### Write plane

CLIO is one producer. ARC remains its live context substrate and semantic-event source. CLIO
projects that stream downstream according to explicit configuration:

```text
ARC semantic-event highway
    |
    +-- agentic provenance stream -> JSONL, CLIO, or Flowcept provider
    |
    `-- selected artifact substream -> native or CMF artifact provider
                                           |
                                           `-- provider-owned artifact storage
```

Artifact provenance is an overlapping substream, not a second event source. Flowcept and CMF are
optional and are not ARC replacements. Selecting Flowcept does not select artifact custody;
selecting CMF does not make Flowcept a prerequisite.

Other runtimes can write compatible JSONL or use Flowcept/CMF directly. Spotter queries durable
provider state, so the original producer need not still be running.

### Query plane

```text
explicit CLIO YAML
      |
      v
Spotter provider factory
      |
      +-- Flowcept client -> MongoDB
      +-- CMF client      -> CMF REST server
      `-- native client   -> documented JSONL/workspace evidence
```

The CLIO YAML is configuration evidence only. Spotter reads it to determine which providers are
enabled, which one is the query default for each domain, and where their stores/services live.
Spotter never calls GACT, imports CLIO application state, or asks CLIO to translate results.

Provider clients are direct:

- Flowcept queries the persisted workflow/task collections in MongoDB.
- CMF queries the current pipeline/stage/execution/artifact REST resources.
- Native queries explicit JSONL and workspace evidence under a documented portable schema.

The upstream Flowcept and CMF MCPs informed the vocabulary and capability analysis. They are not
runtime dependencies and are not started, proxied, or composed by Spotter.

## Provider selection

Provider selection is deployment configuration, never a model-supplied tool argument. The current
configuration axes are independent:

```yaml
provenance:
  agentic:
    providers: [flowcept]
    query_default: flowcept
    flowcept:
      settings_path: /runtime/flowcept.yaml
  artifacts:
    provider: cmf
    cmf:
      server_url: http://127.0.0.1:8380
      pipeline_name: clio-agent
```

Flowcept endpoint and database settings come from its referenced settings file. CMF endpoint and
pipeline come from CLIO's artifact-provider configuration. Credentials remain in deployment
configuration and are never accepted as MCP arguments or returned in results.

When native is selected, the journal and workspace must be explicit:

```yaml
provenance:
  agentic:
    providers: [jsonl]
    query_default: jsonl
    jsonl:
      path: /runtime/provenance
  artifacts:
    provider: native
    native:
      workspace_root: /workspace
```

There is no silent fallback. If Flowcept is selected and unreachable, the request fails as a
provider error. If native is selected and cannot answer a Flowcept- or CMF-level semantic, the
operation fails with `capability_unavailable`; it does not manufacture an approximation.

## Tool design

Spotter exposes a deliberately designed union of useful investigation operations. It does not
copy every administrative, dashboard, prompt, repair, or arbitrary-code tool from the upstream
MCPs. The tools are purpose-specific rather than a generic query language:

### Discovery and correlation

- `capabilities`
- `trace_correlation`

### Agentic execution

- `list_campaigns`
- `list_workflows`
- `list_agents`
- `query_tasks`
- `summarize_tasks`
- `get_timeline`

### Artifact provenance

- `list_pipelines`
- `list_executions`
- `list_artifact_types`
- `list_artifacts`
- `get_execution_lineage`
- `get_artifact_lineage`
- `get_model_card`

This surface is richer than a minimum-common-denominator API. A provider advertises only the
operations it can support honestly. Native support is additive convenience over genuine native
records, not a reinterpretation of weaker evidence as Flowcept or CMF state.

## Result and error contract

Normalized top-level fields let an agent correlate providers and let a renderer consume stable
timeline/graph shapes. Source evidence remains under a namespaced extension:

```json
{
  "workflow_id": "workflow-1",
  "status": "completed",
  "started_at": 1787385600.0,
  "ended_at": 1787385612.5,
  "extensions": {
    "flowcept": {
      "custom_metadata": {
        "clio": {"session_id": "session-1"}
      }
    }
  }
}
```

The projection removes large Flowcept runtime configuration fields and recursively strips common
credential-like keys before returning provider evidence. Binary payloads and Flowcept `data`
payloads are not exposed.

Stable typed errors distinguish:

- `capability_unavailable`: the selected provider cannot answer the operation;
- `provider_unavailable`: its store or service cannot be reached;
- `invalid_request`: arguments or identifiers are invalid;
- `not_found`: the requested recorded entity does not exist.

An empty successful query is only empty evidence. It is never reported as proof that a run was
healthy, complete, or anomaly-free.

## Native JSONL contract

Native support accepts CLIO semantic events carrying `event_type` and a portable Spotter dialect:

```json
{"schema_version":"spotter.provenance.v1","record_type":"workflow","workflow_id":"wf-1","data":{"status":"completed"}}
```

Portable record types are `workflow`, `agent`, `task`, `pipeline`, `execution`, `artifact`, and
`model_card`. Pipeline and model-card operations are advertised only when those record types are
actually present. Unknown JSON objects fail validation instead of becoming provenance by accident.

## UI contract is separate

gact-tui asks clio-agent for concrete REST response shapes. clio-agent obtains the requested view
from the configured provider and returns a provider-neutral render model.

- Observability owns `log`, `timeline`, `gantt`, and execution-graph modes. Flowcept can back those
  modes, but Flowcept-specific data remains annotations rather than UI routing logic.
- Artifact Provenance is a separate tab and graph. It can be backed by native CLIO provenance or
  CMF without changing how the UI requests or draws the graph.

The Spotter MCP can return compatible timeline and graph structures for agent reasoning, but the UI
does not call MCP. These two consumers may share schema concepts; they do not share a transport.

## Live qualification

The final homelab qualification used Flowcept `full-online` (Redis plus MongoDB), a CMF
server/PostgreSQL deployment with local DVC-compatible custody, and CLIO configured with Flowcept
as its only downstream agentic provider and CMF as its artifact provider/store. Native JSONL and
the native artifact CAS were absent; ARC remained the required live context/event source.

A real Codex Luna Spotter expert in session `sess_72b9686f6177` made eight successful Spotter MCP
calls across both domains. It reported Flowcept and CMF ready, summarized a 126-record Flowcept
workflow, found artifact `artifact_0bb8c80d22be41dda2264446453484bf` at
`cmf+dvc://local/files/md5/9b/e69deba6ddd9e264bb38801512b813`, and returned a lineage graph
rooted at that stable artifact id with four nodes and zero edges.

The exact CLIO listener was then stopped. A bounded standalone Spotter process still exposed all
15 tools and queried the same durable Flowcept and CMF state, including a completed 134-record
workflow and correlation counts from both provider domains. This proves that Spotter does not call
back into CLIO.

All tools declare standard MCP read-only and idempotent annotations. CLIO now adds the installed
blueprint checksum to declared MCP cache identity, so an updated pack refreshes tool metadata even
when its launcher is unchanged. A clean post-fix Luna session made capabilities and artifact-
lineage calls with zero permission rows and returned the same four-node graph.

The implemented UI path remains separate. No gact-tui source change was required: its focused
Observability and artifact-provenance suites, TypeScript checks, and production build passed over
the existing REST/provider-neutral rendering boundary.

## Repository ownership

### clio-agent

- ARC-derived event emission;
- agentic/artifact provider selection;
- Flowcept and CMF write mapping;
- bounded delivery, receipts, filtering, health, and shutdown;
- stable REST read models for gact-tui.

### clio-agent-marketplace / Spotter

- standalone MCP server and tool vocabulary;
- explicit CLIO YAML loading;
- direct Flowcept, CMF, and native provider clients;
- capability routing and typed errors;
- safe normalized results with namespaced provider evidence;
- the Spotter expert prompt and MCP declaration.

### gact-tui

- Observability log/timeline/Gantt/execution graph;
- separate artifact-provenance graph;
- provider-independent rendering over CLIO REST contracts;
- no provider credentials, provider database clients, or MCP server.

## Acceptance gates

The implementation is qualified only when all relevant gates are recorded separately:

1. Unit tests cover config selection, direct provider queries, normalization, credential pruning,
   capability errors, and no-fallback behavior.
2. A real Luna-driven CLIO run publishes agentic records to Flowcept.
3. The same run publishes artifact provenance and custody records to a real CMF server/store.
4. That run uses Flowcept plus CMF with native JSONL/CAS downstream persistence disabled.
5. The standalone Spotter MCP queries the exact Flowcept and CMF records without calling CLIO.
6. The real Spotter expert invokes that MCP and explains the recorded run.
7. Spotter queries still work after the producing CLIO process is stopped.
8. The UI's focused timeline/Gantt/execution-graph and artifact-graph suites pass independently.
9. Repository-wide tests, lint, and type checks are reported honestly; a timeout, skip, or
   environment failure is not a green gate.

## Explicitly rejected designs

- Putting query federation or native query emulation in clio-agent.
- Routing gact-tui through Spotter MCP.
- Having Spotter call GACT or require a running producer.
- Running or proxying the upstream Flowcept and CMF MCPs.
- Copying the union of all upstream administrative tools without designing our own interface.
- A tiny generic interface that erases useful Flowcept or CMF semantics.
- Provider arguments chosen ad hoc by the model.
- Silent fallback to another provider when a semantic is unavailable.
- Treating Flowcept and CMF as interchangeable provenance backends.
- Treating Flowcept's MongoDB/GridFS internals as CLIO artifact custody.

The invariant is: producers write to configured provenance systems; the UI asks CLIO for stable
REST render models; agents ask Spotter for purpose-specific provenance operations; Spotter reads
the configured durable systems directly and reports only what they can prove.
