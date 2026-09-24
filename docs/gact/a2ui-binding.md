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
  the narrower set ONE session may actually `createSurface` against —
  exactly the catalogs the session's agent declares, in its declared order.
  An installed-but-undeclared catalog (a builtin the agent did not list, or
  an inactive pack's catalog) still resolves for validation (replay of an
  old surface never breaks) but is not producible in that session.

### The per-agent allowlist (v15 S8)

An agent's `a2ui_catalogs` is the **complete** allowlist of catalogs it may
produce against. Nothing is implicit: the builtins are producible only for
an agent that lists them, exactly like a pack's own catalog.

```yaml
a2ui_catalogs:
  - earthscope-stations: catalogs/earthscope-stations   # pack-local: name: relative/dir
  - clio-workspace                                      # builtin, by name (or basic)
```

- **Order is preference.** The written order is the agent's preference
  order: `supportedCatalogIds`, the session catalog route's producible rows,
  the catalog skill index, and `select_catalog` all follow it. (Before S8
  these lists were sorted alphabetically by `catalogId`, which made Basic
  the default for a surface that named no catalog.)
- **No declaration, no A2UI.** An agent that declares nothing has no
  producible catalogs. A root agent then gets no producer tools
  (`agents/auto_tools.py`), no catalog skill is disclosed, and the typed
  reason `a2ui_no_catalogs_declared` is recorded on the session ledger and
  the trace. An agent whose declarations resolve to nothing records
  `a2ui_no_catalogs_resolved`. A child's producer tools still come only from
  an explicit `tools:` declaration; a child that declares them in such a
  session gets a typed refusal with the same reason.
- **Basic stays installed.** Basic is still in the registry and still
  renders, but only an agent that lists `basic` can produce against it.
- **Validation.** An unknown builtin name, a malformed entry, a pack
  directory that is missing or does not load, or a conflicting declaration
  is a validation error at blueprint validate, install, and activation
  (`a2ui_catalogs/blueprint.py::validate_blueprint_catalogs`, which runs the
  same resolution the runtime uses). The legacy mapping form
  (`name: relative/dir` entries under a mapping) is still read, as pack-local
  catalogs only, in written order.

**One resolution.** Every consumer that decides producibility or disclosure
derives from `a2ui_catalogs.activation.resolve_session_catalogs`: session
producibility, the agent capability advertisement, `select_catalog`, the
catalog skill index (`skills.SkillCatalog._catalog_refs`), producer-tool
attachment, the session catalog and capability routes, and the session
catalog resolver the action dispatcher resolves a surface's catalog through.

