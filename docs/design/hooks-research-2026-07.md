# Hooks: A Design Survey Across Coding Agents, Agent Frameworks, and Scientific/Enterprise Agents

**Prepared for:** clio-agent campaign item #3 (Hooks)
**Date:** 2026-07-24
**Method:** primary sources only — cloned repos (`google-gemini/gemini-cli`, `openai/codex`, `sst/opencode`, `cline/cline`, `RooCodeInc/Roo-Code`, `microsoft/agent-framework`, `mastra-ai/mastra`, `cuga-project/cuga-agent`), downloaded packages (`langchain-core`, `langgraph`, `openai-agents`, `google-adk`, `crewai`, `strands-agents`, `pydantic-ai-slim`, `semantic-kernel`, `ag2`, `llama-index-core`, `smolagents`, `@ampcode/plugin`), and official docs. Community mirrors and secondary sources are marked **UNVERIFIED**.

---

## 0. TL;DR — what the field agrees on

Four independent CLI agent teams (Anthropic, OpenAI, Google, GitHub) converged on **the same wire protocol** for hooks, to the point of shipping compatibility aliases for each other:

> **Spawn a subprocess → write a JSON event envelope to its stdin → read stdout. Exit 0 means "proceed, and parse stdout as JSON control output". Exit 2 means "block, and feed stderr back to the model as the reason". Any other exit code is a non-blocking error.**

Gemini CLI injects `CLAUDE_PROJECT_DIR` alongside `GEMINI_PROJECT_DIR`. Cursor injects `CLAUDE_PROJECT_DIR`. Copilot CLI reads `.claude/settings.json` and accepts PascalCase Claude event names, switching its payload shape to match. Codex accepts `CLAUDE_PLUGIN_ROOT`. **Interop with this contract is now a table-stakes feature, not a differentiator.** If clio-agent invents a different envelope, it opts out of every hook script already written.

The second, less obvious convergence: **hooks may only tighten policy, never loosen it.** Claude Code shipped this as a security bug fix ("Fixed PreToolUse hooks returning `allow` bypassing `deny` permission rules, including enterprise managed settings"); Gemini enforces it structurally (extension policies with `allow` or `yolo` are silently ignored); Codex enforces it via `allow_managed_hooks_only`. Design this in from day one — it is very expensive to retrofit.

The third: **a hook subsystem failure must be distinguishable from a user rejection.** Claude Code shipped this fix twice. When a hook times out and the model is told "the user rejected this," unattended sessions deadlock.

---

## 1. The design space, as a decision list

Every hook system in this survey is a set of answers to these eleven questions. Sections 2–5 fill in who answered what; §6 is my recommendation for clio-agent.

| # | Question | Range of answers observed |
|---|---|---|
| 1 | **Transport** | subprocess (all CLIs) · HTTP POST (Copilot, Claude Code) · in-process callback (all frameworks) · LLM prompt (Cursor `type:"prompt"`, Claude Code `prompt`/`agent`) · MCP tool call (Claude Code `mcp_tool`) |
| 2 | **Granularity** | 5 events (Amp) → 11 (Gemini, Codex) → 14 (Copilot) → 21 (Cursor) → 30 (Claude Code) |
| 3 | **Power** | observe · inject context · mutate input · mutate output · deny-and-tell-model · abort-run · synthesize fake result · re-invoke (retry) |
| 4 | **Layer** | tool boundary only · + model-request boundary · + streaming chunk · + prompt/turn · + session/config/filesystem |
| 5 | **Matching** | none · exact-string · regex · glob · annotation-based (`readOnlyHint`) · semantic similarity (CUGA) |
| 6 | **Merge rule** for N hooks on one event | most-restrictive-wins · last-writer-wins · union · first-non-null-wins · undefined |
| 7 | **Failure posture** | fail-open · fail-closed · per-hook opt-in (`failClosed`) |
| 8 | **Ordering** | sequential-deterministic · concurrent-undefined · onion (LIFO teardown) |
| 9 | **Trust model** | none · folder-trust · content-hash fingerprint · admin-managed-only |
| 10 | **Identity** | positional index (everyone, and everyone regrets it) · stable name |
| 11 | **Durability** | synchronous-blocking · async fire-and-forget · durable/checkpointed (survives disconnect) |

---

## 2. Coding CLIs — the subprocess-hook family

### 2.1 Event taxonomy, side by side

| Cadence | Claude Code (30) | Codex (11) | Gemini CLI (11) | Copilot CLI (14) | Cursor (21) | Cline (11) |
|---|---|---|---|---|---|---|
| Session | `SessionStart`, `SessionEnd`, `Setup`, `InstructionsLoaded`, `ConfigChange`, `CwdChanged`, `FileChanged`, `DirectoryAdded` | `SessionStart`, `SessionEnd` | `SessionStart`, `SessionEnd` | `sessionStart`, `sessionEnd` | `sessionStart`, `sessionEnd`, `workspaceOpen` | `TaskStart`, `TaskResume`, `TaskCancel`, `TaskComplete`, `SessionShutdown` |
| Turn | `UserPromptSubmit`, `UserPromptExpansion`, `MessageDisplay`, `Stop`, `StopFailure` | `UserPromptSubmit`, `Stop` | `BeforeAgent`, `AfterAgent` | `userPromptSubmitted`, `userPromptTransformed`, `agentStop`, `errorOccurred` | `beforeSubmitPrompt`, `stop`, `afterAgentResponse`, `afterAgentThought` | `UserPromptSubmit` |
| Model call | — | — | **`BeforeModel`, `AfterModel`, `BeforeToolSelection`** | — | — | (`beforeModel`/`afterModel` in SDK runtime only) |
| Tool | `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `PermissionRequest`, `PermissionDenied` | `PreToolUse`, `PostToolUse`, `PermissionRequest` | `BeforeTool`, `AfterTool` | `preToolUse`, `postToolUse`, `postToolUseFailure`, `permissionRequest` | `preToolUse`, `postToolUse`, `postToolUseFailure`, `beforeShellExecution`, `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`, `beforeReadFile`, `afterFileEdit` | `PreToolUse`, `PostToolUse` |
| Subagent | `SubagentStart`, `SubagentStop`, `TaskCreated`, `TaskCompleted`, `TeammateIdle` | `SubagentStart`, `SubagentStop` | — | `subagentStart`, `subagentStop` | `subagentStart`, `subagentStop` | — |
| Context mgmt | `PreCompact`, `PostCompact` | `PreCompact`, `PostCompact` | `PreCompress` | `preCompact` | `preCompact` | `PreCompact` |
| Misc | `Notification`, `WorktreeCreate`, `WorktreeRemove`, `Elicitation`, `ElicitationResult` | — | `Notification` | `notification` | Tab events (`beforeTabFileRead`, `afterTabFileEdit`) | `Notification`, `TaskError` |

**Reading of the table.** Claude Code's 30 events are not 30 useful events; they're ~8 load-bearing ones (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart/End`, `PermissionRequest`, `PreCompact`) plus a long tail added reactively. Gemini's 11 are better-chosen because they cut at a *different* layer: `BeforeModel`/`AfterModel`/`BeforeToolSelection` intercept the LLM request itself, which no other CLI does. Cursor's 21 are the same 8 plus **tool-kind-specific** variants (`beforeShellExecution`, `beforeReadFile`, `beforeMCPExecution`) — a design that trades event count for not needing matchers.

