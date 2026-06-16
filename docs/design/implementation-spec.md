# Implementation Spec — ARC as the Live Context Plane

Unified, implementation-ready spec synthesized from five area specs (arc-data-model,
react-integration, trace, lm-boundary, testing-alcf). Authoritative for the build.
Goal and locked decisions: [`GOAL.md`](../../GOAL.md). Design context:
[`arc-live-context-plane.md`](arc-live-context-plane.md).

All paths are absolute under the worktree `/home/jcernuda/clio-arc-live-plane`.
Citations are `file:line` verified against that worktree. dspy is pinned at **3.1.3**
(`.venv/lib/python3.13/site-packages/dspy/predict/react.py`).

This spec is a dependency graph of components and interfaces, **not** a phased timeline.
Build the whole thing as one system; the ordering below is "X needs Y compiled," not
"do X in week 1."

---

## 0. The one-paragraph architecture

ARC becomes the **live context plane**: a new `SegmentStore` (in
`src/clio_agent/arc/segments.py`) holds an ordered, scoped, mutable sequence of
`Segment`s. The gact ReAct loop (`_RetainingReAct`, `gact/app.py:5407`) **writes** one
segment per produced piece (thought / tool_call / observation) as it generates them, and
**reads** the prompt back by overriding dspy's `_format_trajectory` (dspy `react.py:91`)
to rebuild the `thought_/tool_name_/tool_args_/observation_{idx}` dict **from ARC** on
every render. The four ops (`append`, `insert`, `delete`, `summarize`) mutate segments
between renders, so out-of-band edits change the *next* prompt — this is the whole point.
Every op is logged to the durable Trace (`gact/semantic_events.py`) as an `arc.op` event,
and ARC is replayable from those events. A 90%-of-context-window auto-compaction trigger
fires per-expert off the provider's exact `prompt_tokens`, calling
`summarize(all)`. A recording `dspy.BaseCallback` at `on_lm_start` captures the exact
outgoing `messages` for the byte-equality / mutation-propagation acceptance tests.

---

## 1. Component boundaries and interfaces

Seven components. Each box below is "what it is + the exact interface other components
bind to." Conflicts between the area specs are resolved inline (marked **RESOLVED**).

### 1.1 `Segment` (the data model) — `src/clio_agent/arc/schema.py`

A `msgspec.Struct` following the existing `schema.py` conventions (required-first /
defaults-last; `msgspec.field(default_factory=...)` for id/timestamp; module-level
`encode_X`/`decode_X` pair — `schema.py:28-54`, `schema.py:678-848`). `Message`
(`schema.py:28`) is the template.

**RESOLVED — `content` type.** The area specs disagreed (`Dict[str, Any]` vs `Any` vs
`str | dict`). **Use `content: Dict[str, Any]`** (a single msgspec decode type that
round-trips through msgpack cleanly and preserves `tool_call`'s `{name, args}`
structurally so the dspy read-back hands `tool_args_{i}` back as a dict, never a
re-parsed string). Provide a `segment_text(seg)` helper for the flat-text/render/
token-count paths. A `str | dict` union needs tagged unions to round-trip and breaks the
read-back; `Any` loses validation. Single dict type wins.

**RESOLVED — `order` type.** `float`, not `int`. `insert` between `order=2.0` and
`order=3.0` picks `2.5` with **no renumbering** of later segments (gap allocation),
mirroring the B-tree's `(str, float)` composite key (`index.py:39`). `render`'s `*_{idx}`
indices are recomputed by render position, so the float order is internal only.

**RESOLVED — `session_id` field.** Present (infra, not in the locked render schema). The
existing ARC machinery keys everything on `(session_id, …)` (B-tree composite key,
`release_session`/`delete_session` lifecycle). `scope` is the *tag address*
(`agentX/expertY`); `session_id` is the owning session. `segment_text`/render never emit
`session_id`. See §1.2 for how the gact-side combined address resolves to the pair.

```python
# schema.py — add `Literal` to the typing import at schema.py:23
from typing import Any, Dict, List, Literal, Optional   # Literal is new

# ---- add after Message (schema.py:54) ----

SegmentKind = Literal[
    "system", "user", "tool_def", "thought", "tool_call", "observation", "summary"
]
SegmentStatus = Literal["live", "tombstoned"]


class Segment(msgspec.Struct):
    """One ordered, scoped piece of live context — the unit of the ARC live plane.

    Locked schema: GOAL.md:54-68. content shape per kind (GOAL.md:64):
      thought/observation/summary/system/user -> {"text": str}
      tool_call -> {"name": str, "args": dict[str, Any]}
      tool_def  -> {"name": str, "schema": Any} or {"text": str}
    """
    # Required (locked render fields the writer always supplies).
    scope: str                 # tag address "agentX/expertY" (GOAL.md:59)
    kind: SegmentKind          # render + token-attribution category (GOAL.md:63)
    content: Dict[str, Any]    # payload, shape per kind (GOAL.md:64)
    session_id: str            # owning session (infra; not a render field)
    step: int                  # ReAct iteration; -1 for system/user/tool_def (GOAL.md:60)
    order: float               # render order within scope; gap-allocated (GOAL.md:61)
    logical_time: int          # store-assigned monotonic clock (GOAL.md:62)
    # Optional with defaults.
    id: str = msgspec.field(default_factory=lambda: str(uuid.uuid4()))   # op target (GOAL.md:58)
    token_count: int = 0       # cached per-segment estimate (GOAL.md:65)
    derived_from: List[str] = msgspec.field(default_factory=list)        # provenance (GOAL.md:66)
    status: SegmentStatus = "live"   # tombstone deletion; render skips (GOAL.md:67)
    trace_ref: str = ""        # link to the Trace event/turn (GOAL.md:68)
    created_at: float = msgspec.field(default_factory=lambda: time.time())  # diagnostics only


def segment_text(seg: Segment) -> str:
    """Best-effort flat text of content (render + token counting). Pure, no I/O."""
    if seg.kind == "tool_call":
        name = str(seg.content.get("name") or "")
        return f"{name}({msgspec.json.encode(seg.content.get('args') or {}).decode()})"
    text = seg.content.get("text")
    return text if isinstance(text, str) else msgspec.json.encode(seg.content).decode()


# ---- codecs (after decode_variant_record, schema.py:848) ----
def encode_segment(seg: Segment) -> bytes: return msgspec.msgpack.encode(seg)
def decode_segment(data: bytes) -> Segment: return msgspec.msgpack.decode(data, type=Segment)
def encode_segments(segs: List[Segment]) -> bytes: return msgspec.msgpack.encode(segs)
def decode_segments(data: bytes) -> List[Segment]: return msgspec.msgpack.decode(data, type=List[Segment])
```

### 1.2 `SegmentStore` (the ops + read surface) — `src/clio_agent/arc/segments.py` (new)

