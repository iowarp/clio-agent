# CLIO Remote Agents and Protocols

Oct 6, 2026 · @Jaime

A2A is the protocol CLIO should build peers and exposure on. Academy is supported natively, so agents running on DOE machines are first-class peers. The Agent Client Protocol connects editors to CLIO and lets CLIO drive local coding agents.

## What CLIO needs

CLIO reaches external agents in two ways. An `external-agents` pack lets any agent discover and call the agents a user has connected. A plugin can also declare **specific peers** it needs, such as a Tosca runner for the manufacturing plugin, and the user binds their address and credentials. Separately, an agent can **expose** itself to other systems.

The protocols are judged against five needs:

- **Long-running work.** HPC jobs run for hours to days, so a task must survive client disconnects and be reattached later.
- **One delegation interface.** A peer should look like a defined subagent to the root agent, whether it runs locally, in the cloud or at Argonne.
- **Runtime binding.** The agent names a peer; the operator supplies its address and credentials.
- **Firewalled compute nodes.** Peers on Aurora or Polaris often cannot accept inbound connections.
- **Exposure for free.** CLIO agents should be reachable by other systems from a tick mark, with their description generated from the resolved spec.

## A2A

A2A covers almost everything CLIO needs for agent-to-agent work, and its task model matches long-running HPC jobs better than anything else available. The current release is [spec v1.0.1](https://github.com/a2aproject/A2A/releases) (May 28, 2026), following v1.0.0 on March 12, 2026, the first major version with many breaking changes.

**Governance.** Google created A2A and donated it to the Linux Foundation, which [launched the project](https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents) on June 23, 2025. A Technical Steering Committee has one voting member each from Google, Microsoft, Cisco, AWS, Salesforce, ServiceNow, SAP and IBM ([GOVERNANCE.md](https://github.com/a2aproject/A2A/blob/main/GOVERNANCE.md)).

**Agent Card.** Published at `/.well-known/agent-card.json`. Required fields are `name`, `description`, `supportedInterfaces`, `version`, `capabilities`, `defaultInputModes`, `defaultOutputModes` and `skills`. Capabilities flag streaming, push notifications, extensions and an extended card that clients fetch after authenticating. Cards can be signed with JWS. This maps directly onto CLIO exposure: the card can be generated from the resolved spec.

**Tasks.** A Task carries status, artifacts and history. Messages and artifacts are made of Parts (text, raw bytes, URL or JSON data). The states are submitted, working, completed, failed, canceled, input-required, rejected and auth-required. `contextId` groups related tasks.

**Bindings.** JSON-RPC, gRPC and HTTP+JSON, with the same method names across them: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, push notification config methods and `GetExtendedAgentCard` ([specification](https://a2a-protocol.org/latest/specification/)).

**Long-running work.** Clients poll, stream over SSE, or receive webhooks. `SubscribeToTask` reattaches to a running task, and its first event is always the current Task snapshot. The spec calls itself designed for very long-running tasks, but it has no progress fields, no replay of missed stream events, optional webhook retries and implementation-defined retention.

**Auth.** API key, HTTP auth, OAuth2 (authorization code with PKCE, client credentials, device code), OpenID Connect and mutual TLS. A task can pause in auth-required to ask for credentials midway.

**Discovery.** Well-known URI, curated registries or direct configuration; no registry API is defined.

**SDK.** Python is [`a2a-sdk`](https://pypi.org/pypi/a2a-sdk/json) 1.2.2 (Oct 5, 2026), implementing spec 1.0 over all three bindings with a 0.3 compatibility mode.

## ACP

Two unrelated protocols share the abbreviation. The one for agent-to-agent communication is gone, merged into A2A. The one that matters for CLIO is the Agent Client Protocol, which connects editors to agents, so it is an exposure target and not a peer protocol.

**Agent Communication Protocol (IBM, BeeAI).** A REST protocol (`GET /agents`, `POST /runs`) with synchronous, streaming and background runs. IBM's team [announced the merger into A2A](https://github.com/orgs/i-am-bee/discussions/5) on August 25, 2025, archived [the repository](https://github.com/i-am-bee/acp) two days later, and stopped development; BeeAI users migrate through A2A adapters. CLIO should not build on it.

**Agent Client Protocol (Zed).** The editor equivalent of LSP: an editor talks to a coding agent over JSON-RPC, today on stdio, with Streamable HTTP still a draft proposal ([transports](https://agentclientprotocol.com/protocol/transports)). The agent implements `initialize`, `authenticate`, `session/new` and `session/prompt`, and streams `session/update` notifications with message chunks, tool calls and plans. The client implements permission requests, and optionally file system, terminal and elicitation methods.

Adoption is broad: the [clients page](https://agentclientprotocol.com/overview/clients) lists Zed, JetBrains, Neovim, Emacs, VS Code and others, and [41 agents](https://agentclientprotocol.com/overview/agents) including Claude Agent, Gemini CLI, Codex CLI and GitHub Copilot. The Python SDK is [`agent-client-protocol`](https://pypi.org/project/agent-client-protocol/) (stable 0.12.1, with 1.0.0 release candidates in September 2026); protocol version is 1.

For CLIO this means one more exposure tick mark: `Expose(acp=True)` would let any of those editors drive a CLIO agent, with ACP's permission requests mapped onto CLIO grants. Because the transport is local stdio, it suits CLIO running on the user's machine, not a remote peer.

In the other direction, the `acp` plugin in the `external-agents` pack lets CLIO drive any of those local coding agents, such as Claude Code, Codex or Gemini CLI, as subagents over stdio.

A third ACP, the Agentic Commerce Protocol from Stripe and OpenAI, covers checkout flows and is not relevant.

## Academy

Academy is the strongest option for agents that live on DOE machines, but it is a Python actor framework rather than an agent protocol, so CLIO supports it natively through an adapter. It comes from Globus Labs (University of Chicago and Argonne), is MIT licensed, and reached [`academy-py` 1.0.0](https://pypi.org/project/academy-py/) on September 8, 2026.

**Agent model.** Agents subclass `Agent`, expose remotely callable `@action` methods, and run autonomous `@loop` methods until shutdown ([concepts](https://docs.academy-agents.org/stable/concepts/)). Callers hold a `Handle` and await action results directly. Messaging is request and response only; pub/sub and streaming actions are open issues.

**Exchanges.** Agents reach each other through an exchange: local, Redis, a hybrid of direct TCP with Redis fallback for peers behind NAT or firewalls, or an HTTP exchange using SSE, with a hosted instance at exchange.academy-agents.org. The HTTP exchange authenticates with Globus OAuth tokens. Agents discover one another through the exchange's query methods.

**HPC deployment.** A `Manager` launches agents onto any executor, including Parsl and Globus Compute ([HPC guide](https://docs.academy-agents.org/stable/guides/hpc/)). The [Academy paper](https://arxiv.org/html/2505.05428v2) ran agents on Aurora and Polaris, reporting 5.5 ms warm starts and about 385 µs action latency, with case studies in materials discovery (MOFA) and decentralized learning across more than 1,500 agents. Persistent agents can outlive the application that launched them.

**LLM and protocol integration.** The [LLM guide](https://docs.academy-agents.org/stable/guides/llm/) shows agents wrapped as LangChain tools by hand, since handles carry no docstrings or signatures, and an MCP plug-in lives in a separate extensions repository. No A2A support was found.

**Limits as a CLIO peer.**

- Python only, with pickle as the default serialization (alternatives since 0.5.0).
- No streaming or progress events, only request and response.
- Globus Auth consent prompts and 48-hour token expiry need refresh handling for long runs.
- Fault tolerance is left to the application.
- Action descriptions for an LLM must be supplied by hand.

## Comparison

Only A2A serves as a general peer protocol; Academy covers HPC-resident agents and the Agent Client Protocol covers editors.

| Property | A2A | Academy | Agent Client Protocol |
| --- | --- | --- | --- |
| Role for CLIO | Peers and exposure | Peers on DOE machines | Exposure to editors; driving local coding agents |
| Status | Spec 1.0.1, Linux Foundation | 1.0.0, Globus Labs | Protocol v1, community |
| Transport | JSON-RPC, gRPC, HTTP+JSON | Exchange: Redis, TCP, HTTP+SSE | JSON-RPC on stdio; HTTP in draft |
| Long-running tasks | Task states, resubscribe with snapshot, webhooks | Persistent agents; request/response only | Session-scoped |
| Streaming | SSE | Not yet | `session/update` notifications |
| Behind firewalls | Needs a reachable endpoint; webhooks hard on compute nodes | Hybrid and HTTP exchanges relay through the exchange | Local process only |
| Self-description | Signed Agent Card with skills | Discovery queries; no LLM-ready descriptions | `initialize` capabilities |
| Auth | OAuth2, OIDC, API key, mTLS | Globus OAuth | `authenticate` method |
| Language | Six official SDKs | Python only | Rust, TypeScript, Python, Java, Kotlin |

## Design for CLIO

External agents are reached through plugins, like every other capability. The `external-agents` pack gives an agent discovery and delegation over A2A, ACP and Academy, and a plugin that always needs a specific peer declares it. The agent itself owns only its exposure.

```python
# manufacturing plugin, ai.iowarp.clio/peers/
tosca_runner = Peer("tosca-runner", protocol="a2a",
                    skills=["topology optimization"])
aurora_sim = Peer("aurora-sim", protocol="academy",
                  actions={"run_case": "Run one simulation case and return its outputs"})

# the agent owns only its exposure
class Factorio(ScienceAgent):
    plugins = ScienceAgent.plugins + ["manufacturing@^1"]
    expose = Expose(a2a=True, acp=True)
```

```yaml
# operator binding, never in the agent definition
peers:
  tosca-runner:
    card: https://<host>/.well-known/agent-card.json
    auth: oauth2-client-credentials
  aurora-sim:
    exchange: globus
    agent: <academy agent id>
```

**Discovery, not fragments.** Peers describe themselves: A2A through Agent Cards, Academy through exchange discovery queries. No peer contributes a prompt fragment. The `external-agents` pack contributes one, telling the agent it has tools to discover and delegate to the agents the user has connected, conditional on which protocol plugins are present. Because Academy handles carry no descriptions, an Academy peer's actions are described in its declaration.

**One delegation interface.** The root agent delegates to `tosca-runner` exactly as it would to a defined subagent. Results come back as CLIO artifacts and events, whichever protocol carried them.

**Binding is a requirement.** A peer declared by a plugin is required configuration. The UI asks the user for its address and credentials, and the session does not start until the binding is filled or the plugin that needs it is removed. At session start the runtime fetches each bound peer's Agent Card, or queries the Academy exchange, and checks that the declared skills or actions are present.

**Location.** The agent never fixes where a peer runs. It may state a preference, such as `prefers=Prefers(locality="hpc")`, the same way it states model preferences; the operator decides.

**Long-running tasks.** A peer call is an external task in CLIO's task layer. It returns a handle at acceptance and is observed, waited on and cancelled with the same task controls as any other work, and its result reaches the caller through the mailbox. CLIO stores the A2A task id and `contextId` as the task's backend reference, polls or resubscribes from the server side, and treats the snapshot returned on resubscribe as the source of truth. An observed terminal state settles as externally acknowledged; lost visibility stays uncertain rather than becoming a failure, and an uncertain submission is never sent again. Webhooks are not used toward compute nodes, since they rarely accept inbound connections. Because A2A defines no progress fields, CLIO reads progress from status messages and partial artifacts.

**Academy peers.** An adapter turns an Academy handle into a CLIO external task with the same controls. With no streaming, the task reports working until the action returns. Action descriptions come from the `Remote` declaration, since Academy handles carry none, and Globus refresh tokens live in the operator's credentials to survive the 48-hour expiry.

**Exposure.** `Expose(a2a=True)` publishes an Agent Card generated from the resolved spec: the role becomes the description, and plugins and defined subagents become skills. The card is signed with the deployment's key. `Expose(acp=True)` ships a local stdio adapter so editors can drive the agent, mapping permission requests onto CLIO grants.

**Trust.** Everything a peer returns is data, never instructions to the agent, the same boundary that applies to tool results.

## Decisions from review

- **Academy is supported natively.** CLIO is a science platform, and agents on DOE machines should be first-class peers rather than reached through a second protocol.
- **Missing peers block the session.** A required binding is surfaced in the UI for the user to fill; the agent does not silently run without it.
- **Peers are discovered, not described in the prompt.** Only the `external-agents` pack contributes a fragment.
- **Specific peers belong to plugins.** Agents own only their exposure.
- **Progress for long jobs is not available** through A2A, and CLIO accepts that.
- **ACP exposure is local only** until Streamable HTTP lands in the Agent Client Protocol.

## Sources

All opened October 2026.

- [A2A specification](https://a2a-protocol.org/latest/specification/), [releases](https://github.com/a2aproject/A2A/releases), [governance](https://github.com/a2aproject/A2A/blob/main/GOVERNANCE.md), [discovery](https://a2a-protocol.org/latest/topics/agent-discovery/), [A2A and MCP](https://a2a-protocol.org/latest/topics/a2a-and-mcp/), [SDKs](https://a2a-protocol.org/latest/sdk/), [Linux Foundation launch](https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents)
- [ACP merger announcement](https://github.com/orgs/i-am-bee/discussions/5), [ACP repository](https://github.com/i-am-bee/acp), [LF AI and Data post](https://lfaidata.foundation/communityblog/2025/08/29/acp-joins-forces-with-a2a-under-the-linux-foundations-lf-ai-data/)
- [Agent Client Protocol overview](https://agentclientprotocol.com/protocol/overview), [transports](https://agentclientprotocol.com/protocol/transports), [clients](https://agentclientprotocol.com/overview/clients), [agents](https://agentclientprotocol.com/overview/agents), [Python SDK](https://pypi.org/project/agent-client-protocol/)
- [Academy repository](https://github.com/academy-agents/academy), [PyPI](https://pypi.org/project/academy-py/), [concepts](https://docs.academy-agents.org/stable/concepts/), [HPC guide](https://docs.academy-agents.org/stable/guides/hpc/), [LLM guide](https://docs.academy-agents.org/stable/guides/llm/), [persistent agents](https://docs.academy-agents.org/stable/guides/persistent/), [paper](https://arxiv.org/html/2505.05428v2)