Three events I'd call under-appreciated and worth copying:

- **`PostToolBatch`** (Claude Code) — fires after a full *parallel* batch of tool calls resolves, before the next model call. Everyone else fires per-tool, which makes "check the aggregate state after this round of edits" impossible to express.
- **`WorktreeCreate`** (Claude Code) — the hook *replaces* the default git behavior and returns the worktree path on stdout. The only example in the survey of a hook that *implements* a capability rather than gating one. And its failure posture is uniquely strict: **any** non-zero exit aborts, not just 2.
- **`workspaceOpen` → `{"pluginPaths": [...]}`** (Cursor) — a hook that bootstraps other extension points. Meta-extensibility.

### 2.2 Firing-point gotchas that will bite you

These are the details that separate a hook system that works from one that leaks:

- **`@`-file references bypass `PreToolUse`.** Claude Code inserts `@path/file` contents while *building the prompt*, so no `Read`-matching hook fires. A file-access policy built only on tool hooks has a hole. (Fix: a deny rule, or a hook on the prompt-construction step.)
- **Bash writes bypass edit matchers.** `Bash(cat > file)` is not an `Edit`. Claude Code's docs recommend a `Stop` hook scanning `git status --porcelain` for compliance coverage. Codex's plan-mode prompt independently draws the line at *repo-tracked mutation* rather than *any write* — a better primitive.
- **Pre-execution rejections don't fire `PostToolUseFailure`.** Unknown tool, schema validation failure, permission denial — all return a tool error *before* hooks run. Your audit log will have holes unless you deliberately emit for these.
- **Resumed sessions replay saved `additionalContext`** rather than re-running the hook. Timestamps, git SHAs, and "current on-call" injected at `SessionStart` go stale silently. Only `SessionStart` re-runs.
- **`updatedToolOutput` changes only what the model sees.** The file was already written, the command already ran, the packet already sent. Useful for redaction of what enters context; useless as a control.

### 2.3 Output contracts

The universal envelope, near-identical across Claude Code / Codex / Copilot:

```json
{
  "continue": false,
  "stopReason": "shown to user, NOT to the model",
  "suppressOutput": true,
  "systemMessage": "warning shown to user"
}
```

Per-event decision surfaces diverge, and the divergence is instructive:

| Capability | Claude Code | Codex | Gemini | Copilot | Cursor | Amp | OpenCode |
|---|---|---|---|---|---|---|---|
| Deny tool | `hookSpecificOutput.permissionDecision: "deny"` | same | `decision:"deny"` | `permissionDecision:"deny"` | `{"permission":"deny"}` | `{action:'reject-and-continue', message}` | `throw` |
| Mutate tool input | `updatedInput` | `updatedInput` (only with `allow`) | `hookSpecificOutput.tool_input` (merges/overrides) | `modifiedArgs` | `updated_input` | `{action:'modify', input}` | mutate `output.args` **properties only** |
| Mutate tool output | `updatedToolOutput` | ✗ (reserved, fails closed) | `decision:"deny"` replaces result | `modifiedResult` | `updated_mcp_tool_output` | `ToolResultResult` | mutate `output` |
| Inject context | `additionalContext` | `additionalContext` | `additionalContext` | `additionalContext` | `additional_context` | — | mutate system prompt via `experimental.chat.system.transform` |
| **Synthesize a fake result** | ✗ | ✗ | **`BeforeModel` → `llm_response`** | ✗ | ✗ | **`{action:'synthesize', result}`** | ✗ |
| Self-loop / continue turn | `decision:"block"` on `Stop` | same | `AfterAgent` deny → reason becomes new prompt | `agentStop` block | `stop → followup_message` (+ `loop_limit`) | `agent.end → {action:'continue', userMessage}` | `experimental.compaction.autocontinue` |
| Defer for out-of-band approval | **`permissionDecision:"defer"`** (`-p` mode; process exits with `stop_reason:"tool_deferred"`, resume later) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

**Amp's tagged union is the cleanest contract in the survey** and I'd steal it wholesale:

```ts
export type ToolCallResult =
  | { action: 'allow' }
  | { action: 'reject-and-continue'; message: string }
  | { action: 'modify'; input: Record<string, unknown> }
  | { action: 'synthesize'; result: { output: string; exitCode?: number } }
  | { action: 'error'; message: string }
```

Five outcomes, mutually exclusive, exhaustively checkable. Compare Claude Code, where the same information is spread across a top-level `decision`, a nested `hookSpecificOutput.permissionDecision`, a deprecated `decision: "approve"|"block"` alias, `updatedInput`, `continue`, and exit codes — with a documented rule that you must choose exit codes *or* JSON but never both.

`synthesize` deserves emphasis: it lets a hook return a fabricated tool result without running the tool. That is caching, mocking, offline replay, and safe-mode-simulation, all from one primitive. Gemini has the same idea one layer up (`BeforeModel` returning `llm_response` skips the LLM call entirely).

### 2.4 Configuration schemas

**Claude Code** — three-level nesting, five handler types:

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Bash",
  "hooks": [ { "type": "command", "if": "Bash(rm *)",
               "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/block-rm.sh",
               "args": [], "timeout": 600, "async": false } ] } ] } }
