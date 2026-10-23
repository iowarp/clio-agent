# Plugins, Agent SDK and flows

Design documents for replacing blueprints with Python-defined agents and plugins, and for orchestration written as flows on the task layer. Start with [megachange-plan.md](megachange-plan.md), which says what to build, in what order, and what is out of scope.

| Document | Covers |
| --- | --- |
| [megachange-plan.md](megachange-plan.md) | Implementation plan, rules, phases and acceptance |
| [plugins-and-composition.md](plugins-and-composition.md) | Plugin format, packs, agents as plugin sets, composed prompt, trust |
| [agent-sdk.md](agent-sdk.md) | Python classes, ownership, resolution and validation |
| [orchestration-semantics.md](orchestration-semantics.md) | Flows, `ctx` primitives on the task layer, plans, research survey |
| [remote-agents-and-protocols.md](remote-agents-and-protocols.md) | A2A, ACP and Academy (context; a later campaign) |
| [long-term-memory-plan.md](long-term-memory-plan.md) | Long-term memory: store, tools, background pass, export and import, UI, usage tracking |
| [memory-ledger.md](memory-ledger.md) | Decisions and acceptance evidence for long-term memory |

These are exported from the shared design docs as of Oct 20, 2026. Edit them here from now on; the repository copy is the one the implementation follows.
