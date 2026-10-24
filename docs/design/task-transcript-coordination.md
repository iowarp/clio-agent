# Session and transcript access through Tasks

The human requested this as part of local multi-agent coordination on October 19,
2026. The misleading `memory_*` discovery/read tools are compatibility entry
points. Their capabilities belong to the existing task and conversation owners.
This change introduces no mailbox, task registry, worker or history store.

## Shared surface

- `query_tasks(..., query=None)` searches readable task descriptions and, for
  Session/Subagent assignments, retained transcript text. Scope and permission
  filtering precede matching, counts and pagination. An intent string grants no
  rights. Results carry task handles usable by the existing controls.
- Optional `message_after` (inclusive) and `message_before` (exclusive) select
  retained Session/Subagent messages by their creation instant, not the assignment
  creation/update time. Bounds require timezone-aware ISO 8601 timestamps and
  normalize to UTC for cursor identity. Windowed text matching excludes undated
  labels/descriptions and words from messages outside the window. Without query
  text, the window selects readable assignments containing any retained message
  in that interval. Without bounds, the original search and cursor contract remain
  unchanged. Agent tools and both query HTTP routes share this implementation.
- `observe_tasks(..., view="progress", history="assignment", limit=50,
  artifact_id=None)` keeps its current progress/wait/subscription behavior by
  default. `view="transcript"`, `"summary"` or `"artifacts"` provides bounded
  non-consuming reads from the same task identity. Content views return available
  data immediately; pattern waiting remains a progress-view operation.
- Assignment history stays pinned to the requested finite run. Explicit
  `history="session"` requires stable-session read rights and includes retained
  predecessor context. Exact historical grants do not expand to successors.
- Transcript pages preserve typed source messages/parts and original IDs. A large
  source row has resumable JSON fragments instead of silently discarded bytes.
  Rewritten/deleted cursor sources cause an explicit cursor-recovery error.
- The current unfinished frame is a separate bounded, mutable `live_snapshot`,
  newest parts first. Its partial text ranges are explicit. It does not enter a
  retained-history cursor until sealed, so ordinary streaming growth cannot
  invalidate that cursor. The full sealed source remains available afterward.
- Summary views return recorded model summaries and assignment outcomes with
  coverage/source references. Recent activity is labeled separately; missing
  summaries are explicit. A partial compaction is never called a whole-session
  summary. The compatibility recent-activity overview retains its separate label.
  Generating maintained long-term knowledge is separate work.
- Artifact views list versions linked to the selected assignment/transcript,
  with exact producer identity and hashes. Reading a version uses the existing
  provider/containment/hash-verification owner, with bounded pages. Knowing an
  artifact ID or sharing a workspace does not grant access to another session.
- `send_agent_message(target=handle, ...)` already uses the shared Core mailbox.
  Failed/completed child histories remain readable. Eligible messages may create
  a linked continuation in that stable session, preserving context and accepted
  operations; the historical handle/result never changes identity.

The shipped `session_search`, `session_read_summary` and
`session_read_context_frame` aliases delegate for one compatibility cycle. New
default root/child catalogs describe Tasks, not a separate memory tool family.
The HTTP compatibility routes use the same read implementation and authorization.

## Required acceptance

Construct contract tests before execution: scope/rights separation, exact-grant
boundaries, native/HTTP agreement, pagination/large typed rows, rollback/cursor
recovery, summary coverage, artifact linkage/hash/containment, non-consumption and
revocation before injection. Existing privacy assertions remain required.

Use actual Codex/Luna and Claude's distinct bridge to demonstrate two independent
sessions: discover the other through Tasks, read an early decision after unrelated
work and a service restart, use it with source IDs, inspect a summary, retrieve a
registered artifact's actual bytes/hash, message/reply while busy and idle, and
show denied access. Do not supply the answer or an exact search marker to the
reader. Also fail/interrupt a real child, inspect its transcript, message it to
continue with retained context, and prove accepted work was not replayed.

Review implementation and evidence with Claude Opus only, per the latest human
instruction. Tests and source reviews do not replace model/service acceptance.
