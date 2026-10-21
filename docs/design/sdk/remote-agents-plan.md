# Remote agents: implementation plan

This plan builds the external-agent boundary described in [remote-agents-and-protocols.md](remote-agents-and-protocols.md): peers declared by plugins and reached over A2A, Academy and ACP, and CLIO agents exposed over A2A and ACP. It is the second push after the plugins, Agent SDK and flows change, and it extends that change instead of adding beside it.

## Contract and base

The design document is the contract, under the same rule as [megachange-plan.md](megachange-plan.md): when code and design disagree, change the code; when the design is wrong, fix the document in the same change and record the decision in the ledger. Work runs to completion without stopping for review.

Base: `claude/sdk-plugins-flows` once its goal is met. If that branch has merged, rebase onto its merge target. Read megachange-plan.md, its acceptance ledger (decisions D1 to D23 bind this work too), and `plugin-change-delivery.md` before writing code.

## Rules

Every rule in megachange-plan.md applies. Three more:

1. **CLIO never talks to CLIO through these protocols.** Do not route a CLIO-to-CLIO call through A2A, ACP or Academy, and do not build a test that does.
2. **A peer call is a task-capable MCP call** served by its protocol plugin's MCP server. Add no `TaskKind`, task driver, registry or polling loop outside that server. Recovery, settlement and cancellation come from the existing external task path.
3. **Inbound requests get no extra rights.** An exposed agent runs under its own plugins and the deployment's sandbox and approval settings.

## Out of scope

Do not implement, stub or reserve fields for these:

- Federated clio-core and CLIO-to-CLIO transport. That is the clio-core federation work.
- A2A push notifications and webhooks, and OAuth2 flows for inbound A2A. Inbound requests authenticate with GACT's bearer token.
- ACP over Streamable HTTP.

## Defaults

| Question | Default |
| --- | --- |
| Where peer calls run | One task-capable MCP server per protocol plugin, started lazily |
| Peer in the call-target order | Right after defined subagents (extends D15) |
| Inbound A2A auth | GACT's bearer token |
| A2A context to CLIO session | One stable session per `contextId`, created on the first message |
| Card signing key | Configured key, or one CLIO generates on first exposure and stores in its home |
| Approvals for an exposed agent | A2A `input-required` with the request in the status message; ACP permission requests |
| Peer credentials | CLIO's existing credential store, referenced by name from the binding |

Record each default in the ledger as decisions D24 onward.

## Phase R0: inventory

1. Map the external MCP task path that peer calls will reuse: acceptance, observation, recovery, settlement, cancellation and input handling. Map the call-target resolution, the stage-2 resolver and the GACT auth used by REST routes.
2. Add rows R-* and the live scenarios E1 to E8 below to `docs/design/sdk/acceptance-ledger.md`. Commit, push and open a draft PR, then continue.

## Phase R1: SDK, resolution and the external-agents pack

1. Add `Peer` and `Expose` to `clio_agent.sdk`. `Peer` is plugin content read from `ai.iowarp.clio/peers/`; `Expose` is a field on `Agent` and part of `AgentSpec`.
2. A plugin that declares a peer gains a hard requirement on that peer's protocol plugin.
3. Add `peer` to the call-target order after defined subagents, both for `ctx.call` and for the model's delegation tool.
4. Add peer bindings to configuration and the credential store. Extend stage-2 resolution to block on `peer_unbound`, and to block on `peer_unavailable` when a card lacks a declared skill or an exchange lacks a declared action.
5. Ship the `external-agents` pack with the `a2a`, `acp` and `academy` plugins, each with its MCP server and one shared fragment. Remove decision D9's exclusion in the ledger.

## Phase R2: outbound A2A, Academy and ACP

1. `a2a` server: streaming send when the card supports it, `SubscribeToTask` with `GetTask` fallback, the A2A task id and `contextId` as backend reference, `CancelTask` on cancel, `input-required` as a Question, artifacts stored as CLIO artifacts.
2. `academy` server: a Handle per bound peer through the configured exchange (local, Redis, hybrid, HTTP), each action a task, exceptions as typed failures.
3. `acp` server: start the bound command, drive `initialize`, `session/new` and `session/prompt`, report `session/update` as progress, turn permission requests into Questions, and answer file system and terminal requests inside the session workspace.
4. Discovery tools in each plugin list the peers the user has connected, with their cards or actions.

