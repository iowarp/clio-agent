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
four reviewed captures. Public publication follows the normal website release;
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

## Initial soak and recorded runtime

The initial 48-hour attempt ran on `homelab` in the isolated user unit
`clio-long-horizon-20261011.service`. The service uses the merged `develop` source
`5a92bd80354f5658f7cbe945763bf41df4960abb`, Python 3.14.6, clio-core 2.3.1 and real
Codex/Luna authentication. Its evidence root is
`/home/jcernuda/.local/share/clio-qualification/long-horizon-20261011`.
The installed service on port 17800 and the task-system qualification are untouched.

`scripts/live_verification/long_horizon_soak.py` sends one initial instruction,
then independently observes production goals, loops and schedules. Acceptance
requires 49 distinct saved checkpoints: one initial checkpoint and 48 hourly
continuations over at least 48 real hours. The observer checks contents, marker
retention, file hashes, unchanged previous outputs, minimum spacing, final manifest,
goal completion and no pending wakeup. It does not send the later turns itself.

Systemd enforces one CPU, 3 GiB memory, 384 tasks and a 54-hour deadline. The task
ceiling was raised from 192 before the first scheduled continuation, after the
separate read preflight measured 191 native tasks. This changed neither the
acceptance clock nor the session's work. The observer
also stops on more than 80 descendant processes, 4 GiB of allocated run files, less
than 10 GiB free disk, excessive measured RSS, a stalled checkpoint, or a request
for human intervention. These limits cover this qualification; they are not claims
that CLIO automatically enforces equivalent host limits for every user deployment.

The main Python runtime is an isolated hardlink snapshot, including its interpreter,
and uses no new Git worktree. MCP preparation caches also stay inside the bounded
run directory. The actual dependency
receipt records one lock deviation: `orjson` 3.13.0 supplies a Python 3.14 wheel,
whereas frozen sync could not build locked 3.11.3 on this host. All other installed
locked package versions match. This qualifies that recorded runtime, not every
possible installation. Early unaccepted startup attempts are retained separately;
their two empty 512 MiB core files were removed after shutdown was confirmed.

Its acceptance clock started on 2026-10-11 at approximately 21:49 UTC. This attempt
was explicitly superseded and stopped at approximately 22:42 UTC, with one verified
checkpoint, to qualify the newly discovered fixes and mandatory independent audit
on a fresh clock. **No multi-day pass is claimed.** Its elapsed time is not carried
forward. The compact retained archive has SHA-256
`90ce10fef0e719c7b9fa8555ed2764f7a26cd1ce45817d74ef4c588c98d0a952`;
the C: copy was independently checked before disposable state cleanup.
The initial checkpoint has passed independent byte/hash verification, with the goal
active and an hourly wakeup persisted. A separate real conversation successfully
used `fs_read_file` to read that checkpoint and saved its actual fields in an artifact.
Five evidence-verifier tests, Ruff and scoped Mypy passed. The unrelated declared
web MCP mount was degraded during startup; this run does not qualify web tools.

A real elapsed multi-day soak still needs to demonstrate repeated checkpointing,
bounded resource growth, provider/authentication continuity, context retention,
extended host availability and clean shutdown on the intended deployment host.
The current implementation permits multi-day plans but does not establish those
environmental guarantees. Limits are checked between turns; they cannot interrupt
an individual tool operation at an exact monetary or time boundary. Session token
and cost rollups are not a billing guarantee, especially for secondary judge calls.

## Extensive intervention and evidence audit

The October 11 follow-up adds an independent transcript audit in
`scripts/live_verification/long_horizon_evidence.py`. A partial audit always reports
`qualified=false`. A complete audit requires 48 distinct scheduled continuations,
one real uncached previous-file read and one fresh checkpoint write per turn,
matching tool-result and filesystem hashes, at least one hour between actual write
completions, 49 unchanged ordered files, 48 elapsed hours, the final manifest and
fully settled goal/loop state. Failed, cached, unobserved, duplicate, early or manual
substitute work cannot produce a pass. Thirty-two tests cover positive evidence
and rejection cases; none accelerate the live acceptance clock.

The read-only independent verifier must pass with `--complete` before reporting
qualification. The first independent audit found one valid initial checkpoint and
zero scheduled continuations, correctly reporting partial progress. The new
observer requires both wall time and monotonic elapsed time to reach 48 hours.
Wakeup revisions within one turn are allowed, provided they preserve the full
hourly delay and the final wake follows the write; duplicate file effects fail.
An optional successful uncached `goal_status` query is allowed. It cannot replace
a required previous-file read or checkpoint write; that rejection has a regression
test. No other additional tool is allowed by this qualification.

Additional real-model intervention cases use a separate bounded service on homelab,
port 17964, with its own home, 256 MiB private core and workspace. They reuse the
existing interpreter read-only and create no worktree or dependency environment.
Each case retains requests, transcript, control state, saved bytes and SHA-256s.
The original run is `/home/jcernuda/.local/share/clio-qualification/extensive-faults-20261011`.

The first iteration-cap case failed: the model called `loop_wakeup` twice during
one actual turn, spending a two-iteration allowance after creating only
`loop-1.md`. The runtime stopped safely, but repeated rescheduling should replace
one pending wakeup without consuming another iteration in that same turn.
`last_reschedule_turn_id` now records the real execution context identity and
counts once per turn. Every call still checks time, token and cost budgets, and
sticky stopped loops remain stopped. The failed receipt and original driver are
preserved; this is not counted as passing validation.

