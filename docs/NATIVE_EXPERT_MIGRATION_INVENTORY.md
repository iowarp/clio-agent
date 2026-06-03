# Native Expert Migration Inventory

Related issue: `iowarp/clio-agent#629`.

## Target

The target state is zero native Python domain experts required for normal CLIO
operation or benchmark success.

Python remains the implementation language for runtime substrate: DSPy module
construction, provider calls, tool/MCP adapters, validation, storage, memory,
artifact handling, semantic logging/streaming, trust, permissions, sandboxing,
and generic execution policies. Domain expert behavior should live in Agent
Blueprints and marketplace packs as configurable DSPy semantic contracts.

## Classification

Use these categories when migrating native code:

- **Runtime substrate:** keep in Python. This is generic execution machinery.
- **Declarative tool adapter:** keep implementation in Python, expose through
  tool/MCP descriptors and blueprint tool lists.
- **Blueprint expert behavior:** migrate into Agent Blueprint signatures,
  module declarations, prompts, skills, tools, and policies.
- **Legacy shim:** keep temporarily for compatibility, but remove once
  blueprints have parity and benchmark evidence.

## Native Expert Registry

`src/clio_agent/experts/__init__.py` still registers native DSPy modules:

- `data -> DataExpert`
- `ndp_catalog -> NDPExpert`
- `analysis -> AnalysisExpert`
- `sac_format -> SACFormatExpert`
- `visualization -> VisualizationExpert`

These should not remain the source of domain intelligence. The equivalent
expert identities already exist in the built-in `data-exploration` Agent
Blueprint, and marketplace packs should replace benchmark-specific domain
behavior.

## Expert-By-Expert Migration Map

### `DataExpert`

File: `src/clio_agent/experts/data_expert.py`

Current native behavior:

- Owns HDF5/ADIOS/SAC/NDP path and intent detection.
- Chooses between ADIOS inspection, NDP discovery, SAC inspection, HDF5 dataset
  analysis, HDF5 file analysis, or no-tool synthesis.
- Instantiates `NDPExpert` and `SACFormatExpert` directly.
- Performs typed tool validation and formats deterministic analysis text.

Migration:

- Keep HDF5/ADIOS/SAC/NDP tool implementations and result validators as Python
  tool/runtime substrate.
- Move route/action choice into blueprint DSPy semantics:
  - signature fields for `question`, `parent_evidence`, `available_files`,
    `candidate_actions`, and structured `expert_handoffs`;
  - module type such as ReAct or router/reducer;
  - declared child experts for NDP and SAC;
  - explicit tool availability in the blueprint.
- Remove direct native child expert instantiation after blueprint sync
  delegation has parity.

### `AnalysisExpert`

File: `src/clio_agent/experts/analysis_expert.py`

Current native behavior:

- Owns Parquet/CSV analysis routing.
- Detects natural multi-file prompts through Python heuristics.
- Calls `spawn_many(...)` and returns `Prediction.nanoagents_spawned`.
- Aggregates spawned worker tool evidence into a parent answer.
- Falls back to `dspy.Predict(AnalysisExpertSignature)`.

Migration:

- Keep Parquet/CSV tools as declarative tools.
- Move multi-file/item detection, worker template, concurrency, partial-failure
  policy, and merge schema into blueprint-declared fan-out semantics.
- Move analysis signature/output schema into blueprint DSPy signature
  declarations.
- Treat current `_detect_parallel_items`, `_spawn_tool_backed_nanoagents`, and
  `_aggregate_tool_backed_spawns` behavior as the prototype for #629, not as
  final expert behavior.

### `VisualizationExpert`

File: `src/clio_agent/experts/visualization_expert.py`

Current native behavior:

- Builds a ReAct module over chart tools.
- Wraps local chart functions as `dspy.Tool` objects.
- Special-cases SAC archive plotting outside the normal ReAct path.
- Returns visualization description, file path, and tool provenance.

Migration:

- Keep chart and SAC plotting functions as tools/adapters.
- Move visualization signature, ReAct module selection, allowed tools, artifact
  output schema, and file-path return contract into blueprint DSPy semantics.
- Move SAC plotting ownership into the declared SAC/visualization expert
  hierarchy instead of native file suffix branching.

