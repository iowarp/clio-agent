# CLIO Hooks

The hook system lets an operator run their own code at governance boundaries of a
CLIO turn — before a tool runs, before a model request leaves, at the prompt/stop
boundaries, and on every observed event. It is **one** declarative dispatcher over a
small set of transport adapters. Hooks may only **tighten** (a hook's `allow` never
lifts a caller's `deny`), a hook infrastructure failure is distinct from a user/hook
rejection, and reads are never gated.

This document is the wire contract, the config schema, the trust model, the event
set, and the audit surface.

---

## Configuration

Hooks are a **flat JSON array**, each entry keyed by a **required stable `id`**
(never positional — the identity is the id). The config is discovered, in ascending
precedence, from three scopes:

| scope     | location                              | wins on id-collision |
| --------- | ------------------------------------- | -------------------- |
| `user`    | `<user_config_dir>/hooks.json`        | lowest               |
| `project` | `<cwd>/.clio/hooks.json`              | middle               |
| `managed` | `hooks.managed_config` (admin, opt-in)| highest              |

`CLIO_HOOKS_CONFIG` (or `hooks.config`) points at a single explicit file instead of
user+project discovery. A malformed entry is dropped with a diagnostic naming its
`id`; the rest of the file still loads.

### Config knobs (file → env → default)

| config key                 | env                             | meaning                                                        |
| -------------------------- | ------------------------------- | -------------------------------------------------------------- |
| `hooks.config`             | `CLIO_HOOKS_CONFIG`             | single explicit config file (overrides user+project discovery) |
| `hooks.managed_config`     | `CLIO_HOOKS_MANAGED_CONFIG`     | the admin/managed hook file (highest precedence)               |
| `hooks.allow_managed_only` | `CLIO_HOOKS_ALLOW_MANAGED_ONLY` | `allowManagedHooksOnly` lockdown — drop every non-managed source |
| `hooks.trust_store`        | `CLIO_HOOKS_TRUST_STORE`        | override the trusted-fingerprint store path                    |
| `hooks.defer_timeout`      | `CLIO_HOOKS_DEFER_TIMEOUT`      | bounded park for a PreToolUse `defer` (default 24h)            |

### Entry schema

```jsonc
{
  "hooks": [
    {
      "id": "secret-scanner",           // REQUIRED stable id
      "on": ["PreToolUse"],             // one or more event names (below)
      "match": {                         // optional; absent ⇒ matches everything
        "tool": "hdf5_write",           // ANCHORED regex on tool name (^…$)
        "annotations": {"destructive": true},  // capability match on wire annotations
        "argsPattern": "secret|token"   // (unanchored) regex on JSON tool_input
      },
      "run": {                           // the transport adapter + params
        "type": "command",              // "command" | "http" | "prompt"
        "command": "/usr/bin/python3",
        "args": ["/opt/hooks/scan.py"]
      },
      "timeoutMs": 30000,               // per-hook timeout (subprocess-enforced)
      "failClosed": false,              // deny-capable event ⇒ infra failure denies
      "enabled": true,
      "loopLimit": 0                     // Stop hooks only: per-hook re-drive cap
    }
  ]
}
```

The `tool` match is **anchored** (`^…$`) so `Edit` does not match `NotebookEdit`.
`annotations` matches the wire `tool_annotations` block (`readOnly`/`destructive`/
`openWorld`), which is fail-safe for MCP tools nobody enumerated (an absent/malformed
annotation reads as the most-restrictive shape).

---

## Events

| event              | deny-capable | notes                                                                 |
| ------------------ | ------------ | --------------------------------------------------------------------- |
| `PreToolUse`       | ✔            | the tool gate; `modify`/`synthesize`/`defer` route the interceptor    |
| `PostToolUse`      | —            | after a result; `deny` is feedback, `updatedToolOutput` rewrites view |
| `PostToolBatch`    | —            | after a turn's tool round resolves (observation)                      |
| `UserPromptSubmit` | ✔            | prompt boundary; `deny` vetoes the turn, `defer` suspends it          |
| `Stop`             | — (bounded)  | completion gate; `deny` re-drives one more turn, bounded by `loopLimit` + a global cap |
| `SessionStart` / `SessionEnd`         | — | session lifecycle observation                          |
| `SubagentStart` / `SubagentStop`      | — | child-turn lifecycle observation                       |
| `PreCompact`       | —            | before a transcript is compacted (observation)                        |
| `SemanticEvent`    | —            | fires on every emitted semantic event (observability)                 |
| `BeforeModel`      | ✔            | per LM request; `synthesize`/`model_override`/`request_patch`/`deny`  |
| `AfterModel`       | —            | per LM response; may rewrite `llm_response` before it enters context  |

---

## The wire contract (exit-0 / exit-2 subprocess adapter)

The `command` adapter is the industry **exit-0/exit-2** wire, run with **no
controlling terminal** (no TTY): the JSON **envelope** is written to the hook's
stdin, stdout/stderr are captured.

* **exit 0** → parse stdout as the tagged-union decision object (empty stdout ⇒
  `allow`). A shell-profile banner before the JSON is tolerated (the first decodable
  `{…}` object is used). Exit 0 with non-JSON stdout ⇒ `allow`, recorded with the
  typed reason `hook_unparseable_stdout` (never a silent fail-open).
* **exit 2** → `deny`; stderr becomes the model-facing reason.
* **any other exit / timeout / missing binary** → a **hook infrastructure failure**
  (`HookInfraError`), which is deliberately DISTINCT from a `deny`. For a deny-capable
  event a `failClosed` hook denies with a message that says it is a hook failure (not
  a user rejection); otherwise it is non-blocking. Every such path records a typed
  reason (`hook_timeout` / `hook_crashed` / `hook_missing_binary`).

### Envelope (stdin)

```jsonc
{
  "schema_version": 1,
  "hook_id": "secret-scanner",
  "hook_event_name": "PreToolUse",
  "session_id": "…", "turn_id": "…", "cwd": "…",
  "tool_name": "hdf5_write",
  "tool_input": { … },
  "tool_annotations": {"readOnly": false, "destructive": true, "openWorld": false}
  // model_request:{model,messages,params,tools} for BeforeModel/AfterModel
}
```

### Decision (stdout)

```jsonc
{
  "decision": "allow",   // allow | deny | ask | modify | synthesize | defer
  "reason": "…",
  "additionalContext": "…",         // concatenated across hooks
  "input": { … },                    // modify: mutated tool input
  "result": { … },                   // synthesize: fabricated tool result
  "updatedToolOutput": "…",          // PostToolUse: rewrite the observed output
  "llm_response": …, "model_override": "…", "request_patch": { … }  // model events
}
```

Merge is **most-restrictive-wins**: `deny > defer > ask > synthesize > modify >
allow`. Two `modify` decisions on one event is an **error** (the call is blocked,
never an arbitrary writer-wins).

---

## Trust (content fingerprints)

A hook is operator-declared **code**. Trust answers one question: *has the content of
a previously-seen hook changed since it was last trusted?* — the threat being a
repo-shipped hook silently rewritten by a `git pull`.

On load, each hook gets a **content-hash fingerprint** (its declarative config + the
resolved command/script bytes), keyed by its stable `id`, compared to a persisted
trusted fingerprint stored **next to the hook config** (`hooks.trust.json` — it is
hook config, not agent state, so it is not a new store):

* fingerprint **unchanged** → `trusted` (the hook runs);
* fingerprint **changed** → `untrusted` — the hook **does not run** (dropped from
  matching), and the typed reason `hook_untrusted_content_changed` is recorded so the
  change is queryable. Re-approval = re-persisting the new fingerprint.
* fingerprint **unseen** → trust-on-first-use: `trusted` + persist. (A brand-new hook
  the operator just authored is not a "change".)

`allowManagedHooksOnly` (`hooks.allow_managed_only`) is the admin lockdown: it drops
every non-managed (`user`/`project`) source, so only managed/admin hooks run.

---

## Audit (on the semantic highway)

**Every** hook invocation — the decision (`allow`/`deny`/`ask`/`modify`/`synthesize`/
`defer`), a denial reason, a hook error/timeout, and a pre-execution rejection —
emits **exactly one** `hook.invoked` semantic event. It rides the semantic-event
highway (captured FULL on the durable trace + ARC, queryable after the fact) — **not**
a new JSONL store (RULE 4 / #737). `hook.invoked` is trace-only substrate: it never
appears on the live UI wire. `SemanticEvent`-event invocations are the one exception
(auditing them would recurse on the highway) — they are still captured in the bounded
recent-invocation ring surfaced by `GET /v1/hooks`.

## Introspection — `GET /v1/hooks`

The read-only inspection surface (the `/hooks`-command analog; it replaced the CRUD
deleted in P2.1). It lists **every loaded hook** — including disabled and
content-untrusted ones — with its `id`, the events it runs `on`, its `match`
predicate, its source scope label (`user`/`project`/`managed`), its content `trust`
state, and whether it is `enabled` (and a `runs` flag = enabled ∧ trusted), plus the
bounded recent `hook.invoked` audit records.

---

## Cross-repo contract note

The gact-tui `contract/SPEC.md` `x_clio_hook_backend` enum does **not** yet include
`declarative` (the backend name this build reports on `GET /v1/capabilities`). Adding
`declarative` to that enum is a tracked cross-repo contract-sweep item; it is **not**
papered over on the CLIO side (the backend name is reported honestly).
