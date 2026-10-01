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

## Amendment 2026-09-30: DSPy's extract, config-driven

Owner decision for the agent-loop rebuild
(`docs/design/agent-loop-rebuild-2026-09.md`, phase 6). This amendment narrows
"CLIO does not call the model again after that loop returns" and removed
mechanism 6 in exactly one respect. DSPy's own `ReAct` extract may run once after
a loop. It is literally DSPy's extract: `dspy.ChainOfThought` over the task
inputs, the missing outputs and the text trajectory
(`gact/agents/clio_react_extract.py`).

It runs only when all of these hold:

- `agents.react_extract.enabled` / `CLIO_REACT_EXTRACT_ENABLED` is on (default
  `true`).
- The loop took more than `agents.react_extract.after_steps` /
  `CLIO_REACT_EXTRACT_AFTER_STEPS` model steps (default `3`).
- The loop ended by `max_iters`, which has no answer at all, or by a direct
  response on a signature that declares outputs the loop did not produce (for
  example `question -> answer, summary`).
- It fills only those missing outputs.

What does not change:

- The answer the model wrote, which the user already saw, is never replaced.
- `submit` (the model's own typed outputs), an `ask_user` / `plan_exit` yield and
  `context_window_exceeded` never extract.
- A plain `question -> answer` direct response never extracts.
- A short loop never extracts, so the qualification invariant below holds as
  written. The one-sentence prompt and a blank response are still one provider
  call. An explicit iteration cap of three or fewer steps is still one call per
  step, with no finalization.
- The extract is not hidden. It is a normal DSPy module call on the same LM. The
  expert lifecycle's `expert.extract.completed` event names the fields it filled
  in `payload.extracted`.
- Setting `enabled: false` restores the original contract exactly.

## Qualification invariant

For a prompt such as `Reply ready in one sentence. Do not call tools.`, the
provider is called once, no tool is executed, and one user-visible response is
persisted. Tests also pin a blank response and explicit iteration cap to one
provider call with no hidden finalization attempt. Presentation code may report
that a completed turn has no visible content, but that does not re-enter or alter
the agent path.
