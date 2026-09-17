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
| `a2uiClientCapabilities` | `POST /v1/sessions/{sid}/messages/{id}/retry` | top-level `metadata.a2uiClientCapabilities` |
| `a2uiClientCapabilities` | `POST /v1/sessions/{sid}/a2ui/actions` | top-level `metadata.a2uiClientCapabilities` (new optional body field, beside `message`/`correlation`) |
| `a2uiClientCapabilities` | `POST /v1/agent-tasks/{id}/steer` | top-level `metadata.a2uiClientCapabilities`, validated against the **CHILD** session (`task.child_session_id`), not the caller's own session |
| `a2uiClientDataModel` | `POST /v1/sessions/{sid}/messages` | top-level `metadata.a2uiClientDataModel` |
| `a2uiClientDataModel` | `POST /v1/sessions/{sid}/a2ui/actions` | top-level `metadata.a2uiClientDataModel` |
| `a2uiClientDataModel` | `POST /v1/agent-tasks/{id}/steer` | top-level `metadata.a2uiClientDataModel`, checked against the **CHILD**'s own `sendDataModel` surfaces |

`POST /v1/sessions/{sid}/messages/{id}/retry` runs the same guard as `POST
/messages` (malformed capabilities refuse 422; a valid advertisement is
remembered) but does not rename the data-model key onto the retry record —
only the two live message/action doors normalize it (S5 reads it from
there).

