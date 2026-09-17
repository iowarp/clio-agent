# The GACT binding for A2UI 0.9 capability negotiation

> Slice: S3, `docs/design/a2ui-compat-campaign-2026-09.md`. Umbrella #1363,
> issue #1369. Owner module: `src/clio_agent/gact/a2ui_capabilities.py`.

The A2UI protocol defines capability negotiation and the client data model as
**transport metadata** — it hands the objects' shapes to a transport binding
and says nothing about where they ride on the wire. A2A puts them in message
metadata. GACT is CLIO's transport, and this document is GACT's binding:
where the objects ride, what the server remembers, and how a catalog gets
selected. It is the analog of an A2A extension document.

## The official objects (verbatim, not reinvented)

Three objects, all namespaced under a literal `"v0.9"` key (the protocol's
*capability generation*, distinct from the per-message envelope `version`
field, which is `"v0.9"` or `"v0.9.1"`):

```jsonc
// Client -> agent, metadata key "a2uiClientCapabilities"
{
  "v0.9": {
    "supportedCatalogIds": ["clio-workspace/v1", "a2ui.dev/basic/v0.9"], // preference-ordered
    "inlineCatalogs": [ /* Catalog objects; only sent when the agent accepts them */ ]
  }
}

// Agent -> client (GET /v1/capabilities, GET /v1/sessions/{sid}/a2ui/capabilities)
{
  "v0.9": {
    "supportedCatalogIds": ["a2ui.dev/basic/v0.9", "clio-workspace/v1", "..."],
    "acceptsInlineCatalogs": false
  }
}

// Client -> the CREATING server ONLY, metadata key "a2uiClientDataModel"
{
  "version": "v0.9" | "v0.9.1",
  "surfaces": { "<surfaceId>": { /* that surface's current data model */ } }
}
```

CLIO validates these with the vendored pydantic models
(`clio_schemas.a2ui.v0_9_1.capabilities.A2UIClientCapabilities` /
`A2UIAgentCapabilities`, `clio_schemas.a2ui.v0_9_1.data_model.A2UIClientDataModel`)
— never a hand-rolled shape. CLIO always advertises
`acceptsInlineCatalogs: false`, so a client sending `inlineCatalogs` is a
protocol violation on the client's part (the spec: "supported but not
recommended in production"; a client should only send them when the agent
accepts them) and is refused, not silently dropped.

## Where the objects ride

| Object | Door | Field |
| --- | --- | --- |
| `a2uiClientCapabilities` | `POST /v1/sessions/{sid}/messages` | top-level `metadata.a2uiClientCapabilities` |
| `a2uiClientCapabilities` | `POST /v1/sessions/{sid}/a2ui/actions` | top-level `metadata.a2uiClientCapabilities` (new optional body field, beside `message`/`correlation`) |
| `a2uiClientDataModel` | `POST /v1/sessions/{sid}/messages` | top-level `metadata.a2uiClientDataModel` |
| `a2uiClientDataModel` | `POST /v1/sessions/{sid}/a2ui/actions` | top-level `metadata.a2uiClientDataModel` |

Both keys are documented, **client-writable** metadata — unlike the internal
turn-control keys in `gact/messaging.py::RESERVED_CLIENT_METADATA_KEYS`
(`hook_defer_resume`, `retry_attempt_id`, ...), a client is expected to send
these two, so they are validated and normalized rather than rejected
outright as an unknown/forbidden key.

`X-A2UI-Version: 0.9.1` stays exactly what it was before this slice: a GACT
pre-flight negotiation header on the `/a2ui/*` doors, checked before any body
is even parsed. It is **not** how a message's protocol version is decided —
the per-message envelope `version` field (`"v0.9"` or `"v0.9.1"`, accepted
and persisted verbatim, never rewritten) is authoritative for that. The
header and the envelope answer two different questions: "will this door talk
to you at all" vs. "what does this specific message target."

