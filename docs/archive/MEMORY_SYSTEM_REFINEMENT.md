# Memory System Refinement

Tracking issue: https://github.com/iowarp/clio-agent/issues/331

Related docs:

- [Hierarchical user-defined experts](HIERARCHICAL_EXPERTS.md)
- [External editable prompt system](../PROMPT_SYSTEM.md)
- [User-defined slash commands](USER_DEFINED_COMMANDS_DESIGN.md)
- [Undo and rewind](UNDO_REWIND_DESIGN.md)
- [Command and capability truth](COMMAND_CAPABILITY_TRUTH_DESIGN.md)
- [ARC memory layer](../ARC_MEMORY_LAYER.md)

## Purpose

CLIO needs memory to become an authoritative context-management system, not
only a storage/cache layer.

The next wave of features makes this necessary:

- Hierarchical experts need per-expert context, handoff, and tool-scope truth.
- Prompt profiles need runtime prompt provenance and model-budget awareness.
- User-defined slash commands need command provenance and scoped context.
- Undo/rewind needs tombstone semantics so deleted transcript content does not
  leak back through ARC.
- Optional NDP integration will add large external catalog/resource context that
  must be scoped, inspectable, and toggleable.

The goal for this issue is to make CLIO able to answer:

- What context did this turn or expert invocation receive?
- Why was each context item included?
- What was dropped, truncated, compacted, or tombstoned?
- Which prompt, command, expert, model, tools, permissions, and context files
  shaped the final model input?
- How close is the session to context pressure, and should the user compact?

This is not a request to rewrite ARC storage first. Storage can evolve after the
context truth model is stable.

## Current State

The current implementation already has useful pieces:

- `ARCMemory` persists conversations, invocations, metrics, domain context,
  dataset profiles, procedural memories, and optimized variant records.
- `ContextCompiler` builds context through a filter, compact, enrich, assemble
  pipeline.
- GACT persists session metadata and message ledgers separately from ARC.
- `/v1/sessions/{sid}/compact` creates an evidence-preserving compact summary,
  appends an exact retained evidence index, stores the summary in ARC, and
  replaces the visible GACT transcript with a synthetic compact message.
- `/v1/memory/stats` reports ARC cache counters and a session block.
- The TUI can show a footer memory chip, `/memory` inspector, transcript-derived
  compaction evidence, context-file metadata, and compact-summary markers.
- Focused tests currently pass for memory stats, context compilation, and
  compact-session behavior.

The problem is that these pieces do not form one authoritative context record.
The model receives a blend of:

- GACT user text,
- attached context files,
- ARC conversation history,
- compact summaries,
- dataset profiles,
- procedural memories,
- routing decisions,
- tool summaries,
- prompt strings,
- expert/router/tool metadata,
- model/provider configuration.

No durable object records the final assembled input and the provenance behind
it.

## Audit Findings

### Memory Stats Are Not Context Truth

`/v1/memory/stats` currently reports:

- ARC cache hits/misses/hit rate/capacity,
- ARC index sizes as approximate global counts,
- session `message_count`,
- cumulative session input plus output token totals,
- hard-coded `tokens_budget=4000`,
- hard-coded `profiles_attached=0`.

This is useful as a health signal, but it is not the retained context sent to
the model. A long session can have a large cumulative token count but a small
compiled context after compaction, and the reverse can happen with attached
files or profiles.

### Context Compilation Is Fixed And Opaque

`ContextCompiler` currently:

- uses the last five ARC conversation messages,
- uses the last three routing decisions,
- uses fixed tier budgets: router 2000 tokens, expert 4000 tokens,
- allocates fixed proportions across conversation, profiles, procedural memory,
  and routing,
- uses rough word-count estimates,
- truncates ordinary messages aggressively,
- has special handling to preserve compact-summary evidence tails.

This is a reasonable v0 pipeline, but it does not explain inclusion decisions,
does not report dropped context, and does not adapt to:

- active provider/model context length,
- output/reserved reasoning budget,
- prompt profile such as `heavy` or `light`,
- expert tier and hierarchy,
- user-defined command behavior,
- NDP resource payload size,
- context-file hash or modification drift.

