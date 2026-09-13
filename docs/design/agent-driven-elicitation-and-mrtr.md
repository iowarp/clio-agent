# MCP v2 MRTR and agent-driven elicitation

How CLIO lets an MCP server ask a question mid-tool-call, and how that question can
be answered by the **session's own agent** rather than only the human.

This exists because the pattern is easy to get wrong: the modern MCP era removed the
old elicitation back-channel, and CLIO's agent-answerer is a **custom CLIO path**, so
reading the SDK alone will mislead you. (It misled two passes of investigation on
#1325 — hence this document.)

---

## The two layers

A tool that needs more input mid-flow ("this result is 92K tokens — how do you want
to narrow it?", "which of these candidates?") goes through two independent layers:

1. **Transport — MCP v2 MRTR** (Multi-round Tool-input Retry, #1114 / SEP-2322): the
   modern-era way a tool requests more input and is re-invoked. This is the wire
   mechanism; it is provider- and audience-agnostic.
2. **Audience — human vs agent** (#1309, C1-S7): *who* answers the question. Default
   is the **human** (HITL, unchanged behavior). A server can opt a specific question
   to be answered by the **session's agent** — "agent-driven elicitation."

---

## Transport: MRTR (`input_required`), not `ctx.elicit`

The modern MCP era (2026-07-28, SEP-2577) **removed the elicitation back-channel**.
On a modern connection `fastmcp`'s `ctx.elicit()` **raises** (`fastmcp/server/context.py`
`_ELICIT_MODERN_ERROR`). Do **not** use it. The sanctioned pattern is the **guard
pattern**:

- A tool returns an **`InputRequiredResult`** (`fastmcp/server/context.py`) carrying
  its `input_requests` (the requested schema) and an opaque `request_state`, instead
  of a final result.
- CLIO's client loop drives the round-trip: `mcp.client._input_required` (wired in
  `src/clio_agent/tools/mcp_executor.py`), bounded by
  `tools.mcp.input_required_max_rounds` (env `CLIO_MCP_INPUT_REQUIRED_MAX_ROUNDS`,
  default = the SDK's 10; `src/clio_agent/tools/mcp_runtime.py::input_required_max_rounds`).
- The client collects the answer, re-invokes the tool with `inputResponses` and the
  echoed `request_state`; the server resumes unchanged.
- **Bounded:** exhausting the rounds surfaces as the typed
  `MCPInputRequiredRoundsExceededError` (`src/clio_agent/tools/mcp_errors.py`,
  reason `mcp_input_required_rounds_exceeded`).

Availability: the guard pattern (`InputRequiredResult` + `ctx.input_responses` +
`request_state`) is present in **fastmcp >= 4.0** — it is in the current `4.0.0b5`
baseline and in later releases. Use it on whatever fastmcp version clio-kit ships.
(This is a statement of *compatibility from 4.0 onward*, NOT a reason to hold fastmcp
at any version — keep upgrading fastmcp normally; the pattern continues to work.)

## Audience: default human, opt-in agent

By default a form-mode elicitation is answered by the **human**. A server marks a
question as agent-answerable with a reverse-DNS vendor `_meta` key (CLIO's own
convention — deliberately **not** the reserved `io.modelcontextprotocol/*` namespace):

```json
{"_meta": {"x-clio-agent/audience": "agent"}, "mode": "form", "requestedSchema": { ... }}
```

- Absence of the key, or **any** value other than exactly `"agent"`, is identical to
  today's behavior: the human is asked, nothing agent-side fires, nothing new is
  recorded (`decide_routing` returns a no-route decision).
- Routing keys **only** on the typed `audience` field + client policy — **never on
  the question's content** (superseding principle: no keyword/phrase matching).

Constants: `AGENT_AUDIENCE_META_KEY` / `AGENT_AUDIENCE_VALUE`
(`src/clio_agent/gact/agent_elicitation.py`).

## Negotiation: clio advertises the capability, the server gates on it

A tool must not blindly return an `InputRequiredResult` — a client that cannot
drive the MRTR loop (a legacy or non-clio client) would be left with an
unresolvable result. So the capability is **negotiated at the MCP handshake**, the
standard way MCP is meant to degrade:

- **clio advertises** a vendor extension — `x-clio-agent/agent-driven-elicitation`
  (`AGENT_ELICITATION_EXTENSION_ID`, `tools/mcp_extension_registry.py`) — on
  **every execution-path client** (registry entry #3, ad-only and unconditional,
  alongside `tasks` and `ui`). Declaring it asserts: *this client drives the MRTR
  `InputRequiredResult` loop and honors the `x-clio-agent/audience` hint (agent-first,
  human terminal fallback).* It is a **client capability advertisement**, distinct
  from the per-request `x-clio-agent/audience` `_meta` hint the SERVER sends.
- **the server reads it** with `ctx.client_supports_extension(
  "x-clio-agent/agent-driven-elicitation")` and gates its size-guard on it:
  **present** → return an `InputRequiredResult` tagged `audience: agent`; **absent**
  (any generic / non-clio client) → return the full result, so the tool still works
  for everyone. This is what makes the size-guard **cross-agent safe**.

Why an extension and not the standard `elicitation` capability: the modern era
removed the server-initiated elicitation back-channel, so `ClientCapabilities.
elicitation` no longer implies "can answer a mid-call question" — and it cannot
express *agent* answering specifically. The vendor extension is the honest,
precise signal. (Historically the transport + routing shipped without this
advertisement, so a size-guard had no way to tell a clio client from a generic one
— the guard "worked" in tests only because the MRTR driver runs ungated. The
extension closes that negotiation gap; #1325.)

## Client policy gates it

Per-server opt-in on the config seam (`clio_agent.conf`), not on the declared server
spec:

- Global enable defaults **ON** — `tools.mcp.elicitation.agent_audience.enabled`
  (env `CLIO_MCP_ELICITATION_AGENT_AUDIENCE_ENABLED`). The owner's posture: *the
  capability existing is the point.*
- Per-server **deny list** as the opt-out —
  `tools.mcp.elicitation.agent_audience.denied_servers`
  (env `CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DENIED_SERVERS`).
- Every routing decision is a **typed, recorded** event on the session bus
  (`ROUTED_REASON` / `FALLBACK_REASON`, `on_question_published`) — never silent.

## Fulfillment: an observable, tool-less agent step

A routed question is answered by a **real, bounded child turn of the same session's
own expert** — self-directed (`skip_declared_check=True`), spawned through the
existing invocation machinery (`InProcessExpertInvoker` over
`spawn_child_turn_threadsafe`), seeded with a bounded excerpt of the answering
session's transcript so it runs "through the normal loop" on the user's provider.

- **The answer turn is TOOL-LESS** (`TaskSpec.tool_allowlist=()` stamped at spawn
  mint time; `resolution._apply_session_tool_allowlist`). The server's `message`
  rides into the answer prompt **verbatim**, so a prompt-injected elicitation can
  only ever produce a schema-validated value — it can never drive a tool call.
- The answer is validated against the server's `requestedSchema`
  (`elicitation_schema.validate_elicitation_answer`) **exactly** as a human's is; a
  failing answer falls back to the human, typed (`AGENT_ELICITATION_FALLBACK_DETAILS`).
- It feeds the **same** atomic answer primitives the human route uses
  (`elicitation_bridge.claim_question_transition` + `resolve_elicitation`), so the
  MRTR retry resumes the server unchanged, and the answer is transcript-visible with
  typed attribution (`UserQuestion.answered_by == "agent"`).

## The semantic firewall (design invariant — do not break)

Agent-driven elicitation is **NOT sampling** and must never become an inference
channel. The MCP server gets **no** model access, **no** free-form completions, **no**
prompt control — it gets exactly what elicitation always gave it: a typed,
schema-validated answer to its **own declared question**, with the agent as a
permitted answerer alongside the human.

The tool-less answer turn, the bounded context excerpt, and schema validation are
**load-bearing**: even a `{"type":"string"}` field (the widest shape `requestedSchema`
permits) is confined by its declared `minLength`/`maxLength` and by the fact that the
answering turn can do nothing but emit a value. No `createMessage`/sampling
vocabulary appears anywhere in this path (ratchet:
`tests/test_tools/test_mcp_era_gated_removals.py` must stay green).

## Safety

- **Recursion:** a bounded `agent_elicitation_depth` rides the child session's
  metadata; routing happens only while depth < `_max_depth` (default 1), else human
  fallback (`recursion_depth_exceeded`).
- **Every failure mode** — decline, unparseable/schema-invalid reply, spawn refusal,
  timeout, unexpected error — **falls back to the human**, typed, and never drops or
  loops the question. The human is the **terminal fallback**.
- **`url`-mode elicitation is out of scope** for agent routing regardless of the
  audience hint (`url_mode_requires_human_consent`): opening a URL is a human-consent
  action, not something an LM's typed answer can grant.

---

## Using it from an MCP tool — the size-guard pattern (#1325)

For a file/data tool that can return too much (e.g. `pandas_filter_data` dumping all
1,101 catalog rows ≈ 23K tokens and overflowing a local model's window):

1. Compute the result; **estimate its size** (rows × cols, serialized bytes, ~tokens).
2. If within budget → return the lean result. The on-disk side-effect (the written
   file) happens **once**, unaffected by any of the below.
3. If over budget → **check the negotiation signal first**:
   `ctx.client_supports_extension("x-clio-agent/agent-driven-elicitation")`.
   - **Absent** (generic / non-clio client) → return the full result. The tool
     stays universal; a client that can't drive MRTR is never handed one.
   - **Present** → return an **`InputRequiredResult`** whose form-mode request:
     - carries `_meta {"x-clio-agent/audience": "agent"}`, and
     - declares a flat `requestedSchema` for narrowing — a row filter, `top_n`, a
       column subset, or a `group_by`+`agg`.
4. CLIO routes it to the session's agent (per policy), the agent returns a
   schema-validated narrowing, the SDK re-invokes the tool with `inputResponses`, and
   the tool returns **only** the bounded result.

This keeps tool outputs lean **by construction** and works for any model regardless
of context window. It is the correct fix for the "fat tool output overflows the
context" class (#1325) — not a `max_rows` band-aid, and not `ctx.elicit`.

---

## References

- Code: `src/clio_agent/gact/agent_elicitation.py` (routing + fulfillment, with the
  authoritative module docstring), `elicitation_bridge.py`, `elicitation_correlation.py`,
  `elicitation_schema.py`, `agent_elicitation_reply.py`;
  `src/clio_agent/tools/mcp_executor.py`, `mcp_errors.py`, `mcp_runtime.py`,
  `mcp_handlers.py`.
- Issues: #1114 (MRTR / SEP-2322), #1309 (agent-driven elicitation, C1-S7), #1325
  (fat tool output — the size-guard use case).
- SEP-2577 (modern era removed the elicitation back-channel); SEP-2322 (MRTR).