The question-choice schema also exposed a real defect. Its generic object items
gave the model no label/value properties, resulting in repeated empty options.
The schema now describes the choice fields and requires a nonempty label; the
existing runtime still defaults an omitted value to the label. A schema regression
test and a fresh live run verify the change.

| Intervention | Live evidence and result |
|---|---|
| Clear goal, then replace a loop | Passed: one replacement wake, only `new-output.md` with `NEW-ONLY`; no old delivery or additional delivery during a 70-second hold |
| Cancel an active writing turn | Passed: only the first actual file remained after an 80-second hold; goal/loop inactive and no pending wake |
| Restart during active writes | Passed: previous bytes preserved; only one additional in-flight write completed; explicit server-interrupted controls and no replay |
| Revise wakeup twice in one real model turn | Passed: two actual calls, one iteration consumed, exactly one revised pending wake |
| Block on human input past the wake time | Fresh patched run passed in 233 seconds: 125-second actual wait, no scheduled bypass, actual Blue answer, `HUMAN-BLUE` bytes, `goal_met` and no wake after another 70 seconds |
| Loop iteration cap | Original run exposed double counting. Later file-task cases stopped for missing procedure, voluntarily stopped, or reached `loop_max_iters` at two distinct turns while omitting the second requested file. Whole file-task cases remain failed. A separate control-only case preserves the file-task assertions and independently requests ten wakeups against a two-iteration runtime cap |
| Real provider outage and manual recovery | Outage phase passed: refused outbound connections, recorded failed work, `goal_judge_unavailable`, inactive loop and no wake. First recovery retained the probe's no-tools restriction. A later recovery attempt was invalidated by the evidence-script error described below. A fresh frozen source reruns the whole outage/recovery case |

The frozen attempts are named `extensive-faults-20261011`,
`extensive-fixed-20261011`, `extensive-fixed-02-20261011` and
`extensive-fixed-03-20261011` and `extensive-fixed-04-20261011`. Each uses a separate physical workspace, and every
failed receipt is retained. The first three units are stopped; compact C: archive
hashes were checked before removing their disposable homes/private cores. The
fourth is stopped; the fifth runs only the control-cap and outage/recovery cases. It does not contribute
to uninterrupted soak duration. Actual service source files and harness hashes
are recorded, including revisions to acceptance instructions.

The final focused suite passed **273 tests with no skips** across goals, loops,
rescheduling identity, scheduler/cron, cancellation, question answering and evidence
verification and cold startup. After the read-only status-query clarification,
the independent audit's **32 tests**, plus five filesystem/manifest checks passed.
Ruff, four architecture guards and Node syntax checks pass. Website check
reports zero errors/warnings (six existing hints); the static build and internal
links pass. Two responsive browser cases load and enlarge all four real captures,
restore focus and check overflow at 390 and 1440 pixels. The question image is a
reviewed crop of the fresh accepted run, showing actual choices and Waiting for you.

The wider Mypy attempt including existing test helpers exposed existing errors in
`tests/_cte_isolation.py` and `scripts/live_verification/_common.py`; the existing
question test file also has pre-existing TestClient application typing errors.
These are not hidden or counted as passed checks. Source-only CI-style checking
and direct checking of the new typed files are recorded separately.
All 1,168 source files passed CI-style Linux Mypy; the subsequent SDK import and
cold-start regression passed a separate direct two-file check. Five new/changed
verifier files passed scoped checking with imported baseline diagnostics excluded.

A fresh 48-hour attempt uses the committed patched source and new mandatory
audit observer on a new elapsed clock. Its token ceiling is four million: the first
real Base Agent batch used about 39,000 input tokens, so the earlier one-million
cap could prevent 49 valid batches. Iteration, time, process, memory and disk bounds
remain explicit. The wakeup preserves the complete original procedure rather than
replacing it with an under-specified “continue” instruction. This setup change
does not turn partial evidence into a multi-day reliability claim.

The first patched soak startup (`long-horizon-20261011-02`, source `1e5376bf`)
failed before its initial request was accepted: deferred preflight reported a
missing `openai.Omit`, although an independent import in the same pinned runtime
successfully exported it. DSPy's lazy loader publishes a real but partially loaded
SDK module outside Python's standard import lock, so a concurrent LiteLLM importer
can observe an incomplete namespace. The GACT module now materializes OpenAI before
starting background workers, alongside its existing numpy/pyarrow preloads. A cold
subprocess regression requires a complete SDK before eight concurrent real
DSPy/LiteLLM imports. The pre-fix probe failed that condition. No SDK files or
installed dependencies were changed. The failed startup and stopped-service receipt
are retained; its clock is discarded.

The following startup (`long-horizon-20261011-03`) successfully accepted a real
turn, wrote the correct initial checkpoint and armed an hourly wake. Its independent
audit stopped the run because it also called the read-only `goal_status`. The
qualification now explicitly permits that successful status query while preserving
all mandatory read/write and pacing assertions. Its failed receipt is retained,
and its clock is discarded before starting the clarified qualification.

An operator error invalidated recovery in `extensive-fixed-03-20261011`: the
one-off source-receipt script reused a path variable after an iteration and wrote
JSON into the frozen `prompt_packs/builtin/__init__.py`. Subsequent recovery failed
to load the shared prompt. This was a verifier mistake, not a missing packaged
prompt or a product recovery result. The corrupted file, expected and actual source
hashes, failed case and stopped-service receipt are preserved. No local production
source or installed dependency was changed. The next run comes from a fresh Git
archive, never from the damaged tree.