`POST /v1/agent-tasks/{id}/steer` is a genuine client door onto the **CHILD**
session, not "parent forwarding" — a client steering a running child directly
gets exactly the same treatment `POST /messages` gives its own session,
scoped to the child. This is distinct from the model-facing `message_agent`
tool, which never carries any client metadata at all (see "Sub-agent
stripping" below).

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

The WHOLE request is validated before anything is remembered — a valid
`a2uiClientCapabilities` alongside a REFUSED `a2uiClientDataModel` leaves
nothing persisted; a refused request has no partial effects:

1. **Parse `a2uiClientCapabilities`.** Malformed shape -> HTTP 422
   `a2ui_client_capabilities_invalid`. `inlineCatalogs` present -> HTTP 422
   `a2ui_inline_catalogs_unsupported`. Absent is not an error.
2. **Parse + check `a2uiClientDataModel`.** Malformed shape -> HTTP 422
   `a2ui_client_data_model_invalid`. Checked against **the target session's
   own** LIVE surfaces — the caller's session on `POST /messages` and
   `.../retry`, the **CHILD** session on `POST /agent-tasks/{id}/steer` — a
   `deleted` surface no longer counts even though its `createSurface`
   message (and its `sendDataModel` flag) is still in the transcript. If no
   LIVE surface requested `sendDataModel: true`, the whole request is
   refused HTTP 422 `a2ui_data_model_not_requested` on **every** door that
   carries a data model (not only `POST /messages`).
3. **Remember, only now.** Only after both checks pass does
   `remember_client_capabilities` persist the capabilities object on
   `Session.metadata["a2ui_client_capabilities"]` through the existing
   `SessionStore.update` path (shallow metadata merge, flush-to-disk on every
   call) — no fifth store (RULE 4). **Last advertisement wins**: a later
   request with a different `supportedCatalogIds` list simply overwrites the
   key. Because `SessionStore.update` flushes synchronously, the
   advertisement **survives a process restart** exactly like `goal`/`loop`
   session state. A stored value that later fails to re-validate (a
   hand-edited/corrupted session row) is never read back as a bare `None` —
   it records the same `a2ui_client_capabilities_invalid` reason, tagged
   `source="stored"`, before falling back to "no advertisement."
4. **Carry the data model through, renamed.** On success, the accepted
   record replaces the wire key `a2uiClientDataModel` with the internal,
   snake_case `a2ui_client_data_model` (the same validated value) on `POST
   /messages` and the action route's staged `agent.submit` record, and on
   `POST /agent-tasks/{id}/steer`'s queued steer metadata — so a consumer
   downstream (S5 owns ingestion/fold semantics) reads one consistent
   internal key regardless of which door it arrived through. `POST
   .../retry` remembers capabilities but does not perform this rename.
   `a2uiClientCapabilities` is renamed onto `a2ui_client_capabilities` the
   same way on the message and steer doors' accepted records.

Every refusal above records a typed reason through the S2 per-session ledger
(`CatalogRegistry.record_session_reason` / `.session_reasons(session_id)`,
`gact/a2ui_catalogs/reasons.py`) **before** the HTTP exception is raised —
queryable after the fact, never a bare exception message. The ONE exception:
a pure READ of the negotiation state (`GET /v1/sessions/{sid}/a2ui/capabilities`)
computes `select_catalog(..., record=False)` — looking never writes to the
ledger.

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

**S4: producers learn catalogs through skills, not this document's shapes.**
This document is the wire/negotiation contract; it is never where a model
learns a component's properties. Each of a session's producible catalogs is
disclosed as a generated skill id `a2ui-catalog-<slug>`
(`gact/a2ui_catalogs/skills.py`), auto-declared onto any expert that declares
a producer tool or is a root agent (`agents/skill_runtime.py`). Its body is
the catalog's own `instructions.md` plus a generated component/function/event
index; the exact schema for one component comes from
`load_skill("a2ui-catalog-<slug>", file="catalog.json#/components/<Name>")`
— a JSON-pointer fragment read of the SAME `catalog.json` this document's
`select_catalog` and the S2 validator resolve against, never a second
maintained copy. A component-schema validation failure a producer tool
catches carries this exact `load_skill(...)` call as its `hint` (see
`gact/a2ui_producer/_refusal.py`).

## Catalog selection

"The agent selects the best match from the client's `supportedCatalogIds`
list" — `gact/a2ui_capabilities.py::select_catalog(app, session_id, preferred=None)`:

1. No client advertisement remembered yet for this session -> typed
   `a2ui_client_capabilities_unknown`. Selection is never silently defaulted
   to a catalog the client never mentioned.
2. If `preferred` (an explicit tool argument) is given, it wins ONLY when it
   is itself in *both* the client-supported set and the producible set.
   Otherwise selection stops there with a typed
   `a2ui_preferred_catalog_not_selectable` — it NEVER falls through to the
   general preference-order pick below and substitutes a different catalog
   the caller did not ask for. The reason carries the preferred id and the
   client-supported ∩ producible intersection, so a caller can tell "asked
   for X, got nothing" from "asked for X, silently got Y."
3. With no `preferred` given, walk the client's `supportedCatalogIds` **in
   the client's own preference order** and return the first id that is in
   this session's PRODUCIBLE set.
4. Zero intersection -> typed `a2ui_catalog_no_client_match`.

`select_catalog` always **returns** a `CatalogSelection` (never raises); a
producer tool (S4) is the one that turns an unsuccessful selection into a
tool refusal. The selected catalog is **locked per surface** at
`createSurface` time — that locking is the S4 producer tool's concern, not
this module's.

### S4: how the producer tools actually call this

`create_a2ui_surface(..., catalog_id="")` (`gact/a2ui_producer/create.py`) is
the one caller of `select_catalog`, and only in the two cases where a catalog
still needs deciding:

- `catalog_id` empty AND the surface is new -> `select_catalog(app, session_id)`
  (no `preferred`); a non-selection becomes the typed refusal
  `{"ok": false, "reason": <CatalogSelection.reason>, "detail": ...}` —
  the model reads the SAME reason codes this document defines, never a
  bare exception.
- `catalog_id` empty AND the surface already exists -> the EXISTING surface's
  own locked `catalog_id` is reused (no renegotiation of an established
  surface).
- `catalog_id` given explicitly (non-empty) -> used AS GIVEN, never routed
  through `select_catalog`'s client-preference gate. It still crosses every
  other boundary unchanged: `validate_server_message` refuses an unknown or
  non-producible id (`a2ui_catalog_unknown` / `a2ui_catalog_not_producible`)
  exactly as it always has. This is a deliberate reading of "preferred wins
  only when in both sets": an EXPLICIT tool argument is the caller's own
  assertion (a blueprint/skill that already knows which catalog it wants),
  distinct from `select_catalog`'s `preferred=` parameter (which exists for
  a caller that wants negotiation with a fallback); the two are not required
  to be the same code path, and collapsing them would additionally require
  every producer-tool caller that already knows its catalog id to first
  have a remembered client advertisement, which is not otherwise a
  precondition of producing a surface. `update_a2ui_components`,
  `update_a2ui_data_model`, and `delete_a2ui_surface` never take a
  `catalog_id` argument at all — they always resolve it from the addressed
  surface's own record.

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
  every agent, most of which are not the session's current one). Resolution
  mirrors S2's own session activation (path-activated blueprints included,
  not only installed ones); an unresolved `agent_blueprint_id` records the
  typed `a2ui_blueprint_unresolved` reason rather than silently falling back
  to builtins with no signal.