Standalone class **composed** by `ARCMemory` (exactly as `LiveRuntimeContext` is composed
at `memory.py:99`). Persists through the injected `ARCStore`; uses a `BTreeIndex`
(`index.py`) for ordered access. Does **not** subclass anything. Thread-safe
(`threading.RLock` — `render` may call `list_segments` internally).

**Persistence model — RESOLVED (one record per `(session_id, scope)`).** `render` runs on
the every-iteration hot path; one `store.get` + one `decode_segments` per scope beats N
per-segment gets. Add `"segments"` to `ARC_KINDS` (`storage.py:34`) — both backends derive
containers from it (`storage.py:95`, `:367`), so that single edit auto-provisions the
directory/namespace. Record name = filesystem-safe hash of `f"{session_id}::{scope}"`
(scope contains `/`); reuse the md5 pattern from `store_dataset_profile` (`memory.py:738`).

**RESOLVED — `(session_id, scope)` vs combined-address signatures.** The data-model spec
used `(session_id, scope, ...)`; react-integration used a single combined `scope`. **The
store's canonical interface takes `session_id` and `scope` separately** (it needs both for
the B-tree key and session-lifecycle). The gact side computes a combined address
`f"{session_id}/{agent_def.id}"` for its contextvar; the `ARCMemory` wrapper splits it
(`session_id` is the first path segment, `scope` is the rest) — or, simpler and chosen
here, the gact contextvar stores `session_id` and `scope` as the agent id, and the wrapper
passes both. See §1.4 for the exact wiring. **The scope tag stored on each `Segment.scope`
is the agent/expert tag `agentX/expertY` (no session prefix)** so `scan_scopes("agentX/")`
works for agent-level reads (design table `arc-live-context-plane.md:89-93`).

**The stable op interface (the §7 KV-swap seam — `GOAL.md:86-88`):**

```python
class SegmentStore:
    def __init__(self, store: ARCStore) -> None: ...

    # ---- the four ops (the WRITE surface; each logs to Trace — see §1.5) ----
    def append(self, session_id: str, scope: str, kind: SegmentKind,
               content: dict[str, Any], *, step: int = -1, trace_ref: str = "",
               derived_from: list[str] | None = None, token_count: int = 0) -> Segment:
        """append = insert(end). order = max(order)+1.0; logical_time = next monotonic.
        Cheap: never breaks the cached prefix (design doc:59-60). Returns the Segment."""

    def insert(self, session_id: str, scope: str, position: int, kind: SegmentKind,
               content: dict[str, Any], *, step: int = -1, trace_ref: str = "",
               derived_from: list[str] | None = None, token_count: int = 0) -> Segment:
        """Insert at render position `position` (0-based over LIVE segments).
        order = midpoint of neighbours (gap alloc, no renumber). Breaks prefix from here."""

    def delete(self, session_id: str, scope: str, ids: list[str]) -> int:
        """Tombstone by id (status -> "tombstoned"); render skips them (GOAL.md:67).
        Tombstone-not-erase so the segment survives for Trace reconstruction and
        as-of-T reads before its tombstoning logical_time. Returns count tombstoned."""

    def summarize(self, session_id: str, scope: str, ids: list[str],
                  summary_content: dict[str, Any], *, trace_ref: str = "",
                  token_count: int = 0) -> Segment:
        """summarize = delete(ids) + insert(summary at the first deleted position),
        ATOMIC under the lock (design §4). New Segment kind="summary",
        derived_from=ids (provenance). The LLM call that PRODUCES summary_content is
        the CALLER's job. context-compaction = summarize(all live ids)."""

    def apply(self, op: str, session_id: str, scope: str, **kwargs: Any) -> Any:
        """Stable dispatch over the four ops (the KV-backend swap seam, design §7).
        op in {"append","insert","delete","summarize"}. Raises ValueError on unknown op."""

    # ---- the READ surface (the loop reads this every iteration) ----
    def render(self, session_id: str, scope: str, *, as_of: int | None = None) -> list[Segment]:
        """THE decisive method. Ordered LIVE view: status != "tombstoned", in `order`
        (tie-break logical_time), summaries already substituted. PURE function of the
        store's segments. as_of (logical_time): render the view AS IT WAS at that time —
        only segments with logical_time <= as_of, and a tombstone is effective only if
        ITS tombstoning logical_time <= as_of. as_of=None = current live view."""

    def render_keys(self, session_id: str, scope: str, *, as_of: int | None = None) -> dict[str, Any]:
        """render() projected into dspy's trajectory dict: {thought_{idx}, tool_name_{idx},
        tool_args_{idx}, observation_{idx}, ...}. idx = RENDER POSITION (not Segment.step),
        recomputed after deletes/summaries so the dict is gapless (stock dspy never has
        gaps). summary segments render as observation_{idx}. tool_call.content['args']
        feeds tool_args_{idx} directly (no re-parse). This is what _format_trajectory reads."""

    def render_text(self, session_id: str, scope: str, *, as_of: int | None = None,
                    separator: str = "\n") -> str:
        """render() flattened via segment_text — for byte-equality assertions / inspection."""

    def list_segments(self, session_id: str, scope: str, *,
                      include_tombstoned: bool = False) -> list[Segment]:
        """All segments in order (optionally including tombstoned, for Trace
        reconstruction / provenance expansion). render() is the live subset."""

    def scan_scopes(self, session_id: str, scope_pattern: str = "") -> list[str]:
        """Scope addresses under a prefix (e.g. "agentX/" for agent-level, "" for all).
        Backs cross-scope reads via store.scan("segments", prefix=...) (design table)."""

    def tokens_by_kind(self, session_id: str, scope: str) -> dict[str, int]:
        """Sum token_count of LIVE segments grouped by kind — the per-segment
        attribution that drives compaction targeting (design doc:271-284). Window
        fullness still uses the provider's exact prompt_tokens; this is the breakdown."""

    # ---- lifecycle (mirror LiveRuntimeContext / ARCMemory) ----
    def release(self, session_id: str) -> int:
        """Drop a session's in-memory scopes+indexes (write-through, nothing lost).
        Mirrors LiveRuntimeContext.release (live.py:180). Returns scopes released."""
    def clear(self) -> None:
        """Drop ALL in-memory scope state (live.py:186 parity). Store untouched."""
```

Internal mechanics (private): a `(session_id, scope) -> list[Segment]` in-memory map; a
per-scope `BTreeIndex` for O(log N) locate; a store-wide monotonic `logical_time` counter
(`itertools.count`) recovered past the max persisted `logical_time` on cold load (mirrors
the index-cold fallback at `memory.py:381-392`). `_persist_scope` write-through after every
mutation (like `store_conversation` at `memory.py:187`).

