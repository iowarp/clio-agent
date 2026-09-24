# CLIO Cancellation Semantics

CLIO exposes session cancellation through `POST /v1/sessions/{sid}/cancel`.
The endpoint is intentionally truthful about what it can and cannot stop.

## Levels

`turn_boundary`

- Cancellation was already requested before provider or tool work started.
- CLIO skips the agent turn.

`turn_cancelled_during_prologue` (L1, #1339 follow-on)

- Cancellation landed while the turn's off-loop prologue (transcript persist,
  `turn.started`, enrichment/memory-search, context frame, `UserPromptSubmit`
  hooks) was still running. The prologue checks the turn's cancel token before
  every step and stops immediately — no further semantic events, no further
  RPCs, and an already-running `UserPromptSubmit` hook subprocess is killed
  (its whole process tree). `error_info.error="turn_cancelled_during_prologue"`.

`cooperative`

- The active agent or MCP bridge observed CLIO's cancellation checker at a
  safe boundary and returned a structured cancelled turn.
- CLIO has evidence that the observed boundary stopped normal turn progress.

`best_effort`

- CLIO signaled the cooperative checker and cancelled the asyncio task wrapper,
  but provider or tool work may already be running inside an executor thread or
  upstream service that cannot be forcibly interrupted from CLIO.
- The GACT turn settles promptly as `error_info.error="cancelled"`.
- Clients must not treat this as proof that upstream provider/tool execution
  stopped.

`hard`

- Reserved for a future provider/tool path with proven upstream abort support.
- CLIO does not advertise this today.
- This is not a release blocker for the current backend: CLIO closes the issue
  by documenting and testing truthful best-effort semantics rather than claiming
  unproven upstream abort.

## Wire Evidence

Cancelled assistant messages include:

- `error_info.error` — `"cancelled"`, or `"turn_cancelled_during_prologue"` /
  `"turn_prologue_never_ran"` for the two prologue-boundary cases above.
- `error_info.details.execution_cancellation`
- `error_info.details.cancellation_attempt`

The `cancellation_attempt` object records what was ACTUALLY stopped (L1: the
old constant `hard_abort_supported=false` / `upstream_abort="not_supported"`
pair, and the `executor_work_may_continue` flag — redundant everywhere with
`execution_cancellation` — are deleted; nothing replaces a fabricated fact with
another one):

- `id`
- `session_id`
- `requested_at`
- `in_flight`
- `cooperative_signal_sent`
- `asyncio_task_cancel_scheduled`
- `asyncio_task_cancel_sent`
- `children_cancelled` — count of descendant agent-task turns cancelled
- `provider_streams_killed` — count of in-flight SDK streams aborted
- `composer_autostart_suspended` — whether pending steers/queued messages were
  suspended from auto-promoting

This makes post-hoc inspection possible after transient SSE events are gone.

## Current Contract

`/v1/capabilities` advertises:

- `x_clio_cancellation="best_effort"`
- `x_clio_executor_cancellation=false`

That means CLIO can settle the user-visible GACT envelope as cancelled and can
prevent stale successful results from entering the session ledger, but it does
not claim proven hard upstream abort.

## Closure Evidence For Hard-Abort Follow-Up

Tracking issue: https://github.com/iowarp/clio-agent/issues/283

The final backend contract is intentionally **best-effort**, not hard abort:

- executor-thread work may continue after the visible GACT turn has settled as
  cancelled;
- late tool completions after cancellation are rewritten as unsuccessful
  cancellation telemetry, not stale success metadata;
- pre-turn cancellation skips provider/tool execution entirely;
- a cancel landing during the off-loop prologue stops it at its next
  cooperative checkpoint (never emitting a later step) and kills an
  already-running `UserPromptSubmit` hook subprocess outright;
- cooperative agents that accept a cancellation callback can stop at safe
  boundaries and report `execution_cancellation="cooperative"`;
- every cancelled turn carries durable cancellation-attempt evidence naming
  what was actually stopped (children/streams/composer autostart) — never a
  constant placeholder.

Regression coverage lives in `tests/test_gact/test_cancellation.py` and
`tests/test_gact/test_finalize_error_envelope.py`.
