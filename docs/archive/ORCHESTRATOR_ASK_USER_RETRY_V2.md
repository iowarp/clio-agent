# Orchestrator Ask-User And Retry V2

Tracks GitHub issue #370.

## Goal

Make asking the user a native orchestrator action, not just a backend waiting
state. The agent should pause when information is missing, ask a structured
question, resume with the answer, and preserve provenance. Retry should support
notes and honest model/provider override behavior.

## Planner Action

Add an orchestrator action such as:

```json
{
  "action": "ask_user",
  "question": "Which dataset should I analyze?",
  "choices": [
    {"id": "a", "label": "fusion_run.h5"},
    {"id": "b", "label": "facility_measurements.parquet"}
  ],
  "allow_freeform": true,
  "reason": "missing_target_dataset"
}
```

The session moves to `waiting_user`. No further model/tool work runs for that
turn until the question is answered or cancelled.

## Resume Semantics

Answering a question resumes the same session. The resumed context includes:

- question id
- question text
- answer text/choice
- caller agent/expert
- original turn/message ids
- timestamp and status

Cancelling a question leaves the session coherent and records the cancellation
as recoverable metadata.

## Retry Semantics

Retry supports:

- retry with notes
- retry from a target message/turn
- optional provider/model override

Provider/model override must either be applied to the actual execution or
rejected with a structured unsupported-policy error. It must not merely store
metadata while running the original model.

If override is accepted, metadata records that KV/cache reuse may be invalid and
TTFT/cost may increase.

## APIs

Existing question/retry endpoints remain, but implementation must connect them
to orchestrator actions:

- planner/action schema supports `ask_user`
- session question endpoints create/answer/cancel/list questions
- retry endpoint records notes and applies/rejects model override honestly
- SSE/event bus emits question created, answered, cancelled, and resumed events

## Acceptance Criteria

- Planner can emit `ask_user` during orchestration.
- Session enters `waiting_user` and resumes on answer.
- Answer provenance appears in resumed context and turn metadata.
- Cancel leaves a coherent recoverable state.
- Retry with notes executes a new attempt with notes in context.
- Retry with provider/model override either executes with override or returns a
  structured unsupported-policy error.
- Tests cover ask, answer, cancel, resume, retry notes, retry override, and
  provenance.