**RESOLVED — `render` vs `render_keys`.** Both exist. `render` returns ordered live
`Segment`s (used by tests, token attribution, agent-scope merge). `render_keys` returns the
dspy trajectory dict (used only by `_format_trajectory`). `render_keys` is `render` +
positional-index projection. Keeping them separate avoids the override re-deriving indices.

### 1.3 `SegmentStore` vs `LiveRuntimeContext` — COEXIST, do not delete

`LiveRuntimeContext` (`live.py:90`) is **turn-grained**, in-memory-only, never read back
into the prompt; it projects `Conversation`/`Invocation` records from the trace fold
(`live.py` `_fold`, `:119-176`). That role is orthogonal and stays. `SegmentStore` is the
**segment-grained** plane the loop reads from (the gap called out at
`arc-live-context-plane.md:137-139`). **`live.py` needs NO edits for the segment plane**
(see §1.5 RESOLVED for why the trace fold does not route segment events into
`LiveRuntimeContext`). The two are siblings under `ARCMemory`.

### 1.4 `_RetainingReAct` integration (write + read + trigger) — `gact/app.py:5407`

The crux. Three changes inside `_retaining_react_cls()._RetainingReAct`, plus contextvar
wiring in the two enclosing expert modules.

**Reaching ARC + scope from inside the dspy module.** The ReAct subclass has no handle to
`ARCMemory` or the expert scope. Thread both via contextvars (the existing `_ACTIVE_*`
pattern, `app.py:184-227`):

- `ARCMemory` = `getattr(_ACTIVE_GACT_APP.get().state, "arc", None)` (the live consumer is
  already registered this way, `app.py:12330`).
- **New contextvars** added next to `_ACTIVE_REACT_TRAJECTORY` (`app.py:227`):
  ```python
  _ACTIVE_REACT_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar(
      "clio_gact_active_react_scope", default="")
  _ACTIVE_REACT_SESSION: contextvars.ContextVar[str] = contextvars.ContextVar(
      "clio_gact_active_react_session", default="")
  _ACTIVE_REACT_CONTEXT_WINDOW: contextvars.ContextVar[int] = contextvars.ContextVar(
      "clio_gact_active_react_context_window", default=0)
  ```

**RESOLVED — scope value + session split.** The area specs used a combined
`f"{session_id}/{agent_def.id}"`. **Resolution: store them split** —
`_ACTIVE_REACT_SESSION = active_session_id`, `_ACTIVE_REACT_SCOPE = agent_def.id` (the tag
address, no session prefix). The store's `Segment.scope` is then the agent/expert tag, so
`scan_scopes("agentX/")` and the design's `agentX/*` agent-scope reads work
(`arc-live-context-plane.md:89-93`). v1 has one ReAct loop per agent_def, so scope =
`agent_def.id`; when the expert/agent hierarchy is wired, scope becomes `agentX/expertY`
and session stays the session_id.

**(a) Write each produced piece (write-through + Trace log).** In `forward` (`app.py:5408`),
after `_ACTIVE_REACT_TRAJECTORY.set(None)` (`app.py:5411`) resolve `_arc`/`_scope`/
`_session`/`_trace_ref`; if `_arc is None or not _scope`, every ARC write below is a
guarded no-op (memory-disabled deployments keep stock behavior — mirrors `app.py:12331`).
Pair each of the four existing trajectory assignments (`app.py:5424-5434`) with a segment
write:

| dspy assignment (current) | paired segment write |
|---|---|
| `trajectory[f"thought_{idx}"] = pred.next_thought` (`:5424`) | `_arc.append_segment(_session, _scope, "thought", {"text": pred.next_thought}, step=idx, trace_ref=_trace_ref)` |
| `trajectory[f"tool_name_{idx}"]` + `tool_args_{idx}` (`:5425-5426`) | `_arc.append_segment(_session, _scope, "tool_call", {"name": pred.next_tool_name, "args": pred.next_tool_args}, step=idx, trace_ref=_trace_ref)` |
| `trajectory[f"observation_{idx}"] = …` (`:5427-5434`) | `_arc.append_segment(_session, _scope, "observation", {"text": trajectory[f"observation_{idx}"]}, step=idx, trace_ref=_trace_ref)` — write AFTER the try/except so it records the same value (success or the `"Execution error in …"` string) |

The local `trajectory` dict is **retained** (still written) so `dspy.Prediction(trajectory=…)`
(`app.py:5446`), `_extract_tools_called_from_trajectory`, and the retain/repair path
(`app.py:5440`, `:5726`) keep working — but it is **no longer the prompt source** (the
`_format_trajectory` override below ignores it). This is exactly `GOAL.md:33` "the local
trajectory dict is no longer an independent source."

`system` / `tool_def` segments (the static signature instructions + tool list) are
rendered by dspy itself and are **not** in the trajectory dict, so they are out of
`render_keys`. Write them **once at module build** (in `BlueprintExpertModule.__init__`
after the system prompt is assembled, and once per tool) so `tokens_by_kind` accounts for
the fixed `tool_def`/`system` cost (`arc-live-context-plane.md:281`). They never enter
`render_keys`.