### `NDPExpert`

File: `src/clio_agent/experts/ndp_expert.py`

Current native behavior:

- Owns NDP/CKAN intent terms, search terms, organization filters, resource
  format selection, bounded staging, validation, and failure formatting.
- Runs NDP tools directly through `NativeToolRunner`.

Migration:

- Keep NDP MCP/tool adapters, validators, bounded staging guardrails, and
  provenance helpers as runtime/tool substrate.
- Move catalog workflow policy into a reusable blueprint subtree:
  catalog search, candidate selection, resource resolution, download/staging,
  failure return, and parent-owned recovery.
- Avoid deterministic domain term lists as the primary route. Blueprint
  prompts/signatures/tools should allow model-owned selection, with validators
  enforcing safety bounds.

### `SACFormatExpert`

File: `src/clio_agent/experts/sac_format_expert.py`

Current native behavior:

- Owns SAC path detection and action selection between inspect, statistics,
  and plotting.
- Runs SAC tools directly through `NativeToolRunner`.
- Formats tool-backed analysis and recommendations.

Migration:

- Keep SAC tool adapters and result validators.
- Move inspect/statistics/plot action selection into blueprint DSPy semantics.
- Keep SAC expert as a declared child under analysis/visualization parents as
  appropriate, with sync delegation returning compact evidence to the parent.

### `ClioAgent`

File: `src/clio_agent/agent.py`

Current native behavior:

- Owns the top-level DSPy action planner and answer synthesizer.
- Contains legacy/native routing and expert invocation paths.
- Extracts native `nanoagents_spawned` from predictions and stores ARC
  invocation metadata.

Migration:

- Keep orchestration runtime, DSPy context management, event emission, ARC
  storage, and provider/tool execution machinery.
- Move hardcoded domain routing/action policies into blueprint-driven planner
  semantics.
- Keep generic support for normalized outputs:
  `tools_called`, `expert_handoffs`, `nanoagents_spawned`, artifacts, memory,
  and surfaced errors.

## Blueprint Parity Surfaces Needed

The migration depends on exposing these DSPy semantics declaratively:

- Signature declarations: input fields, output fields, field descriptions,
  structured evidence schemas, artifact schemas, and surfaced-error schemas.
- Module type: prompt-only, Predict, ChainOfThought, ReAct/tool loop, router,
  reducer, retry/refine, and bounded worker.
- Tool/MCP binding: per-expert tool scope, MCP descriptor scope, trust/launch
  requirements, and typed tool-result normalization.
- Sync delegation: child expert calls, parent resume, continuation contracts,
  repeat/skip policy, and compact child evidence.
- Fan-out/spawn: item source, worker template, bounded item limits,
  concurrency, partial-failure policy, merge schema, and child session events.
- State/context: workspace/session memory scope, parent evidence, artifact
  references, and provider/model defaults.
- Observability: streamed events and semantic logs for LLM/module calls,
  tools/MCPs, memory/artifacts, delegation, spawned workers, errors, recovery,
  and final returns.

## Benchmark Rule

The 12-case benchmark should not count a case as final public-demo evidence if
the case relies on native-only domain expert behavior. During migration, old
native behavior can be used to understand the desired semantics, but a final
case pass must show that the active Agent Blueprint/marketplace pack declared
the relevant expert signature, module, tools, delegation/fan-out, and output
contracts.

## Checklist

- [ ] Add parser/validator support for blueprint-declared DSPy signatures and
      module types.
- [ ] Add normalized runtime construction for blueprint-declared DSPy modules.
- [ ] Add blueprint-declared structured output validation and normalization.
- [ ] Add blueprint-declared fan-out/spawn semantics.
- [ ] Convert `AnalysisExpert` multi-file fan-out into a blueprint-driven pack
      case.
- [ ] Convert `VisualizationExpert` ReAct/chart behavior into blueprint module
      declarations.
- [ ] Convert `DataExpert` NDP/SAC child ownership into blueprint sync
      delegation only.
- [ ] Convert `NDPExpert` catalog workflow policy into a reusable blueprint
      subtree.
- [ ] Convert `SACFormatExpert` action selection into blueprint semantics.
- [ ] Update benchmark case folders with live evidence that no final pass
      depends on native-only domain expert behavior.
