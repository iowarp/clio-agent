# Long-horizon conversation controls: bounded live acceptance

Date: October 11, 2026. Base: develop `d66de87a7a4c00e4160ef1445c8e7e3b049d56a0`.
Scope: ordinary conversations, goals, loops, scheduler admission, limits and recovery.
Task/subagent ownership and scientific optimization are excluded.

## Acceptance result and its limits

Real Codex `gpt-6-luna` calls, real artifact storage and the production browser UI
were exercised on Windows with Python 3.14.6 and a private clio-core. No route,
model response, tool result, clock or screenshot content was replaced by a fixture.
All checkpoint data are fictional and public-safe. No OPAL data were used.

The goal completed three actual checkpoint writes in 32 seconds. A paced loop
survived a backend restart, executed two scheduled batches and completed in
238 seconds, cancelling its next wakeup. These are bounded functional acceptance
results, **not a 48/72-hour soak or a claim of scientific validity**. The documented
multi-day commands parse and resolve their requested limits; they were stopped
without launching a multi-day job.

Evidence is retained at `C:/clio-relationships-20261011/long-horizon/`.
Each case has exact HTTP requests/responses, timestamps, session IDs, verdicts and
transcripts. The service's restart records retain source identity. Full browser
originals are under `captures/originals/`; publication crops are separate files.

| Evidence directory | Observed outcome |
|---|---|
| `goal-01` | One checkpoint per real turn, two immediate goal continuations, three successful artifact writes, `goal_met` |
| `paced-04` | Original pending schedule survives a process restart; two scheduled turns, two successful files, `goal_met` and `loop_goal_met`; no pending wakeup |
| `stop-01` | Composer command route resolves 72 iterations and 259,200 seconds; user stop clears paired work and its schedule |
| `budget-01` | Real model turn exceeds a one-token goal bound; `goal_budget` and `loop_goal_capped`, without another judge call |
| `expiration-03` | A one-second loop budget expires during a real scheduler wait; no model turn is staged |
| `goal-expiration-01` | A one-second goal budget expires before the longer loop budget; no model turn is staged; both controls stop |
| `interrupted-01` | Restart during an accepted real turn yields `goal_server_interrupted` and `loop_server_interrupted`; no replay or pending schedule |
| `question-04` | Base Agent's actual blocking `ask_user` holds `waiting_user` across a due wakeup; no scheduled turn is delivered over it |
| `iterations-03` | An unmet goal permits exactly one additional continuation, then stops with `goal_max_iters`; two real model turns and no third turn |
| `overdue-02` | Backend and private core are stopped until after the wakeup is overdue; cold startup restores model readiness, delivers the original schedule once, saves `overdue.md`, and stops with `goal_met` / `loop_goal_met` without a startup turn error |

`artifact-verification.json` records an independent byte fetch for all five output
files. All sizes and SHA-256 hashes match their resource metadata. Contents record
the correct fictional checkpoint numbers. The first paced batch recovered from
an incidental failed history lookup before writing its output; that failure is
retained in the transcript, not edited out of the evidence.

## Defects found and fixed

- Normal startup persisted sessions but kept schedules only in memory when no
  explicit `sessions_path` argument was passed. Schedules now use the resolved
  session-store path, including the environment-configured normal entry point.
- An unmet goal immediately continued even with a waiting loop. Goals now respect
  loop pacing, and either control's limit stops the paired work.
- A wakeup could cross its loop or goal budget during a wait. Admission checks the
  actual current budgets before staging; a cleared/replaced goal cannot revive
  from a stale inbox event or a judge response arriving later.
- Interrupted controls could remain active without recoverable work. Startup
  preserves only a durable waiting loop; interrupted work stops explicitly.
- A failed/unavailable judge could spend more turns without a completion check.
  Evaluation now has a 60-second timeout and a typed stopped outcome.
- Generic presentation titles such as “Created version 1” hid the artifact names
  needed by the judge. Precise structured results take priority. The judge receives
  chronological recent evidence, bounded to 40 messages and 32,000 characters.
- Exhausted staging retries could leave a loop labelled active. They now stop
  paired work with `loop_schedule_failed`.
- Transient startup storage failure delays and retries reconciliation with an
  explicit error, before firing work, instead of killing the scheduler task.
- An overdue wakeup could start before the runtime host was ready, leaving the
  completion judge without an owning model. Admission now waits for readiness
  and provider binding. A scheduled session's saved model can restore the host
  when no global provider is configured; that path also has focused coverage.

Earlier failed receipts remain: `paced-01` exposed schedule persistence;
`paced-02` exposed missing artifact facts; `paced-03` correctly rejected overwriting
retained files and prompted separate physical demo workspaces. `expiration-01`
timed out during an unnecessary provider rebind, removed from the driver;
`expiration-02` incorrectly counted a synthetic command result as a model turn.
`question-01` used a tool-less built-in main instead of activating Base Agent;
`question-02/03` exposed errors in acceptance setup, before any question was sent.
`iterations-01/02` received typed `503 starting` refusals before model readiness;
the final driver waits and retries only that unaccepted starting request.
`overdue-01` exposed the real startup admission defect described above.
These failures are not presented as successful live checks.

## Automated and browser checks

- 187 focused pytest tests passed: goals, loop controls, scheduler/cron, inbox,
  off-loop judge/finalization and new admission/restart integration cases.
- All 1,168 source files passed Mypy using `--platform linux`, matching CI's OS.
  Native Windows full-tree checking reports existing POSIX-only API errors in
  unrelated infrastructure modules. Tests retain existing deprecation and native
  binding teardown warnings; warnings are not represented as failures.
- Ruff lint/format and the file-size, class-in-function, silent-fallback and
  per-file hidden-swallow guards passed without raising a baseline.
- Website check: zero errors/warnings (six existing hints). Build and internal
  link validation passed. Two Playwright cases passed at 390 and 1440 pixels,
  loading and enlarging every capture, restoring keyboard focus and checking
  horizontal overflow. Rendered screenshots were inspected.
- Production composer browser acceptance sent actual `/goal` and `/loop` commands,
  verified resolved limits from the service and inspected Work. Its one-hour demo
  was explicitly stopped after capture; the browser always closes.

The user guide is `site/src/content/docs/docs/long-running-work.mdx`, linked from
Sessions and modes and the Use CLIO sidebar. It explains commands, defaults,
limits, judge limitations, pacing, human input, stopping and restart recovery with
three reviewed captures. Public publication follows the normal website release;
source on develop must not be described as already deployed.

## Reproduce overdue restart acceptance

Use `scripts/live_verification/conversation_control_server.py` with a fresh bounded
evidence directory on the chosen drive, and wait for startup. With that service
running, execute `scripts/live_verification/overdue_loop_restart.py prepare
--out <case-directory> --service <service-directory>` using `uv run --no-sync python`.
Preparation saves the original schedule and stops the owned service and core.
Wait at least 70 real seconds, resume the service with `--resume`, then run the
same verifier with phase `verify`. Neither clocks nor responses are replaced.
Verification retains the recovered transcript, checks single delivery and actual
goal/loop completion, and stops the owned service in its finalizer. Each phase
refuses to overwrite earlier evidence.

## Remaining qualification

A real elapsed multi-day soak still needs to demonstrate repeated checkpointing,
bounded resource growth, provider/authentication continuity, context retention,
extended host availability and clean shutdown on the intended deployment host.
The current implementation permits multi-day plans but does not establish those
environmental guarantees. Limits are checked between turns; they cannot interrupt
an individual tool operation at an exact monetary or time boundary. Session token
and cost rollups are not a billing guarantee, especially for secondary judge calls.
