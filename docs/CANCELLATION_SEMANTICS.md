# CLIO Cancellation Semantics

CLIO exposes session cancellation through `POST /v1/sessions/{sid}/cancel`.
The endpoint is intentionally truthful about what it can and cannot stop.

## Levels

`turn_boundary`

- Cancellation was already requested before provider or tool work started.
- CLIO skips the agent turn.
- `executor_work_may_continue=false`.

`cooperative`

- The active agent or MCP bridge observed CLIO's cancellation checker at a
  safe boundary and returned a structured cancelled turn.
- CLIO has evidence that the observed boundary stopped normal turn progress.
- `executor_work_may_continue=false`.

`best_effort`

- CLIO signaled the cooperative checker and cancelled the asyncio task wrapper,
  but provider or tool work may already be running inside an executor thread or
  upstream service that cannot be forcibly interrupted from CLIO.
- The GACT turn settles promptly as `error_info.error="cancelled"`.
- `executor_work_may_continue=true`.
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

- `error_info.error="cancelled"`
- `error_info.details.execution_cancellation`
- `error_info.details.executor_work_may_continue`
- `error_info.details.hard_abort_supported`
- `error_info.details.upstream_abort`
- `error_info.details.cancellation_attempt`

The `cancellation_attempt` object records:

- `id`
- `session_id`
- `requested_at`
- `in_flight`
- `cooperative_signal_sent`
- `asyncio_task_cancel_scheduled`
- `asyncio_task_cancel_sent`
- `hard_abort_supported`
- `upstream_abort`
- `executor_work_may_continue`

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
- cooperative agents that accept a cancellation callback can stop at safe
  boundaries and report `execution_cancellation="cooperative"`;
- every cancelled turn carries durable cancellation-attempt evidence with
  `hard_abort_supported=false` and `upstream_abort="not_supported"`.

Regression coverage lives in `tests/test_gact/test_cancellation.py`.
