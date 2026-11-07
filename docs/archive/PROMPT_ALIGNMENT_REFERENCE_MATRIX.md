# Prompt Alignment Reference Matrix

Tracking issue: https://github.com/iowarp/clio-agent/issues/334

## Purpose

This matrix defines the public reference baseline for CLIO's prompt-alignment
phase. It is not the prompt rewrite itself. It records which public,
inspectable agent patterns should influence each CLIO prompt family once the
prompt registry, hierarchy, memory provenance, and ask-user/retry primitives are
available.

The goal is practical alignment: CLIO should behave like a mature agent system
where that helps users, while preserving its science-agent identity, tool
grounding, ARC memory semantics, and GACT/TUI observability.

## Reference Set

Use only public, inspectable material:

| Ref | Source | Public Pattern To Extract |
| --- | --- | --- |
| R1 | OpenAI Codex `AGENTS.md` docs: <https://github.com/openai/codex/blob/main/docs/agents_md.md> | Repository-scoped instructions, precedence, and durable project guidance. |
| R2 | OpenAI Codex CLI Help Center: <https://help.openai.com/en/articles/11096431> | Terminal-native coding-agent workflow, sandboxed repo tasks, and multimodal inputs. |
| R3 | Claude Code slash commands: <https://docs.anthropic.com/en/docs/claude-code/slash-commands> | User-defined command prompts as Markdown files, command namespace conventions, and MCP prompt exposure. |
| R4 | Claude Code subagents: <https://docs.anthropic.com/en/docs/claude-code/sub-agents> | Specialized subagents, scoped tool access, explicit delegation, and independent context. |
| R5 | Claude Code settings: <https://docs.anthropic.com/en/docs/claude-code/settings> | User/project configuration split, custom subagent files, and tool permission configuration. |
| R6 | Claude Code hooks: <https://docs.anthropic.com/en/docs/claude-code/hooks> | Lifecycle automation points, pre/post tool events, and user-owned safety checks. |
| R7 | MCP prompts: <https://modelcontextprotocol.io/docs/concepts/prompts> | Server-exposed prompt templates, prompt arguments, embedded resources, and slash-command presentation as a client choice. |
| R8 | MCP tools: <https://modelcontextprotocol.io/docs/concepts/tools> | Tool schemas, model-controlled tool calls, and clear separation between tools and prompts. |
| R9 | CLIO ARC memory docs: [ARC_MEMORY_LAYER.md](ARC_MEMORY_LAYER.md) | Durable memory tiers, retrieval, procedural memories, and context compilation expectations. |
| R10 | CLIO real-provider regression evidence: [REAL_PROVIDER_SEMANTIC_REGRESSION.md](archive/REAL_PROVIDER_SEMANTIC_REGRESSION.md) | Ground-truth failure modes for routing, tool evidence, provider fallback, and streaming claims. |
| R11 | CLIO command/capability audit (`COMMAND_CAPABILITY_AUDIT.md`, culled 2026-07 — see git history) | Truthful command availability, visible TODO commands, permissions, rewind, and unsupported voice semantics. |
| R12 | CLIO memory refinement design: [MEMORY_SYSTEM_REFINEMENT.md](archive/MEMORY_SYSTEM_REFINEMENT.md) | Context-frame truth, provenance, compaction events, and cross-session access constraints. |

## Prompt Family Matrix

