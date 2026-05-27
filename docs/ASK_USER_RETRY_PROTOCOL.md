# Agent User-Question And Retry Protocol

## Purpose

Add a first-class protocol for an agent to ask the user a question mid-workflow,
then continue from the answer. The same protocol should support retrying a failed
or unsatisfactory turn with notes, and optionally retrying with a different
model/provider after a clear warning about recomputation.

This is a cross-repo design:

- CLIO owns orchestrator-level interruption, resumed execution, retry semantics,
  context/memory provenance, and provider/model switching rules.
- gact-tui owns the interaction surface for questions, answers, retry actions,
  warnings, and model selection.

## Motivation

Complex agent workflows sometimes need user input instead of guessing:

- missing required information;
- ambiguous file/action target;
- permission-like human judgment that is not exactly a security permission;
- choice between expensive or lossy strategies;
- recovery after model/tool failure.

Today, the agent can only answer normally, error, or rely on generic free-text
follow-up. That loses the structured workflow boundary and makes it hard for the
TUI to render the state honestly.

## Desired Semantics

### Ask User

The orchestrator should be able to emit an interruption event:

- stable question id;
- prompt text;
- optional short choices;
- free-form answer allowance;
- reason/category;
- expected answer type;
- timeout or no-timeout behavior;
- provenance of which agent/expert asked.

The session should enter a waiting state that is distinct from running,
cancelled, or failed. The TUI should render an answer affordance and submit the
answer through an explicit endpoint rather than by faking a normal user message.

### Resume

After the user answers, CLIO should resume the suspended workflow or start a
defined continuation turn with explicit provenance. The answer should be visible
in transcript/history and in context frames, but marked as answering a specific
agent question.

### Retry With Notes

Users should be able to retry a turn with notes such as:

- "Use the CSV instead of the Parquet file."
- "Be more conservative."
- "Do not use network tools."

The retry should reference the original turn, preserve the notes, and make clear
whether prior tool results are reused or recomputed.

### Retry With Different Model

Users should be able to retry with a different model/provider when supported.
The TUI must warn that this can require recomputation of provider-side KV cache,
increase time to first token, increase cost/latency, and produce different
reasoning/tool choices.

This should not silently mutate the original turn. It should create a new
attempt with a clear attempt id and model/provider provenance.

## Backend Work In CLIO

- Add a session state for waiting on user input.
- Define wire objects for agent questions and user answers.
- Add endpoints for listing pending questions and answering one.
- Add SSE events for question created, answered, expired/cancelled, resumed, and
  retry attempt started/completed.
- Decide whether ask-user is represented as a tool call, orchestrator event, or
  both. The preferred model is a tool-like internal action with first-class GACT
  state.
- Add retry attempt records linked to original message/turn ids.
- Preserve model/provider/prompt/expert/context provenance for each attempt.
- Ensure memory/context frames distinguish original attempt, user answer, and
  retry attempt.

## TUI Work In gact-tui

- Render pending agent questions inline with the conversation and/or a status
  panel.
- Support choice answers and free-form answers.
- Keep the current draft safe while answering an agent question.
- Add retry actions for failed or completed assistant turns.
- Add retry-with-notes flow.
- Add retry-with-model flow with an explicit recomputation warning.
- Render attempts as related versions rather than overwriting transcript truth.

## Open Questions

- Should answers to agent questions be normal user messages, special transcript
  messages, or metadata attached to the interrupted turn?
- Can a suspended tool call resume in-process, or should CLIO treat every answer
  as a new continuation turn?
- Which retry modes may reuse prior tool outputs safely?
- How should cancellation interact with a waiting question?
- How should permission prompts and ask-user prompts be visually distinct while
  still sharing some UI primitives?

## Acceptance Criteria

- CLIO can emit a structured user question during orchestration.
- The TUI displays the question and lets the user answer without losing their
  normal draft.
- Answering resumes or continues the workflow with clear transcript provenance.
- Retrying with notes creates a linked attempt and preserves the note.
- Retrying with a different model/provider shows a recomputation/TTFT warning
  before execution.
- Context/memory metadata can explain which question/answer/retry attempt
  influenced a final response.

## Related Issues

- CLIO backend protocol issue: create in `iowarp/clio-agent`.
- TUI interaction issue: create in `iowarp/gact-tui`.
- Permission surfacing: related but separate; permissions decide whether an
  operation is allowed, while ask-user resolves workflow ambiguity.
- Memory refinement: retry and ask-user provenance should feed context frames.