### Compaction Works But Is Not Yet A Versioned Memory Event

The compact endpoint is now evidence-preserving and updates ARC. The remaining
gap is lifecycle metadata:

- The original archive is kept in process state, not as a durable replayable
  memory event.
- The compact summary is stored as a synthetic assistant message rather than a
  first-class compaction record with compacted message IDs.
- The GACT spec has a `compaction` part type, but CLIO still primarily stores
  compact summaries as text plus metadata.
- Future undo/rewind cannot rely on physical ARC deletion, so compacted and
  tombstoned content need explicit exclusion metadata.

### ARC And GACT Ledgers Can Drift Semantically

GACT visible messages are the UI/source-of-truth transcript. ARC conversation
messages are the runtime context memory. They are written by different paths.
This is acceptable only if context assembly always knows which ledger is
authoritative for a behavior.

For rollback, the rule from the undo/rewind design should hold:

- GACT visible transcript is authoritative.
- ARC is not physically deleted in v1.
- Deleted messages are tombstoned or ignored by future context assembly.
- Deleted transcript content must not reappear after reload/import/compaction.

### Provenance Is Spread Across Features

Tool provenance is relatively strong, and streaming provenance has been
hardened. Prompt, expert, command, and context provenance are not yet unified.

Memory needs a place for:

- prompt id, profile, file path, checksum/version, fallback state,
- expert id, parent/children, tier, pack scope, model fallback,
- user command id, source file, arguments, context mode,
- model provider/id/context window,
- context-file mode, path, size, mtime/hash, truncation,
- permission decisions and policy/audit references,
- NDP source/dataset/resource identifiers when enabled,
- compaction and tombstone lineage.

Without this, the TUI cannot answer why the agent remembered or forgot
something.

## Target Model: Context Frames

Add a durable `ContextFrame` concept.

A context frame is the authoritative record of one model-facing context
assembly. It can be created for:

- a root planner turn,
- a chat turn,
- a tier-2 expert invocation,
- a deeper expert/nanoagent invocation,
- a user-defined command invocation,
- a compaction summarization call.

It should be cheap enough to write on every turn and structured enough for the
TUI to inspect.

### ContextFrame Fields

Recommended initial shape:

```python
ContextFrame = {
    "frame_id": "ctxf_<id>",
    "session_id": "sess_<id>",
    "message_id": "msg_<id>",
    "trace_id": "trace_<id>",
    "parent_frame_id": "ctxf_<id> | null",
    "created_at": 0.0,

    "consumer": {
        "kind": "planner | chat | expert | command | compaction",
        "expert_id": "main | data | analysis | ...",
        "tier": 1,
        "tool_scope": "chat | data | all | none"
    },

    "model": {
        "provider": "argonne | lm_studio | ...",
        "model": "model-id",
        "context_window": 0,
        "reserved_output_tokens": 0,
        "effective_context_budget": 0
    },

    "prompt": {
        "prompt_id": "",
        "profile": "",
        "scope": "builtin | user | workspace | session",
        "path": "",
        "checksum": "",
        "fallback": ""
    },

    "command": {
        "command_id": "",
        "source": "",
        "context_mode": ""
    },

    "context": {
        "estimated_tokens": 0,
        "budget_tokens": 0,
        "pressure": 0.0,
        "pressure_state": "ok | warning | compact_recommended | critical",
        "text_hash": "",
        "text_chars": 0
    },

    "items": [
        {
            "item_id": "ctxi_<id>",
            "kind": "message | compact_summary | context_file | profile | procedural | routing | tool | prompt | policy | ndp",
            "source": "gact | arc | prompt_registry | command_registry | ndp | policy",
            "scope": "session | workspace | user | expert | command | integration",
            "included": True,
            "reason": "recent_message | pinned_file | profile_match | compact_summary | ...",
            "dropped_reason": "",
            "tokens_estimated": 0,
            "truncated": False,
            "metadata": {}
        }
    ],

    "compaction": {
        "source_frame_id": "",
        "summary_message_id": "",
        "compacted_message_ids": [],
        "archived_count": 0,
        "evidence_index_retained": True
    },

    "tombstones": [
        {
            "message_id": "msg_<id>",
            "operation": "delete | undo | rewind",
            "created_at": 0.0,
            "reason": ""
        }
    ]
}
```