| CLIO Prompt Family | References | Alignment Requirements |
| --- | --- | --- |
| System identity | R1, R2, R9 | State that CLIO is a science agent, not a generic chatbot. Keep instructions durable, concise, and scoped by repository/workspace/session provenance. |
| Planner/router | R1, R4, R8, R10 | Route to experts/tools from declared capabilities only. Prefer structured tools over guesses. Return routing errors honestly when no safe route exists. |
| Hierarchical parent experts | R4, R5, R12 | Present child experts as scoped delegation options. Parent prompts must explain when to answer directly, when to delegate, and what handoff metadata must be emitted. |
| Child expert prompts | R4, R8, R10 | Keep the child bounded to its specialty and tool allowlist. It should not re-route broadly unless explicitly granted delegation authority. |
| Tool-use prompts | R8, R10, R11 | Require telemetry-backed tool claims. Distinguish observed tool evidence from inference. Preserve file paths, dataset ids, variable names, artifact paths, and caveats exactly. |
| Command prompts | R3, R7, R11 | Treat slash commands as explicit user-invoked workflows. Keep TODO/unavailable commands visible but disabled; do not prompt the model to run unsupported commands. |
| Skills/user-defined agents | R1, R3, R4, R5 | Treat skills as reusable scoped instructions and agents as executable personas with explicit prompts/tools/model policy. Persist provenance for user-defined behavior. |
| Memory/context prompts | R9, R12 | Explain what context was included and why. Never make hidden cross-session memory implicit; cross-session retrieval must be explicit and provenance-bearing. |
| Compaction prompts | R9, R12 | Preserve exact scientific identifiers and evidence index entries. Do not invent continuity; mark summaries as compacted memory with event provenance. |
| Permission prompts | R5, R6, R8, R11 | Ask before destructive or policy-controlled actions. Reflect policy decisions in user-visible language without implying hidden authority. |
| Error/recovery prompts | R2, R10, R11 | Surface provider, tool, routing, and unsupported-capability failures directly with actionable next steps. Do not convert backend failures into confident answers. |
| Ask-user/retry prompts | R3, R4, R12 | Ask structured questions when required information is missing. Retry attempts must preserve original attempt, notes, model/provider changes, and recomputation warnings. |
| Prompt profiles | R1, R2, R10, R12 | Define `heavy`, `light`, `small_model`, and `fine_tuned` as behavioral profiles, not just token length variants. Small-model profiles need tighter schemas and less implicit reasoning. |
| TUI-visible prompt behavior | R3, R5, R6, R11, R12 | Align prompt wording with visible GACT state: permissions, command availability, memory pressure, context files, retry warnings, model fallback, and tool telemetry. |

## Behavioral Tradeoffs By Profile

| Profile | Behavior Target | Prompt Shape | Test Emphasis |
| --- | --- | --- | --- |
| `heavy` | Larger models and high-stakes science workflows. | Rich delegation criteria, explicit self-checking, evidence preservation, and detailed failure rules. | Multi-step routing, nested delegation, tool evidence, memory summaries, and model fallback disclosure. |
| `light` | Lower-latency local or smaller hosted models. | Short instructions, fewer examples, direct tool preference, and minimal narration. | Correct routing and honest failure without bloating the context. |
| `small_model` | Models that need explicit schemas and narrow action spaces. | Strict output contracts, enumerated actions, reduced ambiguity, and short capability lists. | JSON/schema adherence, no unsupported tool claims, and safe fallback on uncertainty. |
| `fine_tuned` | Models trained for a CLIO role. | Minimal role reminders, tool allowlist, and provenance requirements. | Regression against over-prompting and retained telemetry requirements. |
| `debug` | Development and benchmark runs. | Verbose rationale and provenance fields without changing final user-facing truth. | Reproducibility, traceability, and benchmark evidence capture. |

## Regression Scenarios

The prompt-alignment pass should add or update tests/benchmarks for:

1. Planner selects `data` for HDF5/NDP discovery and `analysis` for schema/statistics.
2. Parent expert delegates to a child only when the child has relevant tools.
3. Child expert refuses unsupported broad synthesis instead of inventing capabilities.
4. Tool-use answer cites only observed `tools_called` or telemetry events.
5. Context-file prompt preserves exact attached path and strips only user-facing `@path` syntax.
6. Compact-memory prompt preserves scientific identifiers near context limits.
7. Retry-with-model prompt warns about recomputation, TTFT, cost, and behavior drift.
8. Permission prompt asks before destructive actions and reflects policy-deny decisions.
9. TODO/unavailable slash commands remain visible but non-runnable.
10. Small-model profile returns valid planner/action schema under constrained context.
11. Heavy profile gives better delegation/tool-evidence behavior without losing exact identifiers.
12. Prompt provenance records prompt id, profile, source path/scope, model policy, and fallback reason.

## Non-Goals

- Do not copy or depend on private or questionable-source prompt material.
- Do not make public references a compatibility promise with another vendor.
- Do not change runtime prompts before the prompt registry can record provenance.
- Do not let prompt text override backend capability truth.

## Acceptance Gate For Prompt PRs

Every prompt-alignment PR should state:

- which prompt family it changes;
- which references above informed the change;
- which profile(s) changed;
- which regression scenarios were run;
- how prompt provenance is exposed;
- whether the change affects hierarchy, memory, commands, permissions, or retry behavior.
