# Unified Spotter provenance MCP

Status: corrected architecture and implementation contract  
Date: August 2026

## Purpose

Spotter needs one MCP server through which an agent or UI can inspect provenance
stored in JSONL, Flowcept, CMF, a CLIO-native provenance store, or a future
provider. Spotter is not a CLIO-specific MCP. Any runtime that publishes valid
records into a supported provenance system must be queryable after the producer
has stopped.

The unified MCP is deliberately **not** a least-common-denominator abstraction.
It exposes the maximum capability union of its configured providers, retains
provider-native semantics and results, and returns an explicit capability error
when a requested operation is unavailable. It must never fabricate Flowcept or
CMF semantics from weaker native evidence.

## Non-negotiable architecture

There are two independent planes.

### Write plane

```text
Any agent or workflow runtime
        |
        | publishes provenance
        v
  +-----+---------------+------------------+----------------+
  |                     |                  |                |
JSONL files       Flowcept runtime     CMF/MLMD       CLIO-native store
                  and its storage      and artifact   or future provider
                  profile              storage
```

CLIO is one possible producer on this plane. ARC supplies CLIO's semantic-event
highway. CLIO's configured write adapters publish the parent agentic stream and
artifact substream to the selected systems. The JSONL, Flowcept, and CMF
integrations in CLIO belong here: mapping, delivery, filtering, health, storage
receipts, and write-side configuration.

Other agents may publish directly to exactly the same systems. Spotter cannot
assume that CLIO created the data.

### Query plane

```text
                         Unified Spotter MCP
                         /       |        \
                        /        |         \
           JSONL/native adapter  |     official CMF MCP
                                 |
                        official Flowcept MCP
```

The Spotter MCP talks directly to provider storage/query infrastructure:

- configured JSONL files or mounted volumes;
- the official Flowcept MCP and the Flowcept storage profile it controls;
- the official CMF MCP and its configured CMF server instances;
- a CLIO-native provenance-store interface, when one is configured;
- future adapters registered through the same provider contract.

The Spotter MCP does **not** call a running `clio-agent`, inspect GACT app state,
or ask CLIO to translate one provider's semantics into another provider's
semantics. Stopping the producing agent must not prevent later provenance
queries.

## Repository boundaries

### `clio-agent`

CLIO owns only its producer responsibilities:

- emit ARC-derived semantic events;
- select and configure write-side agentic and artifact providers;
- project events into Flowcept and CMF without making either mandatory;
- manage write delivery, filtering, receipts, health, and shutdown;
- select artifact custody independently where supported.

CLIO must not contain the Spotter query federation layer. In particular, it must
not contain a router that substitutes native CLIO queries for unavailable
Flowcept or CMF operations.

### Spotter MCP

The canonical Spotter implementation belongs in the CLIO agent marketplace. It
owns:

- provider discovery and capabilities;
- clients for the official Flowcept and CMF MCPs;
- JSONL and other native-store readers;
- the maximum unified tool registry;
- lossless provider result preservation;
- optional UI projections;
- typed capability, availability, and provider errors;
- access policy for destructive or administrative upstream tools.

### UI

The UI consumes MCP result metadata and provider-independent render models. It
does not require CLIO to reinterpret the selected provider. Provider-specific
features remain available as additional views rather than being erased to fit a
smaller schema.

## Upstream MCPs inspected

The design is grounded in the actual upstream repositories and registered MCP
schemas, not their README summaries alone.

- Flowcept: `ORNL/flowcept` at
  `c000b10ea49659af6c5821b61918f3893bd46a92`.
- CMF: `HewlettPackard/cmf` at
  `53d9c3e518ab2fde46955f10520d4842c572bf05`.

Both packages were installed in isolated environments and their real FastMCP
tool registries were enumerated. Both currently resolve an incompatible MCP 2.x
through their open-ended dependency declarations; constraining MCP below 2 made
the current sources importable for schema inspection. This is an upstream
packaging compatibility issue to verify with the teams, not a reason to replace
their interfaces.

## Maximum capability union

The combined server starts from the full upstream tool registries. Provider
prefixes already distinguish most names, so upstream semantics should be
retained rather than renamed into weaker generic operations.

### Flowcept surface

The inspected Flowcept MCP registers 36 tools:

- database queries: `db_query_tasks`, `db_query_workflows`,
  `db_get_task_summary`, `db_list_campaigns`, `db_list_agents`,
  `db_query_objects`, `db_highlight_lineage`, and `db_fix_query`;