**(b) Read the prompt from ARC — override `_format_trajectory` (NOT `forward`'s dict).**
This is the locked seam (`GOAL.md:74-77`). dspy's `_format_trajectory` (`react.py:91-94`)
is the **sole** function converting the trajectory dict → the rendered `trajectory`
InputField string, called once per render inside `_call_with_potential_trajectory_truncation`
(`react.py:151`). Override it to read ARC:

```python
def _format_trajectory(self, trajectory: dict[str, Any]) -> str:
    _scope = _ACTIVE_REACT_SCOPE.get(); _session = _ACTIVE_REACT_SESSION.get()
    _app = _ACTIVE_GACT_APP.get()
    _arc = getattr(_app.state, "arc", None) if (_app is not None and _scope) else None
    if _arc is None or not _scope:
        return super()._format_trajectory(trajectory)          # stock fallback
    arc_keys = _arc.render_segments_keys(_session, _scope)      # the dspy *_{idx} dict from ARC
    adapter = dspy.settings.adapter or dspy.ChatAdapter()
    traj_sig = dspy.Signature(f"{', '.join(arc_keys.keys())} -> x")
    return adapter.format_user_message_content(traj_sig, arc_keys)
```

This mirrors stock `_format_trajectory` **line-for-line except the dict source is ARC**.
Byte-equality (`GOAL.md:27`) holds by construction: both build the signature from
`keys()` and render via the same `adapter.format_user_message_content`. When a segment is
tombstoned/summarized, `arc_keys` differs and the string reflects exactly that — the
mutation-propagation contract (`GOAL.md:30-33`).

> ⚠️ **`_format_trajectory` is called for BOTH `self.react` (loop) and `self.extract`
> (final), and three times each inside the truncation retry** (`react.py:101,118,147-151`).
> The override must be correct for all of them: the extract step renders the *same* ARC
> scope (the full live view at that moment) — which is what we want (extract sees the
> compacted/edited context too).

**(c) 90% auto-compaction — override `_call_with_potential_trajectory_truncation`.** This
is the single per-render chokepoint (`react.py:146`), called for both react and extract,
running synchronously under the expert's contextvars (`app.py:5688-5689`). Fire the check
*before* the send:

```python
def _call_with_potential_trajectory_truncation(self, module, trajectory, **input_args):
    self._maybe_autocompact()                         # proactive, scope-aware, BEFORE send
    return super()._call_with_potential_trajectory_truncation(module, trajectory, **input_args)

def _maybe_autocompact(self) -> None:
    _scope = _ACTIVE_REACT_SCOPE.get(); _session = _ACTIVE_REACT_SESSION.get()
    _app = _ACTIVE_GACT_APP.get()
    _arc = getattr(_app.state, "arc", None) if (_app is not None and _scope) else None
    if _arc is None or not _scope:
        return
    window = _ACTIVE_REACT_CONTEXT_WINDOW.get()
    last = _last_prompt_tokens()                      # exact provider count (see §1.6)
    if not window or not last:
        return
    if (last / window) >= _autocompact_threshold():   # configurable, default 0.85
        live_ids = [s.id for s in _arc.render_segments(_session, _scope)]
        summary = _summarize_segments_llm(_arc.render_segments(_session, _scope))  # the LLM call
        _arc.summarize_segments(_session, _scope, live_ids, {"text": summary})
```

`super()` preserves dspy's reactive `truncate_trajectory` retry (`react.py:147-156`) as the
never-fired backstop (`GOAL.md:38`). After `summarize_segments`, the **next**
`_format_trajectory` renders the compacted scope — no resend machinery needed (dspy
re-renders every call).

**Contextvar wiring in the enclosing modules.** Set `_ACTIVE_REACT_SCOPE` /
`_ACTIVE_REACT_SESSION` / `_ACTIVE_REACT_CONTEXT_WINDOW` immediately before the program call
and reset in the existing `finally`:

- `BlueprintExpertModule.forward`: after `kwargs` is assembled (`app.py:5620`), set the
  three contextvars (`scope=self.agent_def.id`, `session=active_session_id`,
  `window=_resolve_expert_context_window(self.config)`); reset in the `finally` at
  `app.py:5808-5810` next to the `_ACTIVE_BLUEPRINT_TOOL_ROWS` reset. **Note** the repair
  loop (`app.py:5671-5807`) re-invokes `self.program(**…)` multiple times within the same
  scope — all those LM calls share the scope, which is correct.
- `ToolUserAgentModule.forward`: same three contextvars before `self.react_agent(...)`
  (`app.py:5974`), reset in a `finally`.

### 1.5 Trace logging + replay — `gact/semantic_events.py`, `gact/app.py`, `arc/replay.py` (new)

**RESOLVED — write path: direct store mutation + synchronous Trace emit (NOT
event-sourced re-fold).** The trace area spec offered "Option A" (emit an event, let a live
consumer fold it into the store). **Rejected for the write path** because it (1) makes the
loop's live store depend on the event round-trip, (2) adds a second concern to the live
fold, and (3) the design's live seam (`on_semantic_event` → `LiveRuntimeContext`) is
turn-grained and a poor fit for segment events. **Chosen:** `SegmentStore.apply()` mutates
the in-memory+persisted store directly AND emits one `arc.op` event to the durable Trace in
the same call (single funnel — "every op logged by construction," `GOAL.md:39`). Replay
(below) reconstructs ARC from those `arc.op` events. This satisfies "every op logged" +
"reconstructable from Trace" without coupling the live read path to the event bus. The live
fold (`live.py`) is **not** taught about `arc.op` — segments are not turn state.

> Implementation note: `SegmentStore` cannot import gact's `_emit_semantic_event` (layering:
> `arc/` must not depend on `gact/`). **The emit is injected.** `SegmentStore.__init__`
> takes an optional `op_logger: Callable[..., dict] | None`; `ARCMemory` (which *is* allowed
> to know gact via the app handle) wires it, or the gact layer passes a logger that calls
> `_emit_arc_op`. If `op_logger is None` (unit tests, memory-only), ops still work and just
> aren't logged — the trace-separation tests inject a capturing logger. This keeps `arc/`
> dependency-clean while honoring `GOAL.md:39`.

**(a) The `arc.op` event.** One new event type (a free string — `event_type` has no
enum/registry; `_fold` dispatches on string literals, `live.py:120`). Add next to
`_emit_semantic_event` (`app.py:437`):

```python
ARC_OP_EVENT_TYPE = "arc.op"   # the only new event_type string

def _emit_arc_op(app, sid, *, op, scope, logical_time, step=None,
                 segments_written=None, segments_tombstoned=None,
                 position=None, derived_from=None, turn_id="", trace_id=""):
    """Log ONE applied ARC mutation to the durable Trace, AFTER it succeeds.
    segments_written carry FULL segment dicts -> durable; SSE redacts `content`
    via SENSITIVE_KEYS. This event is the replay record."""
    return _emit_semantic_event(
        app, sid, ARC_OP_EVENT_TYPE,
        turn_id=turn_id or _ACTIVE_GACT_TURN_ID.get(),
        trace_id=trace_id or _ACTIVE_GACT_TRACE_ID.get(),
        status=op,                                       # append|insert|delete|summarize
        summary=f"arc {op} @{scope} (lt={logical_time})",
        actor={"role": "runtime", "component": "arc", "scope": scope},
        subject={"scope": scope, "logical_time": logical_time, "step": step, "position": position},
        payload={"op": op, "scope": scope, "logical_time": logical_time, "step": step,
                 "position": position, "segments_written": segments_written or [],
                 "segments_tombstoned": segments_tombstoned or [],
                 "derived_from": derived_from or []},
        detail_level="off",     # high-volume durable-only; FULL still recorded (app.py:396-398)
    )
```

`content` / `args` / `text` are already in `SENSITIVE_KEYS` (`semantic_events.py:29`), so
SSE strips them while the **durable trace keeps them FULL** (`semantic_events.py:461`,
`project_full`) — exactly what op-logging needs.

**Per-op payload (`segments_written` / `segments_tombstoned`):**

| op | `status` | `segments_written` | `segments_tombstoned` | extra |
|---|---|---|---|---|
| `append`/`insert` | `"append"`/`"insert"` | `[new_segment]` (FULL dict) | `[]` | `position` for insert |
| `delete` | `"delete"` | `[]` | `[id, …]` | — |
| `summarize` | `"summarize"` | `[summary_segment]` | `[replaced_id, …]` | `derived_from=[replaced_id, …]` |

`summarize` is **one atomic `arc.op`** carrying both the tombstoned range and the summary —
replay can never see a half-applied summarize.

