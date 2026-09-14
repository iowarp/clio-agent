# Factorio Flat black-box evals

`behavioral-cases.json` contains user messages and semantic expectations. An
external adapter should send each message to an activated `factorio-flat`
session and normalize the public response, the observable tool trace, and the
runtime state that trace produced. Every field below is a real runtime shape;
all five keys are required, because a missing key is not an empty one and the
evaluator rejects a trace it cannot read rather than grading it as compliant.

```json
{
  "case_id": "case id",
  "response": "public assistant response",
  "actions": [
    {"name": "tool name", "arguments": {}, "result": {}}
  ],
  "tasks": [
    {
      "task_id": "agent task id",
      "agent": "child expert id",
      "child_session_id": "the child's session id",
      "status": "queued | running | completed | failed | cancelled",
      "error_reason": "typed reason, empty when none"
    }
  ],
  "questions": [
    {
      "id": "question id",
      "session_id": "session the row lives on",
      "owner_session_id": "session that asked (the CHILD, when forwarded)",
      "attended_session_id": "session the scientist is attending",
      "status": "pending | answered | cancelled | expired",
      "source": "orchestrator | child_forwarded",
      "prompt": "question text",
      "metadata": {}
    }
  ],
  "sessions": [
    {"session_id": "session id", "status": "status observed during the run"}
  ]
}
```

Action results keep their runtime shapes: `spawn_agent_task` returns one handle
(`task_id`, `status`, `run_index`, `queued_reason`), `spawn_agents_parallel`
returns those handles under `spawned`, `wait_agent_tasks` returns full-fidelity
rows under `results`, and `observe_agent_tasks` returns incremental event snapshots
under `tasks`. The evaluator still understands historical `check_agent_tasks`
records so saved qualification traces remain readable; that tool is no longer in
the model inventory.

There is no paused task status. A child that needs a scientist-owned decision
keeps status `running`. The order is: the child's own turn asks and goes
`waiting_user`, and that status is what triggers the runtime to mint a forwarded
question on the attended root whose `owner_session_id` is the child session —
nothing in the forward path sets the child's status. The evaluator recognizes a
blocked consultation by that conjunction, attributing it to a task by joining the
question's `owner_session_id` to the task's `child_session_id`.

## Rules that reject a trace outright

These are integrity rules, not case expectations: they apply to every case, and
an adapter that violates one produces a trace the grader refuses rather than
grades. Most describe states the runtime cannot actually reach, so tripping one
means the capture is wrong.

- every one of the five top-level keys is present and of the right type, and the
  nested `spawns` / `spawned` / `results` / `tasks` lists hold only objects;
- a task's `status` is one of the five above and a question's is one of the four;
- `task_id` is unique across `tasks`, and each row carries `agent`,
  `child_session_id`, and a status;
- every `sessions` row carries a non-empty `status`, and every task's
  `child_session_id` appears there — this is what makes `sessions` load-bearing
  wherever children ran, rather than a key nothing reads;
- every task id a spawn returned appears in `tasks` with the agent that spawn
  requested, and every task in `tasks` was produced by a spawn call in the trace
  (the spawn call is the only place the requested agent appears, so both
  directions of that join are needed);
- `spawn_agents_parallel` returns exactly one handle per requested spawn — a
  refused spawn is a handle carrying `error`, not an omission;
- a forwarded question's `owner_session_id` is some spawned child's session;
- a forwarded question that is not `answered` never sits under a `completed`
  task: a declined forward relays down and an unattended one expires, and both
  fail the bound task.

## What the cases assert

The evaluator never reads pack prompts and does not compare exact prose. It
checks response floors and ceilings, the capabilities a case genuinely needs,
fan-out width, the exact agents consulted, whether the consulted children
actually completed and were collected, whether the answer carries what they
returned, and blocked-child resume continuity.

It asserts outcomes only: no forbidden-tool lists, no call caps, and no required
tool ordering. A case that must be answered directly says so as two outcomes —
no child ran, and the attended session put no question of its own to the
scientist. That second one is asserted on the question row EXISTING, in any
status: the runtime stamps `expires_at` on every ask, so an ignored deflection
expires rather than staying pending, and a status-sensitive rule would let the
same deflection through once its TTL lapsed.

Three limits worth stating plainly rather than implying:

- there is no cap on how many clarifications a turn may ask — how many a
  decision needs is the agent's call — so beyond "it has a question and a stated
  consequence" the only question rule is that the same question is not asked
  twice, and that compares normalized literal text. Two rewordings of one
  question pass it;
- the grounding floor counts distinct shared terms of six or more characters
  between the answer and each child's returned output, minus what the scientist
  already supplied. It is a floor on carried information and cannot tell a
  synthesis from a verbatim copy of one child's result;
- everything above is validated against the hand-written fixture in this
  repository. Judging whether an answer reasons — and confirming these rules
  against traces a real session produces — needs live captures.

Evaluate a captured result file with:

```powershell
uv run --no-project python scripts/evaluate_factorio_flat.py `
  factorio-flat/evals/behavioral-cases.json captured-results.json
```

The repository test fixture contains representative compliant traces plus
negative evaluator tests. It validates the grading contract in CI; replacing it
with traces from a deployed runtime provides live behavioral qualification.