- DataFrame queries: `run_df_query`, `df_query_tasks`,
  `df_query_workflows`, `df_query_objects`, `df_get_task_summary`,
  `df_get_objects_summary`, `df_list_campaigns`, `df_list_agents`,
  `df_highlight_lineage`, and `df_fix_query`;
- charts and dashboards: `db_make_chart`, `db_get_dashboard`,
  `db_update_dashboard`, `df_make_chart`, `df_get_dashboard`, and
  `df_update_dashboard`;
- schema and reports: `get_schema_context`, `get_df_schema_context`,
  `get_workflow_schema_context`, and `generate_workflow_card`;
- runtime and control: `get_latest`, `check_liveness`, `check_llm`,
  `record_guidance`, `show_records`, `reset_records`, `reset_context`, and
  `load_buffer_messages`.

Flowcept's result contract is itself meaningful and must be preserved:
`ToolResult` carries `code`, `result`, `extra`, and `tool_name`. DB and
DataFrame modes are different supported query/storage profiles, not concepts
that Spotter should collapse.

Administrative and mutating tools remain part of the maximum capability
registry. A read-only Spotter expert may be denied permission to invoke them,
but policy denial is different from pretending the capability does not exist.

### CMF surface

The inspected CMF MCP registers eight tools:

- `cmf_show_pipelines`;
- `cmf_show_executions`;
- `cmf_show_executions_list`;
- `cmf_execution_lineage`;
- `cmf_show_artifact_types`;
- `cmf_show_artifacts`;
- `cmf_artifact_lineage`;
- `cmf_show_model_card`.

CMF returns one result per configured CMF client, retaining the client URL and
either `data` or `error`. The unified MCP must keep those per-instance results.
CMF pipelines, executions, artifacts, layered lineage trees, and four-part
model cards remain CMF concepts.

The current upstream client/server snapshot also needs live validation. Some
client methods still call pipeline-based REST paths that are commented out in
the current server in favor of stage-based endpoints, and the execution-lineage
MCP truncates its supplied UUID to four characters. These are review questions
for HPE, not behavior Spotter should silently patch by inventing different
semantics.

### Native providers

Native adapters expose the capabilities genuinely supported by their source:

- a JSONL adapter can search, filter, correlate, and summarize fields actually
  present in its configured record schema;
- a native artifact graph can expose its recorded nodes, edges, evidence, and
  custody data;
- a CLIO-native store adapter can expose that store's real query contract;
- future formats add their own tools and render projections.

A native adapter must not claim to implement Flowcept workflows, dashboards,
campaign summaries, or CMF model cards unless those records and semantics are
actually present in that native format.

The previous temporary Spotter SQLite interface and its invented run-health,
quarantine, and alert operations are not the canonical provenance interface and
must be discarded.

## Provider adapter contract

Each Spotter-side adapter declares metadata rather than being hidden behind one
replacement implementation:

```text
ProviderAdapter
  id                         stable provider-instance identity
  kind                       jsonl | flowcept | cmf | clio-store | extension
  capabilities()             exact supported tool and feature set
  register_tools(registry)   provider-native MCP tools
  invoke(tool, arguments)    lossless provider invocation
  ui_projection(tool, data)  optional render model; never the evidence record
  health()                   connectivity and configuration state
  close()                    provider-owned cleanup
```

The plugin contract permits multiple instances of one provider. Flowcept and
CMF retain their own instance-selection arguments where their MCPs provide
them.

## Capability behavior

The MCP exposes a discovery tool that reports every configured provider,
instance, upstream tool, read/write classification, UI projections, health, and
policy state.

Invoking a tool follows these rules:

1. Route only to an adapter that natively advertises that tool.
2. Preserve the upstream arguments and semantics.
3. Preserve the complete upstream result as evidence.
4. Add UI metadata only as a separate, non-destructive projection.
5. If no configured provider supports the tool, return
   `capability_unavailable`.
6. If the provider is configured but unreachable, return
   `provider_unavailable`.
7. If policy forbids a mutating tool, return `permission_denied`.
8. Never retry the request against a semantically different provider.

For example, `db_query_tasks` without a queryable Flowcept DB profile is a
capability error. Spotter must not manufacture a Flowcept task response from
CLIO JSONL merely because both contain timestamps.

## Lossless result and UI contract

The MCP result wrapper adds routing and rendering metadata while retaining the
provider result intact:

```json
{
  "schema_version": "spotter.provenance.v1",
  "provider": "flowcept",
  "provider_instance": "flowcept-primary",
  "upstream_tool": "db_query_tasks",
  "status": "ok",
  "data": {
    "upstream_result_is_preserved_here": true
  },
  "ui": {
    "component": "timeline",
    "available_views": ["table", "timeline", "gantt"],
    "model": {}
  },
  "warnings": [],
  "error": null
}
```

`data` is the authoritative provider result. `ui.model` is a projection for a
renderer. Thus a CMF layered lineage tree can be projected to generic graph
nodes and edges without deleting the original CMF tree, while Flowcept task
records can drive a timeline/Gantt view without discarding Flowcept-specific
fields.

Provider-specific UI capabilities may add modes such as Flowcept dashboards,
workflow cards, charts, and CMF model cards. The UI chooses from
`available_views`; it does not infer provider support from an empty response.

An error has a stable outer shape and retains safe diagnostics:

```json
{
  "code": "capability_unavailable",
  "message": "No configured provenance provider supports db_query_tasks.",
  "provider": null,
  "tool": "db_query_tasks",
  "recoverable": true,
  "details": {
    "configured_providers": ["jsonl", "cmf"]
  }
}
```

Empty evidence is a successful empty query, never a claim that a run is healthy
or anomaly-free.

## Configuration and deployment

Provider configuration belongs to the Spotter MCP process. It contains storage
mounts and upstream MCP connection details, not a GACT URL.

Representative profiles are:

- local JSONL: mount one or more provenance roots read-only and register the
  JSONL adapter;
- queryable single-agent Flowcept: configure the official Flowcept MCP against
  its supported online Redis/MongoDB profile;
- distributed Flowcept: configure the same official MCP against the shared
  Flowcept infrastructure used by the producing nodes;
- CMF: configure the official CMF MCP with one or more CMF client/server
  instances;
- mixed: enable Flowcept and CMF together, exposing the union of both MCPs;
- extension: install another adapter without changing producer runtimes.

JSONL does not require a container. Where containers are used, the Spotter MCP
talks to those provider containers or mounts their durable volumes. It does not
talk back to the producer.

## Spotter expert behavior

The Spotter expert receives this unified MCP. Its prompt must:

- inspect capabilities before planning queries;
- use provider-native tools and fields;
- distinguish absence of evidence from evidence of absence;
- surface partial results and per-instance errors;
- never assume CLIO produced the records;
- never request a fallback that changes provenance semantics;
- use only read-authorized tools unless the user explicitly authorizes a
  control-plane mutation.

The temporary SQLite-specific Spotter prompt and tool vocabulary must be
removed.

## Acceptance gates

The architecture is not verified until all of the following use the same
Spotter MCP build:

1. A non-CLIO producer writes supported JSONL and Spotter queries it after the
   producer exits.
2. A real CLIO run publishes to Flowcept; the official Flowcept MCP tools query
   the recorded run through Spotter.
3. A real CLIO run publishes artifact provenance to CMF; the official CMF MCP
   tools query the recorded artifacts and lineage through Spotter.
4. Flowcept and CMF are enabled together and their complete capability union is
   visible without collapsing workflow and artifact concepts.
5. With Flowcept disabled, a Flowcept-only tool returns
   `capability_unavailable` and does not query JSONL.
6. With CMF unreachable, a CMF tool returns `provider_unavailable` and does not
   substitute native lineage.
7. Flowcept timeline/Gantt data and CMF lineage/model-card data produce valid UI
   projections while preserving raw upstream results.
8. CLIO is stopped before a query, proving that the MCP depends on durable
   provenance infrastructure rather than the producing process.
9. The same setup is exercised by the Spotter expert using a real agent run;
   mocks are limited to unit tests.

Until these gates pass, CMF and Flowcept support must be described as under
implementation rather than live-verified.

## Explicitly rejected designs

- A CLIO `/v1/provenance/query/{tool}` federation endpoint.
- A `NativeProvenanceQuery` that impersonates Flowcept or CMF concepts.
- Flowcept or CMF query code inside CLIO's write providers.
- A unified MCP that calls GACT or requires a live CLIO process.
- A minimal generic tool list that hides richer upstream capabilities.
- Silent provider fallback when the requested semantics are unavailable.
- Treating Flowcept objects as authoritative CMF/native artifact identity.
- Treating a Flowcept workflow and a CMF pipeline as the same entity.

The correct invariant is simple: producers write provenance to configured
systems; Spotter queries those systems directly and reports exactly what each
system can prove.