## What the server does with them

`gact/a2ui_capabilities.py::apply_client_metadata_guards` is the ONE guard
both doors call, right next to `raise_on_reserved_metadata` on
`POST /messages` (`gact/message_submission.py`) and right after the
unknown-top-level-field check on the action route (`gact/routes/a2ui.py`):

1. **Parse + validate `a2uiClientCapabilities`.** Malformed shape -> HTTP 422
   `a2ui_client_capabilities_invalid`. `inlineCatalogs` present -> HTTP 422
   `a2ui_inline_catalogs_unsupported`. Absent is not an error — it just means
   nothing is remembered this request.
2. **Remember it.** On success, `remember_client_capabilities` persists it on
   `Session.metadata["a2ui_client_capabilities"]` through the existing
   `SessionStore.update` path (shallow metadata merge, flush-to-disk on every
   call) — no fifth store (RULE 4). **Last advertisement wins**: a later
   request with a different `supportedCatalogIds` list simply overwrites the
   key. Because `SessionStore.update` flushes synchronously, the
   advertisement **survives a process restart** exactly like `goal`/`loop`
   session state.
3. **Parse + validate `a2uiClientDataModel`.** Malformed shape -> HTTP 422
   `a2ui_client_data_model_invalid`. On `POST /messages` specifically: if no
   live surface in the session was created with `sendDataModel: true`, the
   whole request is refused HTTP 422 `a2ui_data_model_not_requested` — a data
   model nobody asked for is not silently accepted and thrown away.
4. **Carry it through, renamed.** On success, the accepted message/action
   record replaces the wire key `a2uiClientDataModel` with the internal,
   snake_case `a2ui_client_data_model` (the same validated value) — so a
   consumer downstream (S5 owns ingestion/fold semantics) reads one
   consistent internal key regardless of which door it arrived through.

Every refusal above records a typed reason through the S2 per-session ledger
(`CatalogRegistry.record_session_reason` / `.session_reasons(session_id)`,
`gact/a2ui_catalogs/reasons.py`) **before** the HTTP exception is raised —
queryable after the fact, never a bare exception message.

## Producible vs. installed

Two different questions, two different functions, on purpose (mirrors MCP
server activation):

- **Installed** (`CatalogRegistry.installed()`): every catalog this server
  CAN validate against — the two builtins (Basic, CLIO workspace) plus every
  catalog any discovered Agent Blueprint pack declares, active or not. This
  is what `GET /v1/a2ui/catalogs` and `GET /v1/capabilities`'s
  `a2ui_capabilities` (no session in scope) answer.
- **Producible** (`a2ui_catalogs.activation.session_producible_catalog_ids`):
  the narrower set ONE session may actually `createSurface` against — the
  two builtins plus whatever the session's own active blueprint declares. An
  installed-but-inactive pack's catalog still resolves for validation
  (replay of an old surface never breaks) but is not producible in a session
  that never activated that pack.

`GET /v1/sessions/{sid}/a2ui/capabilities`'s `agent` field and every
`a2ui_capabilities` row on `GET /v1/agents` / `GET /v1/agents/{id}` use the
PRODUCIBLE scoping (a session's own set, or — for an agent-listing row — that
row's own blueprint's declared set), never the full installed catalog.

## Catalog selection

"The agent selects the best match from the client's `supportedCatalogIds`
list" — `gact/a2ui_capabilities.py::select_catalog(app, session_id, preferred=None)`:

1. No client advertisement remembered yet for this session -> typed
   `a2ui_client_capabilities_unknown`. Selection is never silently defaulted
   to a catalog the client never mentioned.
2. Otherwise, walk the client's `supportedCatalogIds` **in the client's own
   preference order** and return the first id that is in this session's
   PRODUCIBLE set. `preferred` (an explicit tool argument) wins only when it
   is itself in *both* the client-supported set and the producible set — it
   never bypasses either.