```
Handler types: `command` | `http` | `mcp_tool` | `prompt` (a Haiku call returning `{"ok": bool, "reason": str}`) | `agent` (a subagent, up to 50 turns). Sources merge rather than override: user → project → local → managed policy → plugin `hooks/hooks.json` → skill/subagent YAML frontmatter.

**Cursor** — flatter, and with two fields nobody else has:

```json
{ "version": 1,
  "hooks": { "afterFileEdit": [ { "command": "hooks/audit.sh",
                                  "type": "command",
                                  "matcher": "Shell",
                                  "timeout": 30,
                                  "failClosed": true,
                                  "loop_limit": 5 } ] } }
```
`failClosed` (default `false`) is **per-hook security posture** — the only system that lets one hook be advisory and another mandatory. `loop_limit` bounds `stop`-hook re-entry. Claude Code's equivalent is a global cap of 8 consecutive `Stop` blocks (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`).

**Cline** — no registry at all: hooks are **extension-less executables with shebangs**, one file per event name, in `~/.cline/hooks/` or `<ws>/.cline/hooks/`. `chmod +x` is the enable switch. Zero config, and a `HookDiscoveryCache` to avoid a `readdir` per tool call.

**Copilot CLI** — the only one with an **`http`** handler as a first-class type (POST the JSON envelope to a URL) and a **`prompt`** handler (inject text or a slash command with no external process). It also strips streaming progress lines before the final JSON parse:
```json
{"type": "progress", "message": "scanning...", "temporary": true}
```

**Gemini CLI** — timeouts in **milliseconds** (default 60000), a `sequential` flag per matcher group (serial vs parallel within the group), and a global `hooksConfig: { enabled, disabled: [names], notifications }` kill switch.

### 2.5 Matcher semantics — a trap everyone fell into

Claude Code's rule is representative and worth quoting because the failure mode is subtle:

> `"*"`, `""`, or omitted → match all. A matcher containing only `[A-Za-z0-9_\- ,|]` → exact string, or `|`/`,`-separated list of exact strings. **Any other character → JavaScript regex, unanchored.**

Consequences shipped as bugs in at least three products:
- `Edit.*` matches `NotebookEdit` (unanchored). You need `^Edit$`.
- `mcp__memory` matches **nothing** — it's all exact-match characters, so it's compared as a literal string, and no tool is named exactly that. You must write `mcp__memory.*`. Codex has the identical rule and the identical footgun.
- Hyphens joined the exact-match set only at Claude Code 2.1.195; before that `code-reviewer` also substring-matched `senior-code-reviewer`.
- Comma separators silently never fired before 2.1.191.
- Claude Code's `FileChanged` matcher is a **literal filename list**, not a regex — `^\.env` watches a file literally named `^\.env`.

**Gemini's answer is better: match on tool *annotations*, not names.** Its plan-mode policy re-allows tools via `toolAnnotations = { readOnlyHint = true }`, which automatically covers MCP tools nobody enumerated. Names are a brittle join key; capabilities are a stable one.

### 2.6 Trust, and the positional-identity bug everyone has

Hooks are arbitrary code from a repo you just cloned. Three different solutions:

- **Gemini** — `TrustedHooksManager` persists `~/.gemini/trusted_hooks.json` as `{projectPath: ["name:command", ...]}`. Project hooks are fingerprinted by **name+command**; a `git pull` that changes either re-prompts.
- **Codex** — hashes a *normalized identity* so the JSON and TOML config forms converge to the same hash, with a `trustStatus` state machine (`Managed`/`Trusted`/untrusted) readable and writable over JSON-RPC. Escape hatch: `--dangerously-bypass-hook-trust`.
- **Copilot / Cursor** — repo hooks gated behind folder trust; enterprise `policy.d/*.json` outranks everything.

Admin lockdown primitives worth copying verbatim: Codex's `allow_managed_hooks_only = true` in `requirements.toml` (drops every non-managed source including plugins), Claude Code's `allowManagedHooksOnly` + `allowedHttpHookUrls` allowlist + `allowedHttpHookEnvVars` intersection, and Gemini's rule that **extension-supplied policies with `allow` or `yolo` decisions are silently ignored**.

**The shared unsolved bug:** hook identity is *positional*. Codex keys per-hook state as `{source}:{event_label}:{group_idx}:{handler_idx}` and its own source comments note this is "currently positional." Reordering a config array invalidates trust state and disable state. **clio-agent should require a stable `id` field on every hook.** This is a one-line design decision that nobody made and everybody now needs.

### 2.7 The rest of the field, briefly

- **OpenCode** — in-process TypeScript plugins, hooks named `tool.execute.before`, `tool.execute.after`, `chat.message`, `chat.params`, `chat.headers`, `permission.ask`, `command.execute.before`, `shell.env`, `tool.definition`, plus `experimental.*` (a nice explicit stability channel). Contract is `(input, output) => Promise<void>` + **mutate `output` in place**; deny = `throw`. Internal features (all auth providers) are themselves plugins in the same array as user plugins — a strong dogfooding signal. **Aliasing trap:** the dispatcher passes `{ args }` where `output.args` aliases the live `args` object, so mutating *properties* works but *reassigning* `output.args = {...}` is silently dropped. If you pass a mutable container, either pass the exact object that will be consumed or read the return value back.
- **Roo Code** — **no hook system.** It exposes `RooCodeAPI extends EventEmitter` over an IPC unix socket (~25 event types) for observation only, plus custom tools in `.roo/tools/*.ts`. Worth noting as the cheap option: an out-of-process event socket gives you audit and telemetry without a plugin host.
- **Aider** — no hooks; `--lint-cmd`, `--test-cmd`, `--auto-lint`, `--auto-test`, `--notifications-command`. The idea worth stealing is the **repair loop**: a post-edit command whose non-zero exit output is automatically re-prompted to the model. That is strictly more useful than an observe-only `afterFileEdit`.

---

## 3. In-process middleware — what the framework world does differently

Coding CLIs converged on subprocesses. Agent frameworks converged on **onion middleware**, and the gap between the two is the most important thing in this report.

### 3.1 LangChain v1 `create_agent` middleware — the closest framework analogue