This shape should be refined during implementation, but the important
principle is stable: each model input gets one inspectable context frame.

### Storage

Store frames in ARC or beside the GACT message store under a durable session
path. The first implementation can use JSON or msgpack; the critical property
is durability and queryability by session and latest frame.

Recommended storage operations:

- `store_context_frame(frame)`
- `get_latest_context_frame(session_id, consumer=None)`
- `list_context_frames(session_id, limit=...)`
- `get_context_frame(frame_id)`

Do not block this issue on a new semantic/vector store.

## Session Scope And Cross-Session Recall

Per-session memory is the default boundary. A normal turn should assemble
context from the active session, attached files, active workspace, relevant ARC
profiles, and integration context allowed by the current expert/tool scope.

That default does not cover requests such as:

- "Based on the work from the last few days, draft the next plan."
- "Use what we learned across the previous benchmark sessions."
- "Summarize the unresolved issues from my recent CLIO work."

CLIO should support these requests through explicit cross-session recall, not
silent global memory leakage.

### Cross-Session Memory Tool

Add an internal tool-like capability available to the root orchestrator and to
other explicitly trusted agents:

```text
memory_search_sessions(query, scope, since, limit, include_archived=false)
memory_read_session_summary(session_id)
memory_read_context_frame(frame_id)
```

The first implementation can search recent session ledgers, compact summaries,
and context-frame metadata before adding a semantic/vector index. The contract
that matters first is that cross-session access is deliberate, observable, and
governed.

Suggested request fields:

- `query`: natural-language recall request.
- `scope`: `current_workspace`, `user`, `all_allowed`, or explicit session ids.
- `since`: optional time window such as `3d`, timestamp, or session count.
- `limit`: maximum sessions, frames, or summaries to return.
- `include_archived`: whether archived sessions may be searched.
- `reason`: why the orchestrator needs cross-session context.

Suggested response fields:

- matched session ids and titles,
- matched compact summaries,
- relevant frame ids,
- included item ids,
- token estimate,
- redaction/truncation state,
- permission or policy decision if one was required.

### Governance

Cross-session memory should be opt-in at the prompt/tool level and visible in
the transcript/TUI. It should not happen merely because the model asks broadly.

Policy recommendations:

- Root orchestrator may request cross-session recall.
- Child experts do not get it by default.
- User-defined experts must declare cross-session memory access explicitly.
- Workspace/user scope controls which sessions are searchable.
- Deleted, rewound, or tombstoned content remains excluded.
- Permission/audit rows should record broad cross-session recall when the query
  could expose sensitive workspace history.

### TUI Semantics

When cross-session recall is used, the TUI should show:

- that cross-session memory was searched,
- which sessions or summaries were used,
- whether the result was truncated,
- which policy allowed the access,
- an option to inspect or exclude specific recalled sessions in the future.

This keeps "based on the last few days" semantics powerful without erasing the
normal per-session compartment boundary.

## Context Pressure Policy

Default policy:

- Compaction remains manual.
- CLIO reports pressure continuously.
- CLIO recommends compaction at a defined threshold.
- CLIO does not auto-compact in this phase.

Thresholds:

| State | Threshold | Meaning |
|---|---:|---|
| `ok` | `< 75%` | No action needed. |
| `warning` | `>= 75%` | The session is approaching context pressure. |
| `compact_recommended` | `>= 90%` | The TUI should recommend `/compact`. |
| `critical` | `>= 95%` | The next turn may lose context unless compacted or scoped. |

External agents commonly use high-watermark compaction near context-window
capacity. Claude Code documentation describes `/context`, `/compact`,
automatic compaction near capacity, and configurable auto-compact thresholds.
CLIO should borrow the pressure model, but keep compaction manual until users
trust the summaries and the frame/tombstone model is stable.

Effective context budget should be:

```text
effective_context_budget =
  model_context_window
  - reserved_output_tokens
  - reserved_reasoning_tokens
  - tool/protocol overhead
  - safety_margin
```

If the provider does not report a context window, use a conservative configured
default and mark the frame as estimated.

## API And Contract Changes

Keep existing GACT fields backward-compatible.

### `/v1/memory/stats`

Continue returning the existing `MemoryStats` shape, but populate it with real
context-frame-derived values:

- `messages_retained`: messages included in the latest frame, not session
  lifetime message count.
- `tokens_retained`: estimated tokens in latest model-facing context.
- `tokens_budget`: latest effective context budget.
- `profiles_attached`: dataset/profile/context attachments included.
- `metadata`: include compact pressure state, latest frame id, model budget
  source, and compaction recommendation.

Example metadata:

```json
{
  "latest_context_frame_id": "ctxf_abc",
  "pressure": 0.91,
  "pressure_state": "compact_recommended",
  "compact_recommended": true,
  "budget_source": "model_context_window",
  "context_window": 131072
}
```

### New CLIO Endpoint

Add:

```text
GET /v1/sessions/{sid}/context/frame/latest
```

Response:

```json
{
  "frame": {},
  "summary": {
    "estimated_tokens": 12000,
    "budget_tokens": 131072,
    "pressure": 0.09,
    "pressure_state": "ok",
    "included_count": 14,
    "dropped_count": 3,
    "truncated_count": 1
  }
}
```

This can remain CLIO-specific until the GACT contract decides whether context
frames are generic.

### Compaction Response/Event

Extend CLIO compact response metadata:

```json
{
  "session_id": "sess_...",
  "compacted": true,
  "frame_id": "ctxf_...",
  "source_frame_id": "ctxf_...",
  "archived_count": 12,
  "compacted_message_ids": ["msg_..."],
  "summary": "...",
  "pressure_before": 0.92,
  "pressure_after": 0.18,
  "auto": false
}
```

The `session.compacted` SSE event should carry the same summary fields where
practical.

## TUI Behavior

The current `/memory` inspector should become a context-truth view:

- show latest frame id and consumer,
- show pressure state and recommendation,
- show effective budget and budget source,
- show included context items grouped by source,
- show dropped/truncated items and reasons,
- show compact-summary lineage,
- show prompt/expert/command provenance when available,
- show context-file snapshot metadata,
- show tombstones affecting context reconstruction.

The footer chip should remain compact:

- cache hit rate when available,
- pressure percentage when latest frame exists,
- warning color at 75%,
- recommended/critical color at 90%/95%.

When latest-frame endpoint is unavailable, TUI should keep the current
`/v1/memory/stats` fallback.

## Interaction With Other Issues

### Hierarchical Experts

Each expert invocation should get its own frame. Parent and child frames should
link through `parent_frame_id`, `trace_id`, and expert metadata. Handoffs should
record what was passed down and what was withheld.

### Prompt System

Prompt registry work should write prompt provenance into each frame:

- prompt id,
- selected profile,
- scope,
- path,
- checksum/version,
- fallback state,
- model policy.

### User-Defined Commands

Command invocations should write command provenance:

- command id,
- file/source,
- arguments,
- context mode,
- selected agent/expert,
- allowed tools.

### Cross-Session Memory Recall

Cross-session memory tool calls should write provenance:

- query and scope,
- matched sessions,
- matched summaries/frames,
- policy/permission decision,
- included and excluded/tombstoned items,
- token budget impact.

### Undo/Rewind

Undo/rewind should not physically delete ARC memory in v1. They should write
tombstones consumed by context-frame assembly. A deleted GACT transcript message
must not be included by future ARC-based context compilation.

### Permission Surfacing

Destructive context mutations, compaction, delete, undo, and rewind should use
the same permission/audit model. Context frames should include references to
permission rows when a permission decision affected the turn.

### NDP Integration

NDP memory should be scoped as integration context. Dataset/resource/catalog
rows should retain source identifiers and size/staging status. Disabled NDP
context should not be included in frames.

## Implementation Plan

### Phase 1: Frame Skeleton And Stats Truth