3. Zero intersection -> typed `a2ui_catalog_no_client_match`.

`select_catalog` always **returns** a `CatalogSelection` (never raises); a
producer tool (S4) is the one that turns an unsuccessful selection into a
tool refusal. The selected catalog is **locked per surface** at
`createSurface` time — that locking is the S4 producer tool's concern, not
this module's.

## Routes

- `GET /v1/capabilities` — gains `capabilities.a2ui_capabilities`, the
  server-wide official agent-capabilities object (installed scope).
- `GET /v1/sessions/{sid}/a2ui/capabilities` — `{"agent": <producible-scoped
  agent capabilities>, "client": <last remembered client capabilities, or
  null>, "selection": <CatalogSelection, or the typed no-advertisement
  reason>}`.
- `GET /v1/a2ui/catalogs` / `GET /v1/sessions/{sid}/a2ui/catalogs` (S2,
  unchanged by this slice) — the client's registry source: every installed
  catalog, with the session route adding the producibility verdict per row.
- `GET /v1/agents`, `GET /v1/agents/{id}` — each row's `metadata` gains
  `a2ui_capabilities`: that row's OWN declaring blueprint's catalogs ∪ the
  builtins (not the caller session's active blueprint — a listing enumerates
  every agent, most of which are not the session's current one).

## Sub-agent stripping

The protocol is explicit: `a2uiClientDataModel` is "sent exclusively to the
server that created the surface," and orchestrators "MUST strip it before
sub-agents." `strip_renderer_metadata` removes both raw wire keys
(`a2uiClientCapabilities`, `a2uiClientDataModel`) and the accepted-message's
renamed key (`a2ui_client_data_model`) from any metadata mapping about to
ride onto a spawned child/expert turn. It is applied at every site that
hands a metadata mapping to a child session:

- `gact/turn_spawn.py::_launch` — a child's FIRST staged user message.
  Structurally this dict is built fresh (never a copy of the parent's
  message metadata), so the strip is the enforced invariant against a future
  change that folds parent metadata in here, proven by
  `tests/test_gact/test_a2ui_capabilities.py::test_spawn_child_turn_never_forwards_renderer_metadata`.
- `gact/agent_message_transport.py::message_in_process` — a mid-run steer
  onto an already-running child's inbox (reached from both the client-facing
  `POST /v1/agent-tasks/{task_id}/steer` route and the model-facing
  `message_agent` tool). This is the one call site where an ARBITRARY
  caller-supplied metadata mapping genuinely could carry the two keys, so
  the strip here is a real removal, not just an invariant —
  proven by `test_message_in_process_strips_renderer_metadata`.

`gact/turn_forward.py`, `gact/delegation.py`, `gact/delegation_return.py`,
`gact/child_forward.py`, and `gact/agents/spawn_runtime*.py` were audited and
carry no other site that forwards a parent turn's *message* metadata
wholesale into a child: `spawn_context.inherited_session_scope_metadata`
copies only an allowlisted SESSION-level prefix set
(`active_agent_blueprint_*`, `active_expert_pack_*`, `expert_pack_id`) that
never matches the two renderer keys, by construction.

## Typed reasons (S2 ledger, reused)

No new store — every S3 degradation lands in the same per-session ledger S2
built (`CatalogRegistry.record_session_reason` /
`gact/a2ui_catalogs/reasons.py`):

| Reason | When |
| --- | --- |
| `a2ui_client_capabilities_invalid` | malformed `a2uiClientCapabilities` |
| `a2ui_inline_catalogs_unsupported` | client sent `inlineCatalogs` |
| `a2ui_client_data_model_invalid` | malformed `a2uiClientDataModel` |
| `a2ui_data_model_not_requested` | data model sent, no `sendDataModel` surface |
| `a2ui_client_capabilities_unknown` | selection attempted, no advertisement yet |
| `a2ui_catalog_no_client_match` | selection attempted, zero intersection |
