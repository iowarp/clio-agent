# ReAct loop completion contract

Status: accepted 2026-09-05.

## Decision

The model owns action selection inside the ReAct loop. CLIO does not call the
model again or execute an inferred tool after that loop returns.

- A response with no tool call is a normal direct response. CLIO does not
  classify its prose or lack of prose. The same text is returned as `answer` with
  `termination_reason="direct_response"`.
- A valid model-selected `submit` remains available for blueprints that need
  structured outputs.
- Parse failures and explicit iteration-cap exhaustion stop without a forced
  submit.
- React output validation is not repaired by a model call outside the loop.
- Malformed tool intent is not parsed from an exception and executed on the
  model's behalf.

## Removed mechanisms

The following mechanisms are intentionally absent:

1. retries when `tool_calls` is empty;
2. the post-loop forced-submit call;
3. bounded submit-repair over retained History;
4. schema-repair resampling in the blueprint wrapper;
5. adapter tool-intent recovery;
6. answer synthesis from retained tool observations;
7. empty-answer classification in agent wrappers.

This is a responsibility boundary, not a retry-budget change. Stronger models
must not be forced through recovery semantics designed to compensate for weaker
models. If a provider or model produces malformed output, the original failure
is observable and attributable to that provider/model.

The runtime does not inspect prose to decide whether the agent should continue.
A response may accompany a tool call: CLIO retains that response, executes the
model-selected tool, and continues with the observation. A response without a
tool call ends the run. Wrapper layers preserve the returned answer instead of
trimming it, synthesizing a substitute, or raising based on its content.

## Qualification invariant

For a prompt such as `Reply ready in one sentence. Do not call tools.`, the
provider is called once, no tool is executed, and one user-visible response is
persisted. Tests also pin a blank response and explicit iteration cap to one
provider call with no hidden finalization attempt. Presentation code may report
that a completed turn has no visible content, but that does not re-enter or alter
the agent path.