- `GET /v1/agent-blueprints/{id}` — the detail response gains top-level
  `a2ui_capabilities`: that ONE blueprint's declared catalog ids ∪ the
  builtins.

## Sub-agent stripping

The protocol is explicit: `a2uiClientDataModel` is "sent exclusively to the
server that created the surface," and orchestrators "MUST strip it before
sub-agents." `strip_renderer_metadata` removes both raw wire keys
(`a2uiClientCapabilities`, `a2uiClientDataModel`) and BOTH accepted-record
renamed keys (`a2ui_client_capabilities`, `a2ui_client_data_model`) from any
metadata mapping about to ride onto a spawned child/expert turn.

**The strip belongs at the FORWARDING boundary only — never at a genuine
client door.** A client steering a running child directly
(`POST /v1/agent-tasks/{id}/steer`) is not "parent forwarding": it is a
fresh client-to-child message, so it gets the SAME door guard `POST
/messages` gives its own session (see above), scoped to the child, and its
validated/renamed metadata rides onto the child's inbox UNSTRIPPED — a
client's own advertisement is never silently thrown away without a typed
reason.

- `gact/turn_spawn.py::_launch` — a child's FIRST staged user message. This
  dict is a fixed literal (`{"agent_task_id": ..., "spawned_by": ...}`),
  never a copy of the parent's message metadata, so it structurally cannot
  carry the renderer keys — no strip call is needed here; the invariant is
  proven by
  `tests/test_gact/test_a2ui_capabilities.py::test_spawn_child_turn_never_forwards_renderer_metadata`.
- `gact/agent_message_transport.py::message_in_process` is a NEUTRAL
  transport shared by two callers with different semantics, so the decision
  lives with the caller, not the transport:
  - `routes/agent_tasks.py::steer_task` (the client door above) validates
    against the CHILD, renames, and passes the result through unstripped.
  - `agents/agent_messaging.py::build_message_agent_tool`'s `message_agent`
    (the true parent->child FORWARDING path) never supplies a `metadata`
    argument at all today, so there is nothing to strip on that path —
    if it ever gains one, THAT call site is where `strip_renderer_metadata`
    belongs, mirroring `_launch`.

`gact/turn_forward.py`, `gact/delegation.py`, `gact/delegation_return.py`,
`gact/child_forward.py`, and `gact/agents/spawn_runtime*.py` were audited and
carry no other site that forwards a parent turn's *message* metadata
wholesale into a child: `spawn_context.inherited_session_scope_metadata`
copies only an allowlisted SESSION-level prefix set
(`active_agent_blueprint_*`, `active_expert_pack_*`, `expert_pack_id`) that
never matches the renderer keys, by construction.

## Typed reasons (S2 ledger, reused)

No new store — every S3 degradation lands in the same per-session ledger S2
built (`CatalogRegistry.record_session_reason` /
`gact/a2ui_catalogs/reasons.py`), bounded to the SAME 256-row ring the global
ledger uses (`A2UI_CATALOG_REASON_RING_MAXLEN`) so a long-lived session's
per-session history cannot grow unbounded (bounded memory is release-gating):

| Reason | When |
| --- | --- |
| `a2ui_client_capabilities_invalid` | malformed `a2uiClientCapabilities` (`detail.source="stored"` when it was a previously-remembered value that failed re-validation, e.g. a hand-edited session row, rather than a live parse) |
| `a2ui_inline_catalogs_unsupported` | client sent `inlineCatalogs` |
| `a2ui_client_data_model_invalid` | malformed `a2uiClientDataModel` |
| `a2ui_data_model_not_requested` | data model sent, no LIVE `sendDataModel` surface on the target session (the caller's own session, or the CHILD on the steer door) |
| `a2ui_client_capabilities_unknown` | selection attempted, no advertisement yet |
| `a2ui_preferred_catalog_not_selectable` | `preferred` not in client-supported ∩ producible |
| `a2ui_catalog_no_client_match` | selection attempted, zero intersection |
| `a2ui_blueprint_unresolved` | (S2, reused) a row's `agent_blueprint_id` did not resolve via path-activation or the installed registry -- its `a2ui_capabilities` falls back to builtins-only |
