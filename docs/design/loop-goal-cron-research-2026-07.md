# Loop / Goal / Cron / Skill-autonomy — research survey

**Prepared for:** governance-surfaces campaign (#1057), Pillar 4 (#1079–#1082).
**Date:** 2026-07-28. **Method:** clio-codebase grounding + two cross-agent surveys (scheduling
*philosophy* and model-callable *tool contracts*), primary sources prioritized.

After governing *what* the agent may do (plan mode) and *each action* (hooks), Pillar 4 governs the
agent's **execution over time**: a self-paced **loop**, a run-until-a-predicate **goal**, and
scheduled/recurring **cron** — plus **skill-carried autonomy effects** that declare them.

---

## 1. Clio grounding (what exists)

| Capability | Verdict | Where |
|---|---|---|
| **CRON** | **PRESENT + real** | `gact/scheduler.py` (`Schedule`/`ScheduleStore`, a 5-field cron parser, `schedules.json` persisted) · `app.py:944-1301` (`_scheduler_tick` minute-aligned, `_fire_schedule` deferred-not-dropped) · `routes/schedules.py` CRUD · daemon-hosted. **Gaps:** single fixed action (post one question), no retry/backoff, no `max_fires`/`run_at`, no timezone, no cron ranges, HTTP-only (no CLI). |
| **LOOP** (self-paced) | **ABSENT** | `loop_inbox.py` is turn-*injection*, not iteration; `max_iters` (`builders.py`/`reactv2_events.py`) bounds ONE turn; `workflows.py` is a one-way DAG (no cycle). |
| **GOAL** (stop-condition) | **ABSENT** | zero symbols. Reusable: `StatePredicate` (`workflows.py:78-101`, `exists`/`equals`), `workflow_step_watch.py` progress-liveness. |
| Persistent daemon (hosts cron) | PRESENT | `gact/app.py` `_lifespan`. |
| Command surface | PRESENT | `gact/runtime/commands.py` (built-in + agent-declared + on-disk **user-defined** command-files; `GET /v1/commands`). |
| Skill effects | PRESENT | `gact/agents/skill_effects.py` (P1.0: `enter_mode`, `spawn_subagent_with_skill`). |

So: **cron = extend** (deletion-first, don't rebuild); **loop + goal = build**, reusing `StatePredicate`
+ `workflow_step_watch` + `max_iters` + the #1031 deferred-resume/idle-hook re-drive seam.

---

## 2. Cross-agent convergences (the load-bearing lessons)

1. **Two-tier scheduling** — every mature product splits a session-local/short-horizon tier (Claude
   Code `/loop`) from a durable, daemon/cloud-resident tier (Claude Routines, LangGraph Platform
   crons, Temporal Schedules). Clio's always-on daemon already is the durable tier.
2. **Cron-string internally, NL authoring on top** — store a 5-field cron; author from "every 5
   minutes" and **echo back exactly what was chosen** (never silently reinterpret). (Claude Code,
   Devin, n8n, LangGraph.) OpenAI ChatGPT Tasks is the outlier (RRULE).
3. **Hard clamps = the #1 anti-runaway mechanism** — a min-interval floor + a max-lifetime ceiling
   (Claude `/loop`: 1-min min, 7-day expiry). Deterministic infra, not a model decision.
4. **Deterministic jitter** — ID-derived fire-time offset to avoid a thundering herd against a shared
   provider quota (Claude Code, Gemini Enterprise).
5. **Goal eval is two-philosophy → use the hybrid** — LLM-judged-against-transcript (Claude `/goal`,
   Haiku votes each turn, *cannot call tools*) vs deterministic state-machine (ADK
   `current_step==COMPLETED`). The community consensus and clio's ⚑ RULE 1 both demand the hybrid:
   an LLM verifier as a fast first pass, a **deterministic hard gate** as the actual halt authority.
   **Never LLM-only for a consequential halt** — it lets the model mark its own homework.
6. **First-class typed budget bounds, not prose** — Claude's `/goal` punts "…or stop after 20 turns"
   into the condition text; that is the anti-pattern. Make `max_iters`/`max_tokens`/`max_wallclock`
   typed fields with a structured reason on trip (clio's `stream_fallback` catalog style).
7. **Cross-run memory as an explicit typed setting** — LangGraph thread-bound-vs-stateless crons,
   Manus continue-vs-fresh, Devin cross-run notes. Clio's ARC is well-positioned.
8. **Default-cautious, opt-in-unattended** permission posture (Claude auto-mode, Manus skip-confirm,
   Gemini people-affecting gate).
9. **Failure-only notification default** (Devin), success logged not pushed.
10. **Event-driven wake > polling** where a signal exists (Claude Monitor tool, ADK webhook).

## 3. Tool-contract convergence (the model-callable interface)

The field exposes scheduling/looping as **first-class model tools**, in a recurring shape:
- **Cron = a `create / list / delete` triad** — Claude Code `CronCreate({cron, prompt, recurring})`→
  **stable id**, `CronList()`, `CronDelete({id})`; mirrored by mcp-cron `add_task/list_tasks/
  remove_task`. `recurring:false` = one-shot auto-delete. The **list-back tool** prevents double-arming.
- **Loop = a self-pace tool** — `ScheduleWakeup({delaySeconds, prompt, reason, stop})`: reschedule
  yourself with a delay + reason, or `stop:true`. **Explicit `stop` field**, not "absence = stop"
  (the pre-v2.1.202 implicit version is the worse design), plus a **bounded fallback** (~20 min).
- **Monitor = an event-wait tool** — stream stdout/ws frames as events; the polling alternative.
- **Task family** — `TaskCreate/Update/Get/List/Stop` with `blocks`/`blockedBy` deps (a richer
  superset of a flat todo list).
- **Goal is deliberately NOT a model tool** — `/goal` is a user command → Stop-hook; **no surveyed
  system exposes a model-callable `set_goal`**. A self-armed halt is the self-grading anti-pattern.
- IDs are **server-generated, returned in the result only** (never echoed from input).

Scheduling stays host/infra/UI-side in OpenAI (API/Agents SDK), Temporal, ADK, Manus, Devin, CrewAI,
n8n — only Claude Code + MCP-cron expose the full model-callable set.

### Pitfalls
Unbounded self-reschedule with no externally-enforced ceiling (the Claude Code runaway-spend
issues); LLM-only goal eval; short-lived creds behind "durable" schedules (Gemini 14-day refresh
trap); **cancel-foreground not canceling the background schedule** (the `ScheduleWakeup`-survived-
Ctrl+C incident — a daemon architecture like clio's is *more* exposed); recursive subagent fan-out
inside a loop; undocumented overlap policy.

---

## 4. Recommended clio design

Three doors per capability; the command and skill catalogs stay **separate**; clio is the
deterministic enforcer (⚑ RULE 1: the model decides via tool/structured-output, clio gates on
reality, never fabricates the decision). Full per-capability design is in the issues:

- **#1079 loop** — `/loop` command · `loop_wakeup(delay_seconds, prompt, reason, stop)` tool · `loop`
  skill-effect · infra = re-drive + typed budget/iter/stall clamps + bounded fallback + **cancel-both**.
- **#1080 goal** — `/goal` command · **read-only `goal_status()` only** (no `set_goal` tool) · `set_goal`
  skill-effect · infra = the two-tier gate (LLM first-pass + deterministic hard gate) + typed bounds.
- **#1081 cron** — `/cron` command · `cron_create/list/delete` triad + `monitor_watch` tools · `schedule`
  skill-effect · infra = extend `scheduler.py` (NL→cron, clamps, `run_at`, timezone, retry, cron+goal,
  jitter, cancel-both). **Windows-cron is a hard live gate.**
- **#1082 skill-autonomy-effects** — generalize P1.0 (`loop`/`set_goal`/`schedule`/`plan_workflow`/
  `plan_small`) as declared, injection-safe effects over the same primitives.

### Universal safety properties (from the survey)
Server-generated stable IDs (result-only) · explicit `stop`/`recurring` booleans (not
"absence=stop") · a list-back tool for every stateful category (avoid double-arming) · hard
min-interval + lifetime clamps · a bounded fallback (never an unbounded wait) with a structured
reason · deterministic goal hard-gate · cancel-foreground ⇒ cancel-background · failure-only
notification.

## Sources
Claude Code docs (scheduled-tasks, /goal, routines, agent-sdk) · live tool schemas (CronCreate/List/
Delete, Monitor, Task*, ScheduleWakeup) · OpenAI (function-calling, background, Agents SDK) · Google
(ADK long-running agents, Gemini Enterprise schedule) · Devin/Cognition · Manus · LangGraph SDK
CronClient + docs · Temporal (schedules, continue-as-new, signals) · MCP-cron/scheduler-mcp · n8n ·
Microsoft Agent Framework. Community-reported: anthropics/claude-code runaway-spend issues.