**`trace_ref` back-link.** `_emit_arc_op` returns the FULL emitted dict whose `event_id` is
the stable id. After emit, stamp each written segment's `trace_ref = returned["event_id"]`
(chicken-and-egg: emit first, then set). `trace_ref` is the event's own id, not part of the
payload; replay derives it identically.

**(b) Replay — reconstruct ARC from the Trace.** Net-new (no reader exists today — the
trace is write-only; only *live* consumers read events). Two functions:

```python
# semantic_events.py — alongside FileSemanticTraceBackend
def read_semantic_trace(path: Path, session_id: str) -> Iterator[dict[str, Any]]:
    """Yield FULL event dicts from the durable JSONL trace, file order.
    File order == emit order == causal order (single append-only writer,
    _trace_writer_loop, :271-290). Inverse of FileSemanticTraceBackend.emit."""

# arc/replay.py (new)
def reconstruct_arc_segments(events, *, scope_filter=None, as_of_logical_time=None) -> list[dict]:
    """Reconstruct the LIVE segment set by replaying arc.op events.
    1. ONLY event_type == "arc.op".
    2. Order by payload["logical_time"] asc; tie-break file order.
    3. Fold: append/insert -> add segments_written; delete -> tombstone
       segments_tombstoned; summarize -> tombstone + add the summary.
    4. as_of_logical_time: ignore ops with lt > as_of.
    5. scope_filter: match each segment's scope (tag namespace, design :87-93).
    Returns LIVE segments sorted by (logical_time, order) == render order.
    trace_ref of each = the replaying event's event_id (byte-identical to live)."""
```

Only `arc.op` events participate; `turn.*`/`tool.call.*`/`llm.*`/`expert.*` are narrative,
not segment mutations (they stay in the trace for the §8.7 audit cross-check). Each `arc.op`
is self-describing (full segment dicts inline), so replay needs no prior ARC state beyond
what it has folded — the Trace is sufficient (`GOAL.md:40`).

### 1.6 LM-boundary token readback + the recording harness

**Exact `prompt_tokens` (drives the trigger).** Cache is forced off for clio LMs
(`config.py:1022`) so every call lands usage in `dspy.settings.usage_tracker.add_usage(...)`
(dspy `clients/lm.py:167`). The gact expert call enters `with dspy.context(lm=create_lm(...),
adapter=...)` (`app.py:5688`) but does **not** currently wrap in `dspy.track_usage()`. **Add
`track_usage` to that context** so a tracker is live:

```python
with dspy.track_usage() as _usage, dspy.context(lm=create_lm(_attempt_config), adapter=adapter):
    result = self.program(**_call_kwargs)
```

**RESOLVED — last-call, not total.** The trigger reads the **last** call's `prompt_tokens`
(each send is the full prompt; window-fullness = that single total), NOT
`get_total_tokens()` which *sums* across calls (`usage_tracker.py:35-50`):

```python
def _last_prompt_tokens() -> int:
    tracker = dspy.settings.usage_tracker
    if tracker is None: return 0
    model = getattr(dspy.settings.lm, "model", "")
    entries = tracker.usage_data.get(model) or []
    return int(entries[-1].get("prompt_tokens", 0)) if entries else 0
```

`_maybe_autocompact` runs *before* the upcoming send, so it reads the *previous* call's
exact count — exactly "compact before the next send when ratio ≥ threshold"
(`arc-live-context-plane.md:230`).

**Window resolution (the confirmed gap).** `config.context_window` / `chosen_context` are
`field(init=False, default=None)` (`config.py:258-259`), populated **only** by
`apply_handshake` (`config.py:348,354`) — which is **NOT** called on the gact runtime path
(`_dynamic_agent_lm_config`, `app.py:3916-3946`, never calls it; handshake routes are
report-only HTTP). So both are `None` at the ReAct call site. **Two fixes, do both:**