## Phase R3: exposure

1. A2A: mount `a2a-sdk`'s server in GACT at `/a2a/<marketplace>/<agent>/` for each agent with `Expose(a2a=True)`. Generate and sign the card from the resolved spec. Map `contextId` to a session, messages to assignments, and `GetTask`, `ListTasks`, `SubscribeToTask` and `CancelTask` to the shared task operations. Map Questions and approvals to `input-required`, and artifacts to Parts.
2. ACP: add `clio-agent acp <marketplace/agent>`, a stdio adapter that talks to GACT through `clio_agent.client` and translates its event stream into `session/update`.

## Phase R4: UI in gact-tui

1. Add a peer binding form, reached from the blocked-session notice, the plugin tree and settings, with credentials stored by name.
2. Add exposure toggles in the agent details. These show the A2A endpoint and card link and the ACP command.
3. Show peer calls as task rows with their protocol, backend reference and settlement.

Verify every UI change with `ui_check.mjs` at 1440 by 900 and 390 by 844, as in megachange-plan.md.

## Phase R5: qualification

The external side of each scenario is a real program, not CLIO: an `a2a-sdk` agent or client, an `academy-py` agent on a local Redis exchange (`redis-server` is installed in the cloud image), an `agent-client-protocol` client script, and `@zed-industries/claude-code-acp`. Keep those programs under `tests/remote_agents/` so the scenarios can be rerun.

| ID | Scenario | Passes when |
| --- | --- | --- |
| E1 | A test plugin declares an A2A peer served by a local `a2a-sdk` agent that streams status for about a minute | A flow's `ctx.call` and the model's delegation both return the result as tasks with the A2A backend reference. `cancel_tasks` makes the agent observe `CancelTask`. A CLIO restart mid-call re-attaches, and the agent saw exactly one task. |
| E2 | Start a session with that peer unbound, then bound to a card missing a declared skill | The first is blocked with `peer_unbound` and the UI offers the binding form; the second with `peer_unavailable` naming the skill. After a correct binding the session starts. |
| E3 | An Academy peer on a local Redis exchange with one action that succeeds and one that raises | The first returns as a task result; the second is a typed failure carrying the exception. |
| E4 | The `acp` plugin drives `claude-code-acp` on a small edit in the workspace | Updates arrive as progress, a permission request appears as a Question and is answered, the edit lands inside the workspace, and the result returns as a task. |
| E5 | `Expose(a2a=True)` on the standard agent, driven by an `a2a-sdk` client | The card verifies against the deployment key and lists skills from the spec. A message completes. A second message on the same `contextId` lands in the same session. Streaming delivers status. A Question surfaces as `input-required`, and answering it completes the task. `CancelTask` cancels the CLIO subtree. |
| E6 | An `agent-client-protocol` client script drives `clio-agent acp clio/standard` | `initialize`, `session/new` and `session/prompt` work, updates stream, and a permission request is answered. |
| E7 | Agents with and without the `external-agents` pack | The fragment appears only with the pack, and the discovery tools list the bound peers with their cards or actions. |
| E8 | The binding form, exposure toggles and peer task rows | Screenshots read at both sizes show each one working, with no console errors or failed requests. |

Blocked rows, which a cloud session cannot reach: the Globus-authenticated Academy HTTP exchange, agents on DOE machines, and real editors driving the ACP adapter. Nothing else may be marked blocked.

## Done

The push is done when all of these hold, each shown in the session transcript, not only claimed:

1. Phases R1 to R4 are implemented, and ledger decision D9 is replaced by the decisions this push records.
2. The focused tests for every touched area pass, and `ruff check` and `ruff format --check` are clean on the touched paths.
3. Scenarios E1 to E8 pass against a freshly restarted stack, each with its commands, task handles and screenshots read.
4. Every new ledger row is passed apart from the blocked rows above.
5. Branches named `claude/remote-agents` are pushed in clio-agent and gact-tui, the submodule pin is updated, and the draft PRs carry a summary and the evidence.
6. A final report lists every new ledger row with its status and evidence.