```python
class AgentMiddleware(Generic[StateT, ContextT, ResponseT]):
    def before_agent(self, state, runtime) -> dict[str, Any] | None
    def before_model(self, state, runtime) -> dict[str, Any] | None
    def after_model(self, state, runtime) -> dict[str, Any] | None
    def after_agent(self, state, runtime) -> dict[str, Any] | None

    def wrap_model_call(self, request: ModelRequest,
                        handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse
    def wrap_tool_call(self, request: ToolCallRequest,
                       handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command
```
(every method has an `a`-prefixed async twin; decorator forms `@before_model`, `@wrap_tool_call`, `@dynamic_prompt`, `@hook_config` are exported)

Two semantics matter:
- `before_*`/`after_*` return a **state update dict**, merged through graph reducers. Short-circuit is a *jump*: declare `@hook_config(can_jump_to=["end"])` and return `{"jump_to": "end"}`.
- `wrap_*` receive a `handler` callable. **Call it zero times = deny; once = normal; N times = retry.** Middleware composes automatically, first-defined = outermost.

Built-ins shipped on this API: `HumanInTheLoopMiddleware`, `PIIMiddleware`, `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware`, `SummarizationMiddleware`, `ContextEditingMiddleware`, `ModelFallbackMiddleware`, `ToolRetryMiddleware`, `LLMToolEmulator`, `TodoListMiddleware`. Note how many of these are *impossible* to write as before/after hooks — fallback, retry, and emulation all require re-invoking the wrapped call.

### 3.2 Google ADK — the cleanest short-circuit convention

Six callbacks (`before_agent`, `after_agent`, `before_model`, `after_model`, `before_tool`, `after_tool`, plus `on_model_error` / `on_tool_error`), each `Optional[X] | list[X]`, sync-or-async, with one uniform rule:

> **Return `None` → proceed. Return a non-`None` typed value → that value replaces/short-circuits the operation.**

`before_model_callback` returning an `LlmResponse` skips the model call. `before_tool_callback` returning a `dict` skips the tool. `after_tool_callback` returning a `dict` replaces the result. `on_tool_error_callback` returning a value swallows the exception; `None` re-raises. List semantics: "called in the order listed **until a callback does not return None**."

And ADK is the only system in the survey with a clean **operator/author split**: `BasePlugin` registers the *same twelve* callbacks globally on the `Runner`, and **plugin callbacks run first and take precedence** over the agent author's own callbacks. That is governance. In-process middleware that the agent author can simply omit is not.

### 3.3 The others, in one line each

| Framework | Control API | Deny mechanism | Notable |
|---|---|---|---|
| **LangChain `BaseCallbackHandler`** | ~20 `on_*` methods | **none** — pure observation | errors swallowed unless `raise_error=True`; `astream_events` is the pull-dual of the same stream |
| **OpenAI Agents SDK** | `RunHooks`/`AgentHooks` all return `None` | **guardrails**, not hooks: `tripwire_triggered` | `ToolGuardrailFunctionOutput.reject_content()` (tell the model) vs `.raise_exception()` (kill the run) — the clearest deny/abort split anywhere |
| **CrewAI** | 3 overlapping generations: `step_callback`, `@before_llm_call` hooks, event bus | return `False` from `before_*` → `HookAborted` | `@before_llm_call(agents=["researcher"])` — per-agent hook scoping |
| **Microsoft Agent Framework** | `async process(context, call_next)` × 3 pipelines | don't call `call_next`, or raise `MiddlewareTermination` | live-mutable `context.tools` with `add_tools`/`remove_tools` for progressive tool exposure |
| **Semantic Kernel filters** | `OnFunctionInvocationAsync(context, next)` | don't call `next` | *"If it's not invoked, next filter or function won't be invoked"* |
| **AWS Strands** | typed dataclass events with a per-field write ACL | `event.cancel_tool = "reason"` | `event.selected_tool = other_tool` **swaps which tool runs**; `After*` events set `should_reverse_callbacks=True` → LIFO teardown from a flat registry |
| **Pydantic AI** | `ToolPrepareFunc` returning `None` **hides the tool** from that request | `DeferredToolRequests` as an *output type* | HITL expressed as a terminal output + resume, structurally identical to LangGraph interrupts |
| **Mastra** | `Processor` interface, `processOutputStream` returning `null` drops a chunk | `context.abort(reason, {retry})` → `TripWire` | `retry: true` re-runs with the reason as feedback, bounded by `maxProcessorRetries` |
| **AG2/AutoGen** | `register_hook(name, fn)` (chained), `register_reply` | `safeguard_tool_inputs` returning `None` aborts | five safeguard points: tool in/out, llm in/out, human in |
| **LlamaIndex, smolagents** | event handlers / `step_callbacks` | none (smolagents: `final_answer_checks` raising `AgentError`) | telemetry-grade only |

### 3.4 Subprocess vs in-process — the honest trade

**In-process wins:**
1. **Live object graphs.** ADK hands you the real `LlmRequest`; MAF hands you a mutable tools list. JSON envelopes can't carry a model handle.
2. **Wrapping, not bracketing.** Retry, fallback, caching, and A/B all fall out of `handler`/`next`/`call_next`. A subprocess cannot re-run the call it just observed. **This is the single biggest capability gap.**
3. **Streaming interception.** Per-token subprocess spawns are economically impossible.
4. **Typed contracts** caught by static checkers instead of runtime JSON validation.
5. **Shared mutable run state** without round-tripping through the filesystem.
6. **Latency** — microseconds vs tens of ms per tool call.

**Subprocess wins:**
1. **Language independence** — a bash one-liner, an existing linter, a Go binary.
2. **Fault isolation** — a segfaulting hook can't corrupt agent state.
3. **Trust boundary** — hooks run outside the agent's address space, unreachable by prompt-injected in-process code. This is why the security-relevant hooks in CLIs are subprocesses.
4. **Deploy without redeploy** — edit a settings file, don't rebuild a binary.

**They are not exclusive, and the bridge is trivial:** map subprocess exit 0 → return `None`, exit 2 + stderr → return the deny payload. ADK's `None`-means-proceed convention is *exactly isomorphic* to exit-code-2-with-stderr. A single internal middleware interface with a subprocess adapter gives you both.

---

## 4. Non-coding agents — governance, policy, and one cautionary tale

### 4.1 IBM CUGA — hooks as declarative data

