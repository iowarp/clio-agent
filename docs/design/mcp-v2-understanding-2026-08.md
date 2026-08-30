# MCP v2 (the 2026-07-28 revision) — a working understanding

Written 2026-08-29 for the clio-agent unified-client campaign (iowarp/clio-agent#1274).
Companion: mcp-client-obligations-2026-07-28.md (the per-item obligations table).

## 1. The one big idea: statelessness

MCP v1 was a conversation between two stateful peers: an `initialize` handshake opened a
session, the session held negotiated state, the server could send requests *down* to the
client over the held connection, and HTTP deployments needed session ids and resumable
SSE streams to fake a persistent pipe over stateless infrastructure.

v2 re-founds the protocol: **every request is self-describing, and the server holds no
per-client state between requests.** Each request carries its own context in `_meta` —
protocol version, client capabilities, client info. There is no initialize, no session,
no `Mcp-Session-Id`, no SSE resumability. Anything that *is* stateful becomes an explicit,
named handle: a task id, a subscription id, an opaque `requestState` blob. The payoff is
that a server can be a lambda behind a load balancer, and the cost is that everything the
held connection used to do implicitly must now be done explicitly. The rest of the spec
is the working-out of that cost.

## 2. Discovery replaces the handshake

`server/discover` is the new front door: servers MUST implement it, clients MAY call it.
Version agreement is per-request, not per-session: the client states its version in
`_meta` on every request; a server that can't speak it refuses with `-32022`, and the
refusal *names the versions it supports* so the client can retry with a mutual one.
For coexisting with v1 servers, a dual-era client probes (discover on stdio; inspect the
400 body on HTTP), caches the era verdict per server, and falls back to legacy
`initialize` — with the rule that fallback must never key on one specific error code.

## 3. MRTR — the interaction inversion

The deepest change. A stateless server cannot initiate a request to the client — there
is no channel to speak down. So every server→client interaction is inverted into
**Multi Round-Trip Requests**: the server *answers* the client's request with
`resultType: "input_required"` plus a list of input requests; the client satisfies them
out-of-band (asks the human a form question, opens a URL with consent) and **retries the
original request** with the answers attached and the server's opaque `requestState`
echoed back, so a stateless server can resume exactly where it left off. `resultType`
absent means "complete" (legacy servers); unrecognized values are invalid.

This is why elicitation *survives* (as form-mode and URL-mode, delivered through MRTR)
while sampling, roots, and logging are *deprecated wholesale* (SEP-2577) — they were the
old server-initiated world, and nothing should build on them again.

## 4. Tasks — long work as an explicit handle (extension)

Long-running work no longer holds a connection open. Under the tasks extension
(`io.modelcontextprotocol/tasks`), each tool declares `taskSupport`
(forbidden / optional / required) right in its tools/list entry; a task-mode call
returns a handle immediately, and the client polls `tasks/get` (stamping `Mcp-Name`
with the task id on every task RPC), feeds mid-task input through the same MRTR shape,
and cancels by request. Crucially, tasks are an *extension*, not core — which leads to:

## 5. The extensions framework — negotiate once, generically

Capabilities are now an open-ended map of reverse-DNS-named extensions that both sides
declare; unknown extensions MUST degrade gracefully (ignored, never fatal). The official
registry today: tasks, **Apps** (`io.modelcontextprotocol/ui` — servers ship sandboxed
HTML UIs as `ui://` resources bound to tools; the host renders them in an iframe and the
UI speaks JSON-RPC back through the host's own consent path), OAuth client-credentials
(machine-to-machine auth), and enterprise-managed authorization. The design lesson the
framework encodes: a client should implement *extension negotiation* once, and each
extension is just an entry — hardcoding any single extension into the client's plumbing
is exactly the mistake the framework exists to prevent.

## 6. Transports

**Streamable HTTP**: every message is its own POST; the response is either JSON or an
SSE stream scoped to that one request. Standard headers ride outside the body so
infrastructure can route without parsing JSON: `MCP-Protocol-Version`, `Mcp-Method`,
`Mcp-Name`, and optionally `Mcp-Param-{name}` mirrored from tool arguments the server
annotated (`x-mcp-header`) — with the client duty of *excluding* tools whose header
annotations are invalid. Closing the response stream IS cancellation. A broken stream
loses the request: the client re-issues it as a NEW request with a new id — never
resumes (Last-Event-ID is gone).

**stdio**: newline-delimited JSON-RPC, nothing non-MCP on stdin, stderr is not an error
channel, cancellation via `notifications/cancelled`, an orderly shutdown ladder, and the
expectation that a client restarts a crashed server and re-establishes its subscriptions.

## 7. Subscriptions — the one held-open thing, made explicit

`subscriptions/listen` replaces both the old GET notification stream and
`resources/subscribe`: an opt-in, long-lived stream with explicit filters (tools /
prompts / resources listChanged, plus specific resource URIs), correlation by
subscription id on stdio, and mandatory re-subscription after reconnect. This is how a
client stays *fresh*: change notifications from this stream are the authoritative cache
invalidation signal.

## 8. Caching — the server's word, not the client's clock

List, read, and discover results carry `ttlMs` and `cacheScope`. A caching client keys
by method+params, binds `private`-scoped entries to the authorization context, never
caches MRTR-retry results, treats TTL as a hint (with jitter, never tight polling), and
invalidates on notifications. A cache that expires only on its own local clock is
non-conformant in spirit: staleness is governed by the server's declared semantics plus
its change signals.

## 9. Authorization — OAuth 2.1, hardened and issuer-bound

On 401, discover the protected-resource metadata (WWW-Authenticate, then well-known
paths in order); discover the authorization server via BOTH RFC 8414 and OIDC discovery
in specified priority; PKCE S256 is mandatory and the client must *refuse to proceed* if
the AS doesn't advertise it; every authorization and token request carries the RFC 8707
`resource` indicator; the `iss` on the redirect is validated by exact comparison before
the code is redeemed; stored credentials are bound per-issuer and never reused across
authorization servers. Dynamic client registration is deprecated in favor of **CIMD**:
the client's `client_id` IS an https URL at which the client hosts its own metadata
document — which makes "being a well-behaved MCP client" partly a *deployment* concern
(you must host that document somewhere stable).

## 10. What a host owes its human

Several consent behaviors are now spec obligations, not courtesies: keep the human in
the loop on tool invocation with a real deny; make unmistakable WHICH server is asking
when elicitation fires; URL-mode elicitation must never pre-fetch, must show the full
URL, and must open it in a surface the client/LLM cannot inspect; tool annotations are
untrusted unless the server is; icons render only under strict source rules. The model
is explicit: the server is a counterparty, the host is the user's fiduciary.

## 11. The error model — refusals that teach

The reserved codes are self-describing so a client can heal instead of guessing:
`-32020` header mismatch (re-list and retry), `-32021` missing required client
capability (the payload *names* the required capabilities — re-dial with them),
`-32022` unsupported version (the payload names supported versions). Resource-not-found
moved to `-32602` with mandatory acceptance of the legacy code. The philosophy: a typed
refusal is an answer, and answers are actionable — retrying an unchanged request against
a deterministic refusal is a client bug.

## 12. Summary in one paragraph

MCP v2 turns "a conversation between two stateful peers" into "self-describing requests
against stateless capability surfaces." All long-lived state is explicit (tasks,
subscriptions, requestState); all human interaction is pulled client-side and delivered
by retrying (MRTR); all capability is negotiated generically (extensions framework); all
freshness is server-governed (cache hints + change notifications); all refusals are
typed and self-describing. A conformant client therefore has one shape: negotiate
everything, assume nothing, retry-don't-resume, honor the server's declared semantics,
and put the human — with real consent surfaces — at every point where the protocol
reaches out of the machine.