**Forward shape: resolution over declaration sources.** Agent-plugins 1.0
will replace blueprints, and an agent will be a concatenation of plugins,
each declaring its own catalogs. So resolution is written as an ordered
union over declaration sources, not over "the blueprint":
`a2ui_catalogs/declarations.py::resolve_agent_catalogs(sources) ->
ResolvedCatalogs`. Today there is exactly one source (the active
blueprint's `a2ui_catalogs`, `session_declaration_sources`); each plugin
becomes one more `CatalogDeclarationSource` and no consumer changes. Each
`CatalogDeclaration` is self-contained: its name plus its origin (a
builtin, or a directory already resolved against its own declaring unit's
root). The merge keeps source order, then declaration order; the same name
with the same origin is deduplicated; the same name with a different origin,
or two names resolving to one `catalogId`, is the typed
`a2ui_catalog_declaration_conflict` and the later declaration is refused —
never a silent override.

`GET /v1/sessions/{sid}/a2ui/capabilities`'s `agent` field and every
`a2ui_capabilities` row on `GET /v1/agents` / `GET /v1/agents/{id}` use the
PRODUCIBLE scoping (a session's own set, or — for an agent-listing row — that
row's own blueprint's declared set), never the full installed catalog.

**S4: producers learn catalogs through skills, not this document's shapes.**
This document is the wire/negotiation contract; it is never where a model
learns a component's properties. Each of a session's producible catalogs is
disclosed as a generated skill id `a2ui-catalog-<slug>`
(`gact/a2ui_catalogs/skills.py`), auto-declared onto any expert that declares
a producer tool or is a root agent (`agents/skill_runtime.py`), in the
agent's declared order. An agent with no declared catalogs gets no catalog
skill lines at all. Its body is
the catalog's own `instructions.md` plus a generated component/function/event
index; the exact schema for one component comes from
`load_skill("a2ui-catalog-<slug>", file="catalog.json#/components/<Name>")`
— a JSON-pointer fragment read of the SAME `catalog.json` this document's
`select_catalog` and the S2 validator resolve against, never a second
maintained copy. A component-schema validation failure a producer tool
catches carries this exact `load_skill(...)` call as its `hint` (see
`gact/a2ui_producer/_refusal.py`).

## Pack-borne catalogs: a worked example

S7 (docs/design/a2ui-compat-campaign-2026-09.md, iowarp/clio-agent-marketplace#69)
is the campaign's composability proof: a marketplace pack ships its own
catalog and this binding accepts it with **no clio-agent source edit** — a
pack author needs nothing beyond the files below and the declaration in
`AGENT.md`.

`external/clio-agent-marketplace/earthscope-single-agent/` ships
`catalogs/earthscope-stations/{catalog.json, catalog.clio.json,
instructions.md}` — `StationMap`/`StationPicker` aliasing the renderer's
`clio.map.v1`/`ChoicePicker` kernels (the latter preset to
`variant: "multipleSelection"`), plus the unmodified Basic `Text`/`Column`/
`Row`/`Button`, and one declared event: `earthscope.stations.selected`
(`destination: "agent"`, a `context_schema` requiring `searchId` and a
non-empty `stationIds` array). Its `AGENT.md` frontmatter declares it exactly
like an MCP server:

```yaml
a2ui_catalogs:
  - earthscope-stations: catalogs/earthscope-stations
  - clio-workspace
```

(its own catalog first, then the builtin workspace catalog; since v15 S8 the
list is the agent's complete allowlist), and `experts/main.md` lists the same
names under its own `a2ui_catalogs:` so the root expert gets the generated
skills `a2ui-catalog-earthscope-stations` and `a2ui-catalog-clio-workspace`. Installing the pack (`install_agent_blueprint`,
the same path an MCP server's declaration takes) registers the catalog under
`(catalogId, "0.9.1")`; activating the blueprint in a session makes it
**producible** there (see "Producible vs. installed" above) — nothing else
changes: the same `select_catalog`, the same `create_a2ui_surface` producer
tool, the same `POST /v1/sessions/{sid}/a2ui/actions` dispatcher route this
document already describes.

`tests/test_gact/test_a2ui_pack_composability.py` is the CI-run proof: it
installs this real pack into an isolated user config dir, activates it,
and — against an otherwise unmodified server — asserts the catalog is
producible with its file/sidecar/instructions, is listed in both
`GET /v1/capabilities` and a client-preference-first `select_catalog`, that
`create_a2ui_surface` renders the pack's own worked example (parsed straight
out of `instructions.md`, so the test cannot drift from the docs), and that
`earthscope.stations.selected` round-trips through the idle, duplicate, and
waiting-user delivery paths. `tests/test_real_cases/test_earthscope_interactive.py`
carries the live-provider twin of the same three scenes.

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
3. With no `preferred` given, walk this session's PRODUCIBLE catalogs **in
   the agent's declared preference order** (v15 S8) and return the first one
   the client advertises in `supportedCatalogIds`. A surface that names no
   catalog therefore gets the agent's first declared catalog the client can
   render, never Basic by accident.
4. Zero intersection -> typed `a2ui_catalog_no_client_match`.

Before any of this, an agent with no producible catalogs returns
`a2ui_no_catalogs_declared` (nothing declared) or `a2ui_no_catalogs_resolved`
(declarations resolved to nothing): no client advertisement could change the
outcome.

`select_catalog` always **returns** a `CatalogSelection` (never raises); a
producer tool (S4) is the one that turns an unsuccessful selection into a
tool refusal. The selected catalog is **locked per surface** at
`createSurface` time — that locking is the S4 producer tool's concern, not
this module's.

### S4: how the producer tools actually call this

`create_a2ui_surface(..., catalog_id="")` (`gact/a2ui_producer/create.py`) is
the one caller of `select_catalog`:

- The surface already exists -> the EXISTING surface's own locked
  `catalog_id` is reused; no renegotiation of an established surface, and
  the `catalog_id` argument (if any) is ignored.
- The surface is new -> ALWAYS `select_catalog(app, session_id,
  preferred=catalog_id or None)`, whether `catalog_id` was left empty or
  given explicitly. An explicit id is a preference, not an assertion that
  bypasses the client-preference gate: "preferred wins only when it is
  itself in both the client-supported and the producible set" applies to
  every caller uniformly. A non-selection becomes the typed refusal
  `{"ok": false, "reason": <CatalogSelection.reason>, "detail": ...}` — the
  model reads the SAME reason codes this document defines, never a bare
  exception, and a caller-preferred-but-unselectable id refuses with
  `a2ui_preferred_catalog_not_selectable` rather than silently substituting
  a different catalog or producing against one the client never advertised.

`update_a2ui_components`, `update_a2ui_data_model`, and
`delete_a2ui_surface` never take a `catalog_id` argument at all — they
always resolve it from the addressed surface's own record.

## Routes

- `GET /v1/capabilities` — gains `capabilities.a2ui_capabilities`, the
  server-wide official agent-capabilities object (installed scope).
- `GET /v1/sessions/{sid}/a2ui/capabilities` — `{"agent": <producible-scoped
  agent capabilities>, "client": <last remembered client capabilities, or
  null>, "selection": <CatalogSelection, or the typed no-advertisement
  reason>}`.
- `GET /v1/a2ui/catalogs` / `GET /v1/sessions/{sid}/a2ui/catalogs` (S2) —
  the client's registry source: every installed catalog, with the session
  route adding the producibility verdict per row. The session route lists
  the producible rows first, in the agent's declared order (v15 S8), then
  every other installed catalog; a client that advertises its rows in order
  therefore advertises the agent's preference.
- `GET /v1/agents`, `GET /v1/agents/{id}` — each row's `metadata` gains
  `a2ui_capabilities`: that row's OWN declaring blueprint's resolved
  catalogs, in declared order (not the caller session's active blueprint — a listing enumerates
  every agent, most of which are not the session's current one). Resolution
  mirrors S2's own session activation (path-activated blueprints included,
  not only installed ones); an unresolved `agent_blueprint_id` records the
  typed `a2ui_blueprint_unresolved` reason and reports no catalogs. A row
  with no blueprint declares nothing and reports none.
- `GET /v1/agent-blueprints/{id}` — the detail response gains top-level
  `a2ui_capabilities`: that ONE blueprint's resolved catalog ids, in
  declared order.

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
| `a2ui_blueprint_unresolved` | (S2, reused) a row's `agent_blueprint_id` did not resolve via path-activation or the installed registry -- its `a2ui_capabilities` is empty |
| `a2ui_no_catalogs_declared` | (v15 S8) the session's agent declares no `a2ui_catalogs` -- no producer tools, no catalog skills, selection refuses |
| `a2ui_no_catalogs_resolved` | (v15 S8) the agent declares `a2ui_catalogs` but none resolved to a loadable catalog |
| `a2ui_catalog_builtin_unknown` | (v15 S8) an entry names a builtin this server does not ship |
| `a2ui_catalog_declaration_invalid` | (v15 S8) an entry is malformed, or its catalog directory does not load |
| `a2ui_catalog_declaration_conflict` | (v15 S8) the same name (or `catalogId`) declared with a different origin -- the later declaration is refused |
| `a2ui_data_model_foreign_surface` | (S5) `a2uiClientDataModel` named a surfaceId this session never created -- that entry is dropped, the rest of the request still proceeds |
| `a2ui_action_duplicate` | (S5) an action resubmitted the same idempotency key -- the existing record is returned, nothing re-delivered |
| `a2ui_waiting_user_uncorrelated` | (S5) the session is `waiting_user` but no pending question correlates to the action's surface or `context.question_id` -- refused 409 |
| `a2ui_permission_out_of_scope` | (S5) the action named a `permission_id` outside this session's own scope |
| `a2ui_repair_exhausted` | (S5) a second `VALIDATION_FAILED` for the same surface revision arrived after one repair delivery -- the surface folds to `state="failed"` |
| `a2ui_client_error_unhandled` | (S5) a client error report used a code other than `VALIDATION_FAILED` -- persisted, never delivered |

## S5: action lifecycle, delivery matrix, idempotency, and the error round-trip

Slice: S5, [#1372](https://github.com/iowarp/clio-agent/issues/1372). Owner
package: `src/clio_agent/gact/a2ui_actions/` (`record.py`, `dispatcher.py`,
`delivery.py`, `narration.py`, `client_state.py`). `routes/a2ui.py` keeps
only the routes: header negotiation, body parsing, and a thin
`dispatch_action_negotiated` wrapper that calls the owner
`dispatch_action(app, sid, client_message, correlation, metadata)`.

**The durable record.** Every client action (or error report) becomes an
`a2ui_action` transcript Part -- a sibling of the `a2ui` surface part, on the
SAME ledger, folded by `A2UIStore` into `A2UISurfaceRecord.actions[]`
(`gact/a2ui_actions/record.py::fold_action_records`, wired into
`A2UIStore._project`). A record's lifecycle transition (`received ->
delivered -> consumed`, or `received -> failed`) is append-only: each
transition mints a NEW part sharing the record's `id`; the fold keeps only
the latest snapshot per id, in causal (`recorded_at`) order -- exactly the
convention `project_a2ui_parts` already uses for `a2ui` parts.

**Idempotency.** `idempotency_key = sha256(surfaceId, sourceComponentId,
timestamp, canonical JSON of context)`. Before persisting anything, the
dispatcher looks the key up over the surface's own `actions[]`; a match
returns the EXISTING record (200) and publishes `a2ui.action.duplicate` --
nothing is re-persisted, nothing is re-delivered. A resubmission of the
identical envelope (a double-click, a retried POST) is a no-op past the
first; a genuinely distinct click (a fresh `timestamp`) always mints a new
record. No cross-request lock guards the check-then-persist window (matching
the pre-S5 code, which had none either) -- a truly concurrent double
submission is unhandled, tracked as residual debt rather than blocking this
slice.

**Delivery matrix**, keyed by the action's sidecar-declared `destination`
(never its name):

| Destination | Session state | Delivery | Owner |
| --- | --- | --- | --- |
| `agent` (default) | idle | `start` | `_start_background_user_turn` (a fresh turn; metadata carries `a2ui_action`/`surface_id`/`a2ui_action_context`) |
| `agent` | running | `steer` | `loop_inbox.enqueue_user_steer` |
| `agent` | `waiting_user`, correlated | `resolve_question` | `app.state.answer_user_question` (the pending question tagged `metadata["a2ui_surface_id"] == surfaceId`, or named by `context.question_id`) |
| `agent` | `waiting_user`, uncorrelated | `rejected` | typed 409 `a2ui_waiting_user_uncorrelated`; the record is durably `failed` |
| `permission` | any | `permission` | `resolve_permission`, gated to the session's own scope (itself + spawned descendants) |
| `run`, `operation: "cancel"` | any | `run_cancel` | `cancel_session_state` |
| `run`, `operation: "retry"` | any | `run_retry` | `app.state.retry_turn_action` |

`gact/a2ui_actions/delivery.py::deliver_to_agent` is the ONE function behind
every `agent`-destination row, shared verbatim by an ordinary action AND a
`VALIDATION_FAILED` repair delivery (below) -- a repair is "one more
agent-bound event," never a special case.

**Structured context is the authoritative agent input.** Every `agent`
delivery stamps `metadata["a2ui_action"]` (the record id),
`metadata["surface_id"]`, and `metadata["a2ui_action_context"]` (the
resolved `context` object, verbatim) onto the staged/steered/resumed turn.
`narration.py::narration_for` composes the bounded (<=2 KiB), deterministic
text channel from the SAME context (prefixed by `context.userMessage` when
present) -- no `text`/`prompt` field is ever required.

**Narration (S5b).** The text above is *some* rendering of the context --
until the sidecar says otherwise, it is the S5 fallback: event name + the
canonical context JSON, plus `context.userMessage` when the renderer sends
one. That fallback is prose an agent can misread as a REPORT rather than a
REQUEST (clio-agent#1363's live-gate finding: a resumed turn read `A2UI
event: earthscope.stations.selected` + JSON and never staged the selected
stations). The event's MEANING is the pack author's to declare, the same way
0.9.1's own `context_schema` already declares its SHAPE: clio-schemas 0.3.2
adds `CatalogSidecar.events[<name>].narration`, a template string rendered
against the resolved `context` via `clio_schemas.a2ui.sidecar.
render_narration(route, context) -> str | None` (an unresolved `{placeholder}`
is left literal, never raises; a list/dict value renders as compact JSON, so
`{stationIds}` against `{"stationIds": ["MTA1", "PKRD"]}` reads
`["MTA1","PKRD"]`). When a `context_schema` is ALSO declared, every
`narration` placeholder must name one of its `properties` -- checked at
sidecar-construction time, not at render time, so a typo in a pack's own
`catalog.clio.json` fails loudly at install, not silently at delivery. When
declared, the rendered template IS the narration (bounded to the same <=2 KiB
ceiling, same truncation marker), followed by the canonical context JSON on
an UNBOUNDED second paragraph -- the structured object always reaches a
text-only provider, however long the declared template renders. When no
route declares a narration (no route at all, or one without this field), the
S5 fallback form is unchanged, and the session records the typed reason
`a2ui_event_narration_undeclared` ONCE per event name (not once per action --
`gact/a2ui_catalogs/registry.py::CatalogRegistry.
record_narration_undeclared_once`). This mirrors 1.0's own `userMessage`
action field (the campaign's seam-point item 19): 0.9.1 carries no such field
on the wire, so `narration` is 0.9.1-sidecar-declared, early metadata a pack
author already controls today, not a wire change.

**Consumed.** `gact/a2ui_actions/record.py::mark_a2ui_action_consumed` flips
a `delivered` record to `consumed` (publishing `a2ui.action.consumed`) the
moment the turn/steer that carried it actually starts executing: hooked at
`turn.py::_run_turn_in_background` (a fresh or idle-redriven turn -- both
stage through `_start_background_user_turn`) and
`steer_delivery.py::compose_steer_block` (a mid-turn-drained steer). A no-op
for any metadata that carries no `a2ui_action` key or whose record is not
currently `delivered`.

**The client data-model per-surface filter.** S3's
`apply_client_metadata_guards` proves only that the SESSION carries at least
one live `sendDataModel` surface. `client_state.py::filter_owned_data_model`
narrows further, per surface key: a surfaceId this session never produced is
dropped with `a2ui_data_model_foreign_surface`; one that exists but is
deleted or was not created with `sendDataModel: true` is dropped with
`a2ui_data_model_not_requested`. The rest of the request -- including the
action's own delivery -- still proceeds; only the offending entries are
missing from `result["a2ui_client_data_model"]["surfaces"]`.

**The error round-trip.** `client_state.py::ingest_client_error` handles the
official `{version, error: {...}}` envelope, routed BEFORE any surface
lookup (a renderer reporting its own rejection is accepted even when
`surfaceId` cannot be resolved). `VALIDATION_FAILED {surfaceId, path,
message}`: the FIRST report at a surface's current revision gets exactly one
repair delivery, narrated `"renderer rejected surface <id> at <path>:
<message>; repair with update_a2ui_components(...) or load_skill(...)"`; a
SECOND report at the SAME revision (`prior_error_count_for_revision`) never
delivers -- the record is `failed`/`a2ui_repair_exhausted` and
`fold_action_records` folds the SURFACE itself to `state="failed"` (a
genuine new revision resets the count). Any other error code is persisted
with `reason="a2ui_client_error_unhandled"` and never delivered.

**Events.** `a2ui.action.received|delivered|consumed|failed|duplicate`,
scoped like the pre-S5 `a2ui.action.received`. Payload contract (set during
the S6 client review, gact-tui#407 -- a lenient client schema, unknown keys
ignored, missing optional keys never gap the stream):

```json
{
  "surface_id": "<required>",
  "action_name": "<required>",
  "action": "<same value, kept for the pre-S5 consumer>",
  "source_component_id": "<optional>",
  "action_id": "<record id>",
  "state": "received|delivered|consumed|failed",
  "delivery": "start|steer|resolve_question|permission|run_cancel|run_retry|rejected",
  "reason": "<typed code when failed/rejected/duplicate>"
}
```

**Interactions projection.** `routes/interactions.py::_a2ui_interactions`
derives `status` from the surface's LATEST action record
(`delivered`/`consumed` -> `answered`, otherwise `pending`) instead of the
deleted `/lastAction` data-model write; `payload.last_action` carries that
record. `_surface_last_action` is deleted.

## Desktop parity (S8)

The client-side architecture already states the invariant this section
proves server-side (`external/gact-tui/packages/core/src/v3/a2ui/client-
metadata.ts`): "the transport never sees or builds this metadata, it only
carries whatever the repository layer merges in — identical by
construction." Browser (fetch, against the dev-server origins in
`gact/cors.py`'s `_DEFAULT_ORIGINS`) and Tauri desktop (the packaged
webview's IPC over the SAME local GACT HTTP API) build and send the
identical `a2uiClientCapabilities` / `a2uiClientDataModel` JSON bodies — one
`packages/core` module, one code path, no per-transport branch.

On the server, `Origin` is consulted in exactly ONE place: `gact/cors.py`'s
allowlist, which only governs whether a **browser's** CORS preflight is
granted. It is never read by the metadata door itself
(`a2ui_capabilities.apply_client_metadata_guards`, the ONE guard every
client-writable ingest in "Where the objects ride" above calls) — that
function parses `metadata.a2uiClientCapabilities`/`a2uiClientDataModel` from
the request BODY only.

A real Tauri desktop request never carries `Origin` at all — not "typically
doesn't," structurally can't. `desktop/src-tauri/src/gact_http.rs`'s own
module doc: the WebView's origin (`http://tauri.localhost`) is cross-origin
to the local sidecar and clio emits no `Access-Control-Allow-Origin`, so a
vanilla browser `fetch()` from the WebView would be CORS-blocked; `gact_http`
is a Tauri command that performs the request from Rust with the `ureq`
native HTTP client instead, which — unlike a browser engine — never
auto-attaches an `Origin` header. `web/src/lib/transport/browser-transport.ts`
and `tauri-transport.ts`'s own private `headers()` methods build a
byte-for-byte identical explicit set either way (`Accept`/`Content-Type`/
`X-GACT-Version`/`X-A2UI-Version`/optional `Authorization`) — neither ever
sets `Origin` itself; a browser's `Origin` comes solely from the browser
engine auto-attaching `window.location.origin`. The guard must therefore
never treat a present-vs-absent `Origin` as a signal either way:
`tests/test_gact/test_a2ui_desktop_parity.py` vendors both transports' real
header sets and the real `a2uiClientCapabilities` body shape/catalog ids
under `tests/fixtures/a2ui_client_metadata/` (no raw Tauri network capture
exists in gact-tui to vendor verbatim — the desktop e2e suite drives a real
WebView rather than recording HTTP — so that fixture is derived from the
Rust bridge source above) and proves both are remembered byte-for-byte
identically.

Consequence for a pack/catalog author: nothing in a catalog's sidecar or
instructions may assume "this session is a browser" or "this session is
desktop" — that distinction does not exist past the CORS preflight, and nothing
downstream of `apply_client_metadata_guards` (catalog selection, producer
tools, the action dispatcher) can observe it either.