CUGA (IBM Research; #1 AppWorld, #1 WebArena for a period) is the only agent in the survey that expresses interception as an **operator-owned declarative policy store** rather than code:

```python
class PolicyType(str, Enum):
    PLAYBOOK; INTENT_GUARD; TOOL_GUIDE; TOOL_APPROVAL; OUTPUT_FORMATTER; CUSTOM

class PolicyActionType(str, Enum):
    GUIDE_PROMPT; BLOCK_INTENT; INJECT_CONTEXT; MODIFY_TOOLS
    TOOL_INJECT_DESCRIPTION; TOOL_REQUIRE_APPROVAL
    FORMAT_OUTPUT; REDIRECT; LOG_ONLY

class ToolTrigger(BaseModel):
    type: Literal["tool"] = "tool"
    value: str                                    # tool name
    stage: Literal["before", "after"] = "before"
```

That last class is **`PreToolUse`/`PostToolUse` expressed as data**. Triggers also include `NaturalLanguageTrigger` (semantic similarity, `threshold=0.7`), `KeywordTrigger` (with and/or), `AppTrigger`, `StateTrigger`, `AlwaysTrigger`. Enforcement is `ToolGuard`, a decorator around the tool provider — and note the failure posture, from its README:

| Condition | Result |
|---|---|
| Applicable guard exists and passes | original tool runs |
| Applicable guard exists and blocks | original tool is not called |
| **Applicable guard exists but runtime/domain cannot load** | **tool call is blocked** |

**Fail-closed.** Blocked calls return `{"error": ..., "blocked_by_policy": true, "policy_violation": true, "tool": ..., "app": ...}`. And CUGA's `PLAYBOOK` policy type lets an operator supply a *plan* to the planner — policy that pre-empts decomposition, not just gates execution.

### 4.2 ChemCrow — the canonical anti-pattern

ChemCrow (Nature-published chemistry agent) implements chemical-weapon safety checks as **ordinary tools that return strings into the observation channel**:

```python
class SimilarControlChemCheck(BaseTool):
    def _run(self, smiles: str) -> str:
        max_sim = cw_df["smiles"].apply(lambda x: self.tanimoto(smiles, x)).max()
        if max_sim > 0.35:
            return f"{smiles} has a high similarity ({max_sim:.4}) to a known controlled chemical."
        else:
            return "... This is substance is safe, you may proceed with the task."
```

Three failures, all structural:
1. **Optional** — the LLM decides whether to call the check (it's told to, in prompt text).
2. **Advisory** — no code path blocks anything; the result is just text in context.
3. **Fail-open** — exceptions return the string `"Tool error."` and execution continues.

Plus the string `"you may proceed with the task"` is an *instruction* sitting in tool output — the exact shape of a prompt injection. Coscientist (same domain, physical lab robots) has no gate at all before `EXPERIMENT <code>`; its own paper recommends a human-in-the-loop it doesn't implement.

**The lesson, stated generally:** if your threat model includes the model ignoring a check's result, the check must sit **outside the observation channel**. A safety hook that returns advice to the model is not a control; a safety hook that returns `deny` to the harness is.

Biomni is one config line from being fully gateable — it *is* LangGraph, with `generate`/`execute` nodes, but `workflow.compile()` is called with no `interrupt_before=["execute"]`, and `<execute>` blocks go straight to `run_with_timeout`.

### 4.3 MCP — hooks with inverted ownership

MCP's `elicitation` and `sampling` are the cross-vendor hook analogue, and they invert who asks: the **server requests** the gate, the **client** decides.

- `elicitation/create` restricts `requestedSchema` to *flat objects with primitive properties only*, so any client can render it, and returns a **tri-state** `action: "accept" | "decline" | "cancel"` — distinguishing explicit refusal from abandonment, which almost everyone else collapses into a boolean. Spec: servers **MUST NOT** use it for sensitive information.
- `sampling` carries the strongest published norm: *"there SHOULD always be a human in the loop with the ability to deny sampling requests."*
- `ToolAnnotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) are policy metadata with **fail-safe defaults** (`destructiveHint` and `openWorldHint` default *true*) and an explicit spec warning never to trust them from untrusted servers.

**Weakness to design around:** MCP gates are synchronous-blocking JSON-RPC. Drop the connection, lose the gate. Contrast LangGraph checkpointers, Bedrock's `invocationId` + `returnControlInvocationResults`, ADK's `LongRunningFunctionTool` ticket, and CUGA's `MemorySaver` — all of which decouple the gate from the connection. **Durability, not synchrony, is the hard axis**, and retrofitting it means rewriting the loop.

### 4.4 AWS Bedrock — the cleanest external-approval protocol

Return of Control: set `RETURN_CONTROL` as `customControl` in `actionGroupExecutor`; the agent returns `{returnControl: {invocationInputs, invocationId}}`; you resume by posting `sessionState.returnControlInvocationResults`. Per-action approval is `requireConfirmation: ENABLED | DISABLED`, justified in the docs as protection *"from taking actions due to malicious prompt injections."* AgentCore Policy (Cedar) evaluates *"every agent action ... at the boundary outside of agent's code."*

---

## 5. Recommended design for clio-agent

### 5.1 Architecture: one internal interface, three adapters

```
                       ┌────────────────────────────────┐
   agent loop  ────────►  HookDispatcher                 │
                       │  (matching, ordering, merging,  │
                       │   timeout, failure posture)     │
                       └───┬────────┬─────────┬──────────┘
                           │        │         │
              ┌────────────▼──┐ ┌───▼─────┐ ┌─▼──────────────┐
              │ subprocess    │ │ http    │ │ in-process     │
              │ adapter       │ │ adapter │ │ middleware     │
              │ (stdin JSON,  │ │ (POST)  │ │ (wrap/next)    │
              │  exit 0/2)    │ │         │ │                │
              └───────────────┘ └─────────┘ └────────────────┘
```

Define the **internal** interface as onion middleware (`wrap_tool_call(request, next)`), because it is strictly more expressive, then implement the subprocess adapter *on top of it* as a middleware that calls `next()` exactly zero or one times. You get retry/fallback/caching for in-process extensions, and full Claude-Code wire compatibility for scripts, without two parallel systems.

### 5.2 Event set — 12 events, cut at three layers

Do not ship 30. Ship 12 chosen so that every interception need maps to exactly one, and reserve room to add.

| Event | Layer | Can block? | Purpose |
|---|---|---|---|
| `SessionStart` | session | no | inject context, set env, register watch paths |
| `SessionEnd` | session | no | flush, cleanup |
| `UserPromptSubmit` | turn | **yes** | validate/augment/reject the prompt |
| `BeforeModel` | model | **yes** | swap model/params, **synthesize a response** (cache, offline, mock) |
| `AfterModel` | model | no | redact/rewrite the response before it enters context |
| `PreToolUse` | tool | **yes** | the workhorse: allow/deny/ask/modify/synthesize |
| `PostToolUse` | tool | **yes** (block=feedback) | lint, format, validate, rewrite what the model sees |
| `PostToolBatch` | tool | **yes** | aggregate checks after a parallel batch |
| `SubagentStart` / `SubagentStop` | delegation | Stop: **yes** | scope propagation, result validation |
| `Stop` | turn | **yes** | completion gates (tests pass? todos closed?) |
| `PreCompact` | context | **yes** | preserve state across compaction |

Deliberately **omitted**, with reasons: per-tool-kind events (`beforeShellExecution` etc.) — use annotation matchers instead; `Notification` — use the event stream; worktree/config/cwd/file-watch events — add only when a real user asks. Every event you ship is a compatibility commitment.

**Do add `BeforeModel` with synthesize.** Gemini is the only CLI with it and it's the highest-leverage event nobody copied: offline replay, deterministic tests, response caching, and model routing all become external.

### 5.3 The wire contract

**Input** (stdin JSON, or HTTP body). Common envelope on every event:

```json
{
  "hook_event_name": "PreToolUse",
  "hook_id": "block-secrets",
  "schema_version": 1,
  "session_id": "…", "turn_id": "…", "prompt_id": "…",
  "cwd": "/repo", "transcript_path": "/…/transcript.jsonl",
  "permission_mode": "default|plan|acceptEdits|dontAsk|bypass",
  "agent_id": null, "agent_type": null,
  "model": "…",
  "tool_name": "Bash",
  "tool_use_id": "toolu_…",
  "tool_input": { "command": "npm test" },
  "tool_annotations": { "readOnly": false, "destructive": true, "openWorld": false }
}
```

Ship `schema_version` from v1. Nobody in this survey did, and all of them have now made breaking payload changes. Ship `tool_annotations` too — it's what lets policies be written against capabilities instead of names.

**Output** — one tagged union, not six overlapping fields:

```json
{ "decision": "allow" }
{ "decision": "deny",      "reason": "…shown to the model…", "userMessage": "…shown to the human…" }
{ "decision": "ask",       "reason": "…" }
{ "decision": "modify",    "input": { … } }
{ "decision": "synthesize","result": { "output": "…", "exitCode": 0 } }
{ "decision": "defer",     "ticket": { "callbackUrl": "…", "timeoutSeconds": 3600 } }
```
plus event-independent modifiers: `additionalContext` (string, appended to model context), `systemMessage` (string, shown to user), `suppressOutput`, `continue: false` + `stopReason`.

Exit codes for the subprocess adapter, matching the de-facto standard: **0** = parse stdout as the above (empty = allow); **2** = `{"decision":"deny","reason": <stderr>}`; **anything else** = non-blocking error, subject to the hook's failure posture.

`defer` (borrowed from Claude Code's `-p`-mode `tool_deferred`, generalized with Bedrock's `invocationId` model) is what makes approvals **durable**. The run suspends with a resumable ticket rather than holding a socket open. Given how many enterprise HITL stories need "approve by Monday," this should be in v1, not bolted on.

### 5.4 Config schema

```json
{
  "version": 1,
  "hooks": [
    {
      "id": "block-secrets",                  // REQUIRED, stable — not positional
      "on": ["PreToolUse"],
      "match": {
        "tool": "^(Edit|Write)$",             // anchored regex, explicitly documented as such
        "annotations": { "destructive": true },
        "argsPattern": "\\.env"
      },
      "run": { "type": "command", "command": "./hooks/secrets.sh", "args": [] },
      "timeoutMs": 30000,
      "failClosed": true,
      "async": false,
      "loopLimit": 5,
      "enabled": true
    }
  ]
}
```

Decisions embedded here, each with a reason from the survey:
- **`id` is required and stable.** Fixes the positional-identity bug in Codex/Claude Code/Gemini. Trust hashes, enable/disable state, telemetry, and error messages all key off it.
- **Regexes are anchored by default**, with `contains:` as an explicit alternative. Removes the `Edit.*` → `NotebookEdit` class of bug entirely.
- **`annotations` matching** alongside name matching. Covers MCP tools nobody enumerated.
- **`failClosed` is per-hook** (Cursor's idea), defaulting to `false` for `PostToolUse`-style hooks and `true` for anything that can deny.
- **`timeoutMs`** — one unit, everywhere. Gemini uses ms, Codex seconds, Copilot seconds; the inconsistency is a real source of misconfiguration.
- **A flat array with `on: [...]`**, not Claude Code's three-level `event → matcher group → handler` nesting. The nesting buys nothing and makes hand-editing painful.

Precedence: `managed (admin) > project > user > plugin`, all **merged, never overriding**, with the invariant below.

### 5.5 Six invariants to state in the spec

1. **Hooks may only tighten.** A hook `allow` never overrides a `deny` from a permission rule or a higher-precedence source. Not configurable.
2. **Hook failure ≠ user rejection.** Distinct internal error type, distinct message to the model, distinct telemetry event. Never surface "the user rejected this" for a timeout.
3. **Most-restrictive-wins** when N hooks decide one event: `deny > defer > ask > modify > allow`. `additionalContext` from all hooks is concatenated. `modify` from multiple hooks is an **error**, not last-writer-wins (Claude Code and Codex both resolve this non-deterministically by completion order — a genuine bug).
4. **Fail-closed on infrastructure failure for deny-capable hooks** (CUGA's rule: guard exists but can't load ⇒ block).
5. **`After*` hooks run LIFO** (Strands' `should_reverse_callbacks`) so a flat registry gives proper onion nesting.
6. **Bounded self-loops.** `Stop`-hook re-entry capped per-hook (`loopLimit`) and globally; a `stop_hook_active` flag in the payload so hooks can self-limit.

### 5.6 Ergonomics worth copying

- **Cline's shebang discovery** as a zero-config on-ramp: `~/.clio/hooks/PreToolUse` executable ⇒ registered. Config file only for anything non-default. Cache the directory listing (Cline does) — you're doing this per tool call.
- **A `prompt` handler type** (Cursor, Copilot, Claude Code): the hook *is* an LLM prompt returning `{"ok": bool, "reason": str}`, no external process. Removes the "I need a classifier, so now I need a Python script and its deps" barrier.
- **`/hooks` inspection command** showing every loaded hook with its source label (`user`/`project`/`managed`/`plugin`) and current trust state. This is the debugging entry point in every product that has one; Copilot's `/env` is cited as the thing that makes hooks tractable.
- **JSONL audit log** of every hook invocation (Cline's `CLINE_HOOKS_LOG_PATH`). Compliance stories need this and it's ~20 lines.
- **No TTY.** Claude Code moved hooks to their own session with no controlling terminal after hook output corrupted the UI; user-facing output goes through `systemMessage`. Start there.

---

## 6. Test cases

### 6.1 Contract conformance (unit)

| # | Case | Expected |
|---|---|---|
| C1 | Hook exits 0, empty stdout | allow, no context change |
| C2 | Hook exits 0, stdout `{"decision":"deny","reason":"nope"}` | tool not run; model receives "nope" |
| C3 | Hook exits 2, stderr `"nope"` | identical outcome to C2 |
| C4 | Hook exits 2 **and** prints valid JSON | blocks (exit code wins); no crash. *Claude Code shipped this as a bug.* |
| C5 | Hook exits 2 with empty stderr | non-blocking error, not a silent block |
| C6 | Hook exits 1 | non-blocking error, tool proceeds (unless `failClosed`) |
| C7 | Hook exits 0, stdout is non-JSON text | for context-injecting events → treated as context; for tool events → ignored + warning. **Never** "default to allow and treat output as a systemMessage" silently (Gemini's documented fail-open). |
| C8 | Hook stdout prefixed by a shell profile banner | parse must survive, or produce a *diagnosable* error naming the banner. *This is the #1 support issue in Claude Code's docs.* |
| C9 | Stdout > 10 MB | spill to file, inject a preview + path; no OOM |
| C10 | Hook writes to `/dev/tty` | no UI corruption |

### 6.2 Decision semantics

| # | Case | Expected |
|---|---|---|
| D1 | Hook returns `allow`; a `deny` permission rule matches | **denied.** Invariant 1. |
| D2 | Hook returns `allow` under `bypassPermissions` mode | allowed |
| D3 | Hook returns `deny` under `bypassPermissions` mode | **denied.** Hooks are unbypassable. |
| D4 | Two hooks: one `allow`, one `deny` | denied; **both hooks still executed** |
| D5 | Two hooks both return `modify` | error surfaced to user, tool blocked. Not last-writer-wins. |
| D6 | Two hooks return `additionalContext` | both concatenated, order stable |
| D7 | `modify` changes `tool_input`; a deny rule matches the *new* input | re-evaluated against the modified input, then denied |
| D8 | `synthesize` returned | tool never executes; `PostToolUse` still fires with the synthetic result flagged `synthetic: true` |
| D9 | `defer` returned | run suspends with a resumable ticket; resuming re-fires the hook |

### 6.3 Matching

| # | Case | Expected |
|---|---|---|
| M1 | matcher `Edit` vs tool `NotebookEdit` | **no match** (anchored by default) |
| M2 | matcher `mcp__memory` vs `mcp__memory__create` | documented behavior, and the *other* interpretation produces a startup warning |
| M3 | matcher on `annotations.destructive` vs an MCP tool that declares it | match |
| M4 | matcher on a tool with **no** annotations | treated as `destructive: true`, `openWorld: true` (fail-safe defaults, per MCP) |
| M5 | matcher with an invalid regex | config load error naming the hook `id`; other hooks still load. *Claude Code once invalidated the entire settings file on one malformed hook.* |
| M6 | unknown event name in config | warning naming the hook; rest of file loads |

### 6.4 Coverage / leak tests — the ones that actually matter

| # | Case | Expected |
|---|---|---|
| L1 | Model writes a file via `Bash(cat > f)` while an `Edit`-matching deny hook is active | **documented behavior**, plus a `PostToolBatch`/`Stop` recipe in docs. Fail loudly in the spec rather than silently in production. |
| L2 | Model reads a secret via an `@file` reference rather than the Read tool | `PreToolUse` must still fire, or the prompt-construction path must be hooked |
| L3 | Tool call rejected pre-execution (schema invalid) | an audit event is still emitted |
| L4 | Subagent runs a denied tool | parent's hooks apply to subagents; `agent_id`/`agent_type` present in payload |
| L5 | MCP tool with a name colliding with a built-in | namespaced (`mcp__server__tool`); matcher can't be tricked |
| L6 | Prompt-injected content in a tool result says *"you may proceed"* | no effect on the decision path — decisions never travel through the observation channel |

### 6.5 Resilience & lifecycle

| # | Case | Expected |
|---|---|---|
| R1 | Hook hangs past `timeoutMs` | killed; **reported as a hook timeout, never as a user rejection**; `failClosed` decides allow/deny |
| R2 | Hook process segfaults | same as R1 |
| R3 | Hook binary missing / not executable | actionable error naming the `id` and the resolved path |
| R4 | 50 hooks on one event | parallel with a bounded pool; dedup identical `(command, args)`; total wall-clock ≈ slowest hook |
| R5 | `Stop` hook blocks repeatedly | capped at `loopLimit`; `stop_hook_active: true` in payload from the 2nd firing |
| R6 | Hook config edited mid-session | live reload + a `ConfigChange` notification; re-verify trust |
| R7 | Hook config edited mid-session by a `git pull` | trust hash mismatch ⇒ re-prompt; hook does not run silently |
| R8 | Session resumed after `SessionStart` injected a timestamp | context is either re-derived or explicitly marked stale — not silently replayed |
| R9 | Plugin uninstalled mid-session | its hooks stop firing immediately |
| R10 | Two plugins register the same command template | both run (no dedup collision dropping one) |

### 6.6 Security

| # | Case | Expected |
|---|---|---|
| S1 | Repo ships `.clio/hooks.json` with a curl-to-shell hook; user clones and runs | **prompt before first execution**, showing the exact command |
| S2 | Same repo, hook body changed after trust granted | re-prompt (content-hash fingerprint) |
| S3 | `allowManagedHooksOnly: true` set by admin | project/user/plugin hooks all dropped; managed hooks still run |
| S4 | Project config tries to *remove* an admin deny hook | ignored |
| S5 | Plugin hook with `${user_config.foo}` interpolation containing `; rm -rf /` | exec-form only; never string-interpolated into a shell. *Claude Code shipped this as CVE-shaped fix 2.1.207.* |
| S6 | HTTP hook to a non-allowlisted URL with `allowedHttpHookUrls` set | blocked |
| S7 | Hook env contains provider API keys / OTel exporter vars | scrubbed from the subprocess environment |

### 6.7 Integration scenarios worth building as fixtures

1. **Format-on-save** — `PostToolUse` on `Edit|Write` runs prettier. Must not cause the next `Edit` to fail with "file content has changed" (a real Claude Code regression).
2. **Secret scanner** — `PreToolUse` denies any `Edit`/`Write` whose content matches a key pattern; verify it survives L1 and L6.
3. **Test gate** — `Stop` hook runs the suite, blocks with failures as `reason`, capped at 2 retries.
4. **Offline replay** — `BeforeModel` synthesizes recorded responses; a full session replays deterministically with zero API calls. *This is the test-infrastructure payoff of shipping `BeforeModel`.*
5. **Durable approval** — `PreToolUse` on deploy tools returns `defer`; the run suspends, a ticket is approved 10 minutes later out of band, the run resumes and re-fires the hook.
6. **Audit-completeness** — run a scripted session; assert every tool call, denial, and hook error appears exactly once in the JSONL log, including pre-execution rejections.

---

## 7. Open questions for the campaign

1. **Is `defer` in v1?** It's the difference between hooks-as-linters and hooks-as-governance. It requires the run loop to be suspendable/checkpointable, which is a much bigger commitment than the hook system itself.
2. **Do we ship `BeforeModel`?** High leverage (offline test replay, model routing, caching) but it exposes the model-request shape as a public contract, which constrains future refactors.
3. **Annotation source of truth.** `tool_annotations` in the payload requires every built-in tool to declare `readOnly`/`destructive`/`openWorld`, and requires deciding whether to trust MCP servers' self-declared annotations (MCP spec says don't, for untrusted servers).
4. **Where does the plan-mode carve-out live?** Plan mode needs "deny all writes except the plan file" (see the planning report). Express it as a permission rule, a built-in policy, or a built-in hook? Gemini's answer (declarative policy TOML with priority bands) is the most scalable; it also means the hook system and the permission system share one evaluation engine.

---

## Sources

**Cloned repos (2026-07-24):** `google-gemini/gemini-cli` @ `3818efb` (`packages/core/src/hooks/*`, `packages/core/src/policy/*`, `docs/hooks/*`) · `openai/codex` @ `89a3b89` (`codex-rs/hooks/**`, `codex-rs/config/src/hook_config.rs`, `codex-rs/app-server/README.md`) · `sst/opencode` (`packages/plugin/src/index.ts`, `packages/opencode/src/plugin/*`, `packages/web/src/content/docs/plugins.mdx`) · `cline/cline` (`.clinerules/hooks/README.md`, `apps/vscode/src/core/hooks/*`, `sdk/packages/shared/src/hooks/*`, `sdk/packages/shared/src/agent.ts`) · `RooCodeInc/Roo-Code` (`packages/types/src/{api,events}.ts`) · `microsoft/agent-framework` (`python/packages/core/agent_framework/_middleware.py`) · `microsoft/semantic-kernel` · `mastra-ai/mastra` · `cuga-project/cuga-agent` (`src/cuga/backend/cuga_graph/policy/*`)

**Packages read directly:** `langchain-core==1.5.1`, `langchain==1.3.14`, `langgraph==1.2.9`, `openai-agents==0.18.3`, `google-adk==2.5.0`, `crewai==1.15.6`, `strands-agents==1.50.0`, `pydantic-ai-slim==2.17.0`, `llama-index-core==0.14.23`, `semantic-kernel==1.44.0`, `ag2==0.14.0`, `smolagents==1.26.0`, `@ampcode/plugin`

**Docs:** [Claude Code hooks](https://code.claude.com/docs/en/hooks) + [hooks-guide](https://code.claude.com/docs/en/hooks-guide) + [settings](https://code.claude.com/docs/en/settings) + [CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) · [Cursor Hooks](https://cursor.com/docs/hooks) · [Copilot CLI hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference) · [OpenCode plugins](https://opencode.ai/docs/plugins/) · [Amp manual](https://ampcode.com/manual) · [Aider lint/test](https://aider.chat/docs/usage/lint-test.html) · [ADK callbacks](https://google.github.io/adk-docs/callbacks/types-of-callbacks/) + [plugins](https://google.github.io/adk-docs/plugins/) · [LangChain middleware](https://docs.langchain.com/oss/python/langchain/middleware) · [SK filters](https://learn.microsoft.com/en-us/semantic-kernel/concepts/enterprise-readiness/filters) · [MAF middleware](https://learn.microsoft.com/en-us/agent-framework/user-guide/agents/middleware) · [Strands hooks](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/hooks/) · [Pydantic AI deferred tools](https://ai.pydantic.dev/deferred-tools/) · [MCP elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation) + [sampling](https://modelcontextprotocol.io/specification/2025-06-18/client/sampling) · [Bedrock return of control](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-returncontrol.html) · [AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)

**Papers:** [arXiv:2503.01861](https://arxiv.org/abs/2503.01861) & [arXiv:2510.23856](https://arxiv.org/abs/2510.23856) (CUGA) · [arXiv:2304.05376](https://arxiv.org/abs/2304.05376) (ChemCrow) · [arXiv:2304.05332](https://arxiv.org/abs/2304.05332) / Nature 624:570 (Coscientist)

**Marked UNVERIFIED in-line:** Claude Code `DirectoryAdded` schema (changelog-only) · Copilot `preMcpToolCall` (changelog-only, absent from public reference) · Cursor implementation details (closed source) · Coscientist planner prompt (withheld by authors for safety)