1. Add context-frame data types and storage helpers.
2. Build frames inside the existing context compilation path.
3. Record included/dropped/truncated items for the current ARC sources.
4. Derive `/v1/memory/stats` session fields from the latest frame.
5. Add pressure thresholds and metadata.
6. Add tests for stats truth and pressure classification.

### Phase 2: Compaction As Memory Event

1. Create a frame before compaction summarization.
2. Record compacted message IDs and source frame ID.
3. Store compact metadata durably.
4. Prefer a true `compaction` part where compatible; keep synthetic text
   fallback for existing TUI behavior.
5. Report pressure before/after compaction.
6. Test evidence retention, durable metadata, and restart behavior.

### Phase 3: Tombstones And Rollback Compatibility

1. Add tombstone/exclusion records for delete/undo/rewind.
2. Ensure context compilation skips tombstoned messages.
3. Ensure compacted summaries do not resurrect tombstoned content.
4. Test message delete and future undo/rewind paths against ARC context.

### Phase 4: Provenance Expansion

1. Add prompt provenance once prompt registry exists.
2. Add expert hierarchy provenance once expert packs exist.
3. Add user-command provenance once command definitions exist.
4. Add permission/audit references for destructive memory-affecting actions.
5. Add NDP source metadata when NDP is enabled.
6. Add cross-session recall provenance once the memory search tool exists.

### Phase 5: TUI Inspector Upgrade

1. Add latest-frame client call.
2. Extend `/memory` inspector to show frame, pressure, included/dropped items,
   compaction, tombstones, and provenance.
3. Keep current stats-only fallback.
4. Update footer memory chip to prefer pressure when available.

## Acceptance Criteria

- CLIO records a durable context frame for model-facing turns.
- `/v1/memory/stats` reflects latest retained context, not lifetime token
  totals.
- The TUI can tell the user why CLIO is near context pressure.
- Manual `/compact` reports pressure and writes durable compaction lineage.
- Deleted/rewound transcript content cannot re-enter context through ARC.
- Prompt, expert, and command provenance have defined fields in context frames.
- Cross-session memory recall is explicit, scoped, auditable, and visible.
- Existing memory stats, context compiler, compact-session, and TUI memory
  inspector tests remain green.

## Test Matrix

Backend tests:

- Empty session stats.
- Ordinary session context frame.
- Session with compact summary.
- Session with attached context file.
- Session with dataset profile.
- Session with dropped/truncated context item.
- Pressure threshold classification at 74%, 75%, 89%, 90%, 94%, and 95%.
- Compact response includes source/latest frame metadata.
- Tombstoned messages are excluded from context.
- ARC and GACT ledgers remain consistent after compaction and restart.

TUI tests:

- `/memory` renders latest frame details when endpoint exists.
- `/memory` falls back to stats-only when endpoint does not exist.
- Footer chip shows pressure states without hiding cache stats.
- Compaction marker detail remains accessible.
- Dropped/truncated items are visible in the inspector.

Regression tests:

- Existing focused tests:
  - `tests/test_gact/test_memory_stats.py`
  - `tests/test_arc/test_context_compiler.py`
  - compact tests in `tests/test_gact/test_sessions_api.py`
- Real provider benchmark case:
  - context pressure plus explicit compaction follow-up.

## Open Questions

- Should context frames store full assembled text, a redacted preview, or only a
  hash plus item-level text? Default should be hash plus item metadata, with
  optional debug text behind a config flag.
- Should CLIO expose frame history through GACT eventually, or keep it
  CLIO-specific?
- Should automatic compaction be added after this issue, or should it remain a
  separate feature once summaries and tombstones are trusted?
- What should the default context window be when a provider cannot report one?
- Should cross-session memory recall require explicit user confirmation by
  default, or should same-workspace recall be allowed automatically with visible
  audit evidence?

## Defaults Chosen

- Manual compaction only in this phase.
- Recommend compaction at 90% effective context pressure.
- Mark critical at 95%.
- GACT visible transcript remains authoritative.
- ARC storage is extended, not replaced.
- Context frames are the unit of memory truth.
- Per-session memory is the default boundary; cross-session recall is an
  explicit orchestrator capability.