1. **Propagate at config-build (root fix).** In `_dynamic_agent_lm_config` (`app.py:3931`),
   after constructing the new `LMProviderConfig`, copy the handshake-discovered fields from
   `base_config` when same provider+model (they're plain `init=False` fields, assignable):
   ```python
   new = LMProviderConfig(...)        # existing app.py:3931
   if same_provider and new.model == base_config.model:
       new.context_window = base_config.context_window
       new.chosen_context = base_config.chosen_context
   return new
   ```
2. **Resolution ladder at module build** (`_resolve_expert_context_window(cfg) -> int`,
   stashed in `_ACTIVE_REACT_CONTEXT_WINDOW`): (1) `cfg.chosen_context or cfg.context_window`;
   (2) `litellm.get_model_info(cfg.model).get("max_input_tokens")`; (3) the `"context"`
   field from `model_limits.json` for `cfg.model` (via `db.lookup_context`,
   `providers/handshake/sources/db.py:85`); (4) `0` ⇒ auto-compaction disabled, dspy's
   reactive backstop remains.

**Threshold — configurable.** `_autocompact_threshold()` reads env `CLIO_AUTOCOMPACT_PCT`
(float 0–1), default `0.85` (design recommends < 0.90, `arc-live-context-plane.md:237`).
Place near the existing `_extract_repair_attempts` config-reader (`app.py:4394`).

**`litellm.token_counter` — pre-send guard ONLY.** Used to catch a single huge observation
overflowing in one shot before sending; never for the recurring trigger (which always uses
exact `prompt_tokens`). With no `custom_tokenizer` it falls back to tiktoken for the local
fleet (approximate). Per-model tokenizer enrichment in `model_limits.json` +
`custom_tokenizer` (`litellm/utils.py:1784`) and post-call self-calibration are the
open-question hooks (`GOAL.md:107-108`) — wire the seam, do not block on full accuracy.

**Recording harness — a `dspy.BaseCallback` at `on_lm_start` (NOT an LM subclass).** New
file `src/clio_agent/arc/prompt_recorder.py`. Non-invasive; works against the real
production LM (for live ALCF runs) and a scripted `dspy.utils.DummyLM` (for unit tests).
`on_lm_start(call_id, instance, inputs)` sees `inputs["messages"]` = the literal `list[dict]`
dspy is about to send (the decorator computes it via `inspect.getcallargs`,
`dspy/utils/callback.py:263-272`). **Deep-copy** `inputs["messages"]` in the callback — the
live plane mutates the message list between iterations, so without a snapshot every captured
call aliases the final state and the prefix/byte-equality tests are meaningless.

```python
@dataclass(frozen=True)
class CapturedCall:
    call_id: str; model: str; messages: list[dict[str, Any]]
    prompt: str | None; kwargs: dict[str, Any]

class PromptRecorder(dspy.BaseCallback):
    def __init__(self) -> None:
        self._calls: list[CapturedCall] = []; self._lock = threading.Lock()
    def on_lm_start(self, call_id, instance, inputs) -> None:
        msgs = inputs.get("messages")
        cc = CapturedCall(call_id=call_id, model=getattr(instance, "model", ""),
                          messages=copy.deepcopy(msgs) if msgs is not None else [],
                          prompt=inputs.get("prompt"),
                          kwargs={k: v for k, v in inputs.items()
                                  if k not in ("messages", "prompt") and not k.startswith("api_")})
        with self._lock: self._calls.append(cc)
    # calls (snapshot under lock), last(), reset(), joined_text(idx=-1) accessors
```

Register globally via `dspy.configure(callbacks=[rec])` or scope it with
`dspy.context(callbacks=[rec])` (union semantics, `dspy/utils/callback.py:286-288`); prefer
the per-context form in tests for isolation. For live runs, add `callbacks=[recorder]` to
the expert `dspy.context(...)` block (`app.py:5688` / `:5970`), debug-gated. A runtime-
invariant variant recomputes the expected prompt from ARC@scope and asserts byte-equality
against `inputs["messages"]`, raising in dev (env `CLIO_ARC_PROMPT_INVARIANT=1`) — the
§8.6 long-run drift guard.

### 1.7 `ARCMemory` pass-throughs — `src/clio_agent/arc/memory.py`

Thin delegations so callers never reach into `self._segments` directly (same style as
`on_semantic_event`/`get_live_context`, `memory.py:1044-1062`).

- **Construct** after `self._store` is set (`memory.py:93`), before `self._live`
  (`memory.py:99`): `self._segments = SegmentStore(self._store, op_logger=<injected>)`.
  The `op_logger` is wired from the app handle (see §1.5 layering note) or left `None` when
  ARC is used standalone.
- **Public surface** (add after `memory.py:1062`): `append_segment(session_id, scope, kind,
  content, *, step=-1, trace_ref="", token_count=0)`, `insert_segment(...)`,
  `delete_segments(...)`, `summarize_segments(...)`, `apply_segment_op(op, session_id,
  scope, **kw)`, `render_segments(session_id, scope, *, as_of=None)`,
  `render_segments_keys(session_id, scope, *, as_of=None)`,
  `render_segment_text(...)`, `segment_tokens_by_kind(session_id, scope)`. Each delegates to
  the matching `SegmentStore` method.
- **Lifecycle** (3 edits): `release_session` — add `self._segments.release(session_id)` and
  surface it in the return dict (`memory.py:1098-1099`); `flush_and_release` — add
  `self._segments.clear()` after `self._live.clear()` (`memory.py:1118`); `clear_all` — add
  `self._segments.clear()` after `self._store.clear()` (`memory.py:1149`; the store clear
  already wipes the `"segments"` kind since it's in `ARC_KINDS`).

---

## 2. Dependency order (NOT a timeline — "X needs Y to compile/link")

```
Segment + codecs (schema.py)                [leaf — no deps]
        │
        ├─► ARC_KINDS += "segments" (storage.py)        [leaf — independent edit]
        │
        ▼
SegmentStore (segments.py)                  needs: Segment, codecs, ARCStore, BTreeIndex,
        │                                          ARC_KINDS "segments"; op_logger is INJECTED
        │                                          (so it does NOT depend on gact)
        ▼
ARCMemory pass-throughs (memory.py)         needs: SegmentStore
        │
        ├──────────────────────────────┬──────────────────────────────┐
        ▼                               ▼                              ▼
_format_trajectory override        _emit_arc_op + arc.op          PromptRecorder
+ append writes (app.py:5407)      logging (app.py:437)           (prompt_recorder.py)
  needs: ARCMemory pass-throughs,    needs: _emit_semantic_event    needs: dspy only
  _ACTIVE_REACT_* contextvars        (exists), Segment dict shape   [leaf — independent]
        │                               │
        ▼                               ▼
_maybe_autocompact + window        read_semantic_trace (semantic_events.py)
  resolution + track_usage           + reconstruct_arc_segments (replay.py)
  needs: contextvars, exact          needs: arc.op payload shape (from _emit_arc_op),
  prompt_tokens, _resolve_window,    Segment fold semantics
  config propagation fix                  │
  (config.py _dynamic_agent_lm_config)    ▼
        │                          Trace-audit acceptance (test_live_plane_audit.py)
        └───────────────┬──────────────────┘
                        ▼
        Acceptance tests (byte-equality, mutation-propagation, prefix)
          need: SegmentStore + ops + _format_trajectory override + PromptRecorder
```

**Hard dependencies:**
- `SegmentStore` depends on `Segment`/codecs, `ARC_KINDS += "segments"`, `BTreeIndex`,
  `ARCStore`. It depends on `op_logger` only as an injected callable (no gact import).
- The `_format_trajectory` override and the three append writes depend on the `ARCMemory`
  pass-throughs and the new contextvars.
- `_maybe_autocompact` depends on: the contextvars, `track_usage` in the expert context,
  the exact-`prompt_tokens` reader, `_resolve_expert_context_window`, and the
  `_dynamic_agent_lm_config` propagation fix (no fix ⇒ no denominator ⇒ trigger inert).
- Replay depends on the `arc.op` payload shape emitted by `_emit_arc_op`.
- The decisive mutation-propagation tests depend on ops + the `_format_trajectory` override
  + the recording harness all being present (true-by-construction first, then guard).

**Independent (no ordering constraint among them):** `PromptRecorder`, the `ARC_KINDS`
edit, the `_dynamic_agent_lm_config` propagation fix, the unit `SegmentStore` tests.

---

## 3. Trickiest integration points + risks

1. **`_format_trajectory` is the load-bearing seam, and it runs for `extract` too.** Stock
   calls it for both `self.react` and `self.extract`, three times each inside the
   truncation retry (`react.py:101,118,147-151`). The override must render ARC@scope every
   time — including for extract, which then sees the compacted/edited context (desired). If
   the override raised for extract, the final answer would break. Guard it (ARC-disabled →
   `super()`), and unit-test the extract render path explicitly.

2. **ARC-authoritative byte-equality is "modulo static signature/instructions."** The
   recording callback captures the **whole** message list; the static system prompt +
   signature framing are NOT from ARC. Tests must isolate the rendered-`trajectory` span
   (the last user message, `react.py:91-94,151`) via a `_trajectory_content(messages)`
   helper before comparing, or the static framing defeats exact equality. Byte-equality is
   true-by-construction (same key set, same adapter call) only if `render_keys` returns the
   **identical key order and values** stock would — verify ordering matches insertion order
   and that `tool_args_{i}` is the same dict object shape dspy formats.

3. **`render_keys` index = render position, not `Segment.step`.** After a `delete`/
   `summarize`, the live segments must renumber to a gapless `0..n` (stock dspy never has
   index gaps). Getting this wrong yields a prompt with `thought_0, thought_2, …` that
   diverges from stock and breaks the prefix test. The `step` field stays as provenance;
   the rendered index is recomputed each call.

4. **The killer `delete(C)→absent` test only passes if the local dict is truly not the
   source.** The override must ignore the local `trajectory` dict for the prompt. If any
   path still renders from the local dict, a deleted segment lingers (it was written to the
   dict at `app.py:5424-5434` and never removed). The `_extract_tools_called_from_trajectory`
   consumer (`app.py:5822`) still reads the local dict — that's fine, it's not the prompt —
   but confirm nothing else feeds the dict back into a render.

5. **Layering: `arc/` must not import `gact/`.** The op-logger is injected
   (`SegmentStore(store, op_logger=...)`). Do not `from clio_agent.gact import ...` inside
   `arc/segments.py`. The trace-separation tests inject a capturing logger; production wires
   `_emit_arc_op` from the app boundary.

6. **Window denominator is `None` today on the dynamic-agent path.** Without the
   `_dynamic_agent_lm_config` propagation fix AND the resolution ladder, `_maybe_autocompact`
   can never compute a ratio and 90% compaction is silently inert. This is the most likely
   "it builds but the feature does nothing" failure. The auto-compaction unit test must feed
   a known window + synthetic `prompt_tokens` and assert firing; a live test must confirm a
   real window resolves.

7. **`track_usage` must wrap the expert context or `_last_prompt_tokens` returns 0.** If the
   tracker isn't installed in the `dspy.context`, `dspy.settings.usage_tracker is None` and
   the trigger is inert. Re-enabling the LM cache (`config.py:1022`) would also silently
   break `prompt_tokens` readback (cache-hit short-circuits `add_usage`) — do not.

8. **Concurrent writers / as-of-T are spec'd but only exercised at agent scope.** v1 is
   single-expert-per-scope; `logical_time` + `as_of` are implemented and unit-tested, but
   the concurrent-shared-scope semantics (`GOAL.md:106`) are an open question deferred until
   agent-scope sharing is wired. Do not block v1 on it; do keep `logical_time` correct and
   monotonic (recovered past the persisted max on cold load).

9. **Deep-copy in `on_lm_start`.** The live plane mutates the message list across
   iterations; without a snapshot all captured calls alias the final state, silently
   breaking every byte-equality / mutation / prefix test (they'd "pass" against the wrong
   data or all look identical). This is a subtle, high-impact correctness trap in the test
   harness itself.

10. **Repair loop re-invokes the program under the same scope.** The blueprint repair path
    (`app.py:5671-5807`) calls `self.program(**…)` up to `1 + _max_repairs` times. Each
    re-invocation re-renders from ARC and re-appends segments. Ensure re-extract-only
    repairs (`_reextract_over_retained_trajectory`, `app.py:5743`) do not double-append the
    tool loop's segments — the re-extract renders the existing ARC scope and should NOT
    re-run the loop writes. Verify the append writes live only in the loop body, not in the
    extract path.

---

## 4. Test commands

### 4.1 Default lane (no model, no server) — the CI gate

Pure-unit + recording-harness (scripted `DummyLM`). New files under `tests/test_arc/`:
`test_segment_store.py`, `test_live_plane_byte_equality.py`, `test_auto_compaction.py`,
`test_trace_separation.py`, plus `tests/test_arc/conftest.py` (the `recording_lm_callback`
fixture + `_trajectory_content` helper).

```bash
cd /home/jcernuda/clio-arc-live-plane
CLIO_ALLOWED_ROOTS="${TMPDIR:-/tmp}:$PWD" \
  uv run pytest tests/test_arc/ tests/test_gact/test_trajectory_retention.py \
  -m "not integration and not live and not real_case" -v
ruff check src/
```

The autouse `allow_pytest_tmp_path` fixture (`tests/conftest.py:16-42`) sets
`CLIO_ALLOWED_ROOTS` to `tmp_path`, defaults `CLIO_LM_MODEL=ibm/granite-4-h-tiny`, and
disables the registry bootstrap — so the unit files need no extra env.

Acceptance coverage in this lane:
- **Segment store ops** — append monotonic order; insert at position; delete tombstones
  (still in store, absent from `render`); summarize replaces range + records `derived_from`;
  render orders/skips/substitutes; scope isolation (expert vs `agentX/*`); token_count
  cached per segment; as-of-T returns the prefix.
- **Byte-equality + mutation propagation (decisive)** — independently `render`+format,
  byte-equal the captured trajectory span; marker↔segment bijection; `append(X)→present`;
  **`delete(C)→absent`** (the killer — a shadow dict would still have C);
  `summarize(E→E')→E' present, E absent`; `insert(mid,Y)→Y at position`; append-only step is
  a literal prefix of the next; an edit breaks the prefix at the edited boundary and nowhere
  earlier.
- **Auto-compaction** — fires on exact `prompt_tokens/window ≥ threshold` before the next
  send; threshold env-configurable; per-expert (two scopes, two windows, independent);
  dspy's `truncate_trajectory` never fires (spy/monkeypatch); `token_counter` is the
  pre-send guard only.
- **Trace separation** — every `apply` emits a durable event + segment `trace_ref` links
  back; `reconstruct_arc_segments(read_semantic_trace(...))` `render`s equal to the live
  store; after `summarize(all)` the Trace still holds the originals and `derived_from`
  expands the summary.

### 4.2 Live lane (ALCF/Argonne, gated by `CLIO_RUN_LIVE`)

`tests/test_arc/test_live_plane_audit.py` — the §8.7 trace audit over a real run; reuses the
`tests/test_real_cases/clio_sut.py` driver via `CLIO_GACT_URL`. Run a real turn, read the
durable JSONL, replay it, reconstruct ARC@each LM call, assert `==` the prompt captured at
that call (observation point is the **Trace**, since a server-side run can't register a
test-process callback).

```bash
cd /home/jcernuda/clio-arc-live-plane
export CLIO_GACT_URL='http://127.0.0.1:17960'   # the running gact server (see §5)
export CLIO_RUN_LIVE=1
export CLIO_ALLOWED_ROOTS="$PWD:${TMPDIR:-/tmp}"
export CLIO_SEMANTIC_TRACE_BACKEND=file
export CLIO_SEMANTIC_TRACE_PATH="$PWD/.clio-traces"

uv run pytest tests/test_arc/test_live_plane_audit.py \
  -o addopts="" --provider argonne_sophia --model openai/gpt-oss-120b -v

# regression: the existing real case stays green on the new plane
uv run pytest tests/test_real_cases/test_earthscope_case.py \
  -o addopts="" --provider argonne_metis -v
```

---

## 5. ALCF/Argonne live-run command

End-to-end through the gact ReAct path against a real Argonne model. `CLIO_LM_PROVIDER=argonne`
is what wires the agent at startup (`app.py:22576`); `CLIO_SEMANTIC_TRACE_BACKEND=file` is
required for the trace-audit (durable trace is opt-in, `semantic_events.py:386`).

```bash
cd /home/jcernuda/clio-arc-live-plane

# 1) one-time auth (browser OAuth; or export CLIO_ARGONNE_TOKEN on a headless box)
uv sync --extra dev --extra optimizers
uv run python -m clio_agent.providers.argonne_auth status || \
  uv run python -m clio_agent.providers.argonne_auth authenticate

# 2) env (Sophia cell; swap /sophia/ -> /metis/ + drop the openai/ prefix for Metis)
export CLIO_LM_PROVIDER=argonne
export CLIO_LM_API_BASE='https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1'
export CLIO_LM_MODEL='openai/gpt-oss-120b'
export CLIO_ALLOWED_ROOTS="$PWD:${TMPDIR:-/tmp}"
export CLIO_SEMANTIC_TRACE_BACKEND=file
export CLIO_SEMANTIC_TRACE_PATH="$PWD/.clio-traces"
# optional: export CLIO_AUTOCOMPACT_PCT=0.85   # auto-compaction threshold
# optional: export CLIO_LM_MAX_TOKENS=32000    # if the ALCF static default caps at 4096

# 3) launch gact (main() at app.py:22518)
uv run clio-agent-gact --host 127.0.0.1 --port 17960 &

# 4) wait for the LM to wire, then fire one turn through the ReAct path
until curl -sf 'http://127.0.0.1:17960/v1/providers/lm/wait?timeout=30' \
        | grep -q '"state":"ready"'; do sleep 2; done
SID=$(curl -sf -XPOST http://127.0.0.1:17960/v1/sessions \
        -H 'content-type: application/json' -d '{"title":"alcf-live-plane"}' \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -sf -XPOST "http://127.0.0.1:17960/v1/sessions/$SID/messages" \
     -H 'content-type: application/json' \
     -d '{"parts":[{"type":"text","text":"List the HDF5 datasets in <a staged file> and summarize."}]}'

# 5) the durable trace the audit test replays
ls "$PWD/.clio-traces/$SID.semantic.jsonl"
```

The `/v1/providers/lm/wait` long-poll is the readiness gate; the same server is what the
live audit test (§4.2) points at via `CLIO_GACT_URL`. The `--provider/--model` flags in the
test command are the agent-test cell pins (`clio_sut.py:9-10`; presets at
`providers/registry.py:118-161`; Sophia keeps the `openai/` prefix on the wire,
`config.py:1058-1073`).

---

## 6. Concrete edit-point index (file:line)

**`src/clio_agent/arc/schema.py`** — add `Literal` to the import (`:23`); add
`SegmentKind`/`SegmentStatus`/`Segment`/`segment_text` after `Message` (`:54`); add
`encode_segment`/`decode_segment`/`encode_segments`/`decode_segments` after `:848`.

**`src/clio_agent/arc/storage.py`** — add `"segments"` to `ARC_KINDS` (`:34`). No other
storage change.

**`src/clio_agent/arc/segments.py`** — **new.** `SegmentStore` (the four ops + `apply` +
`render`/`render_keys`/`render_text`/`list_segments`/`scan_scopes`/`tokens_by_kind` +
`release`/`clear`); `op_logger` injected.

**`src/clio_agent/arc/replay.py`** — **new.** `reconstruct_arc_segments(...)`.

**`src/clio_agent/arc/memory.py`** — import `SegmentStore` (`:49`); construct
`self._segments` after `:93`; add the `*_segment(s)` pass-throughs after `:1062`; wire
`release`/`clear` into `release_session` (`:1098-1099`), `flush_and_release` (`:1118`),
`clear_all` (`:1149`).

**`src/clio_agent/arc/prompt_recorder.py`** — **new.** `PromptRecorder(dspy.BaseCallback)` +
`CapturedCall`.

**`src/clio_agent/arc/live.py`** — **no edits** (sibling, not modified; the trace fold is
NOT taught about `arc.op`).

**`src/clio_agent/gact/semantic_events.py`** — add `read_semantic_trace(path, session_id)`
after `:344`; add `Iterator`/`Iterable` to the typing import (`:21`).

**`src/clio_agent/gact/app.py`** —
- new `_ACTIVE_REACT_SCOPE` / `_ACTIVE_REACT_SESSION` / `_ACTIVE_REACT_CONTEXT_WINDOW`
  contextvars after `:227`;
- `ARC_OP_EVENT_TYPE` const + `_emit_arc_op(...)` after `:437`;
- `_resolve_expert_context_window(cfg)` + `_autocompact_threshold()` helpers near `:4394`;
- `_dynamic_agent_lm_config` (`:3931`): propagate `context_window`/`chosen_context` from
  `base_config`;
- `_RetainingReAct` (`:5407`): resolve `_arc`/`_scope`/`_session`/`_trace_ref` after `:5411`;
  three `append_segment` writes at `:5424`/`:5426`/`:5434`; `_format_trajectory` override;
  `_call_with_potential_trajectory_truncation` override → `_maybe_autocompact` →
  `_last_prompt_tokens`;
- `BlueprintExpertModule.forward` (`:5620`): set the three contextvars; reset in `finally`
  (`:5808-5810`); wrap the program call in `dspy.track_usage()` (`:5688`);
- `ToolUserAgentModule.forward` (`:5974`): same contextvar wiring + `track_usage`.

**`src/clio_agent/config.py`** — optional `autocompact_threshold` field near `:246`
(env-sourced in `load_config_from_env`) if preferring config over a gact-local env read.

**`tests/test_arc/`** — new `conftest.py`, `test_segment_store.py`,
`test_live_plane_byte_equality.py`, `test_auto_compaction.py`, `test_trace_separation.py`,
`test_live_plane_audit.py` (live, `CLIO_RUN_LIVE`-gated, reuses `clio_sut.py`).

---

## 7. Load-bearing decisions (the five to not second-guess)

1. **`content: Dict[str, Any]`** (not `str|dict`, not `Any`) — `tool_call`'s `{name,args}`
   round-trips structurally so the dspy read-back hands `tool_args_{i}` back as a dict.
2. **`order: float` with gap allocation** — mid-insert never renumbers later segments;
   render `*_{idx}` indices are recomputed by position, so float order is internal.
3. **One store record per `(session_id, scope)`** — `render` is the every-iteration hot
   path; batch the whole scope into one get/decode.
4. **Write-through + synchronous `arc.op` Trace emit inside `apply`** (op_logger injected) —
   single funnel, every op logged, ARC replayable; the live read path does NOT depend on the
   event bus, and `arc/` does NOT import `gact/`.
5. **Trigger off the LAST call's exact `prompt_tokens / window`** (not `get_total_tokens()`
   sum, not `token_counter`), with `track_usage` installed and the window propagated past the
   `apply_handshake` gap — checked in `_maybe_autocompact` before the next send.
