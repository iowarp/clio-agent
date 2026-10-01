"""Measure-first benchmark for Phase 11a ("the context view").

clio-core (ARC) is the data highway; the agent's context should be a VIEW over it.
Today every ReAct step re-folds the whole session's content lane. This script measures
that cost on the REAL code path against a REAL, PRIVATE clio-core daemon, before any
change is made:

1. **warm context build per step** -- what ``ClioReAct`` does each step: its
   recorder's ``read_steps`` (the scope's context view folded into messages, closed
   steps cached), ms p50/p90, plus the atoms each read folded (``fold_atoms`` input);
   ``render_ms`` is the view snapshot alone (``ARCMemory.context_view``);
2. **append cost per atom** -- every ``ARCMemory.append_segment`` the real
   ``StepRecorder`` issues: wall ms, store RPCs (ARCStore ops and native clio-core
   calls), bytes put, folds run;
3. **cold first read** -- a new ``ARCMemory`` over the same daemon/namespace (a restart:
   no hot copy, no lane cache): ms, RPCs, bytes read;
4. **Codex-direct request build per step** -- the engine's own work per step over the
   folded messages (``prepare_request`` and ``continuation``; no network, no LM call),
   counting every ``wire.build_request``;
5. **after the compaction** -- whether the next read still scans pre-compaction atoms.

The workload is the shape ClioReAct writes: per forward ``carry_over`` / ``injections``
/ ``user_message``; per step ``step_open`` then ``step_done`` (1 thought + k tool calls
+ k observations); a subagent forward (its own scope and expert span) inside every main
turn; one compaction of the main scope (``summarize_segments`` over its live working
set, as ``gact.compaction._fold_scopes`` does) once half of the atoms are written.

Measurement hooks (all in this script, none in ``src/``): instance-level wrappers on
the ``ClioCoreStore`` (ops + bytes), counting proxies over its native client/CTE
handles (native RPCs), a wrapper on ``ARCMemory.append_segment`` (per-atom samples) and
on ``fold_atoms`` (atoms folded per read; a warm read of the view folds none).

The daemon is private (``tests._cte_isolation``: own port block, own storage under
``--root``, own shm namespace); it is stopped and its root deleted at the end. Each
size runs in its own ARC namespace (its own CTE tag), so a cold scan of one size never
lists another size's blobs.

Usage::

    uv run python scripts/bench_context_view.py --sizes 100,1000,10000 --out out.json
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import gc
import json
import math
import os
import random
import shutil
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # tests._cte_isolation is the private-daemon helper

import dspy
from dspy.lm15 import (
    CacheConfig,
    ContinuationState,
    Message,
    OpenAICodexLM,
    Request,
    ThinkingPart,
    ToolCallPart,
)

from clio_agent.arc import context_view as arc_context_view
from clio_agent.arc import storage as arc_storage
from clio_agent.arc import working_set_fold as arc_fold
from clio_agent.arc.init_degradation import ArcStoreUnavailableError
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import ClioCoreStore, make_arc_store
from clio_agent.arc.working_set_fold import FoldingSegmentStore
from clio_agent.gact.agents import clio_react as react
from clio_agent.gact.agents import clio_react_record as record
from clio_agent.lm.request_config import config_from_lm_kwargs
from clio_agent.providers.codex import constants as codex_constants
from clio_agent.providers.codex import direct_engine
from clio_agent.providers.codex.direct_engine import (
    _with_reasoning_summary,
    continuation,
    prepare_request,
)
from tests._cte_isolation import (
    CteIsolation,
    cte_isolation_available,
    eagerly_attach_private_daemon,
    isolate_cte_env,
    reap_private_daemon,
    remove_private_cte_root,
)
from tests._process_hygiene import release_this_process_client

# Ports other agents on this machine own; the private block must never be one of them.
_FORBIDDEN_PORTS = frozenset({17991, 17996})
_MAIN_SCOPE = "main"
_SUB_SCOPE = "researcher"
_CODEX_MODEL = "gpt-5.5"
_NATIVE_CLIENT_RPCS = frozenset({"AsyncPutBlob", "AsyncDelBlob", "AsyncTagQuery"})
_NATIVE_TAG_RPCS = frozenset({"GetBlobSize", "GetBlob", "GetContainedBlobs", "PutBlob"})


# --------------------------------------------------------------------------- #
# Typed failures                                                              #
# --------------------------------------------------------------------------- #
class BenchError(Exception):
    """A benchmark run that cannot produce trustworthy numbers."""


class PrivateDaemonError(BenchError):
    """The private clio-core daemon did not come up, or is not the private one."""


class BenchInvariantError(BenchError):
    """A measured path disagreed with itself (e.g. cold read != warm read)."""


class CleanupError(BenchError):
    """The private daemon root could not be removed after the run."""


# --------------------------------------------------------------------------- #
# Measurement hooks (script-only)                                             #
# --------------------------------------------------------------------------- #
@dataclass
class RpcCounters:
    """Store traffic seen through the instrumented ``ClioCoreStore``."""

    ops: Counter[str] = field(default_factory=Counter)
    native: Counter[str] = field(default_factory=Counter)
    raw_bytes_put: int = 0  # record bytes handed to ``put`` (pre-base64)
    wire_bytes_put: int = 0  # base64 payload + search companion text actually sent
    bytes_got: int = 0  # decoded bytes returned by ``get``

    def snapshot(self) -> RpcSnapshot:
        """Freeze the counters for a later :meth:`RpcSnapshot.delta`."""
        return RpcSnapshot(
            ops=sum(self.ops.values()),
            native=sum(self.native.values()),
            puts=self.ops["put"],
            gets=self.ops["get"],
            scans=self.ops["scan"],
            raw_bytes_put=self.raw_bytes_put,
            wire_bytes_put=self.wire_bytes_put,
            bytes_got=self.bytes_got,
            native_by_name=dict(self.native),
        )


@dataclass(frozen=True)
class RpcSnapshot:
    """A point-in-time copy of :class:`RpcCounters`."""

    ops: int
    native: int
    puts: int
    gets: int
    scans: int
    raw_bytes_put: int
    wire_bytes_put: int
    bytes_got: int
    native_by_name: dict[str, int]

    def delta(self, later: RpcSnapshot) -> dict[str, Any]:
        """Traffic between this snapshot and ``later``."""
        names = set(self.native_by_name) | set(later.native_by_name)
        return {
            "ops": later.ops - self.ops,
            "native_rpcs": later.native - self.native,
            "puts": later.puts - self.puts,
            "gets": later.gets - self.gets,
            "scans": later.scans - self.scans,
            "raw_bytes_put": later.raw_bytes_put - self.raw_bytes_put,
            "wire_bytes_put": later.wire_bytes_put - self.wire_bytes_put,
            "bytes_got": later.bytes_got - self.bytes_got,
            "native_by_name": {
                n: later.native_by_name.get(n, 0) - self.native_by_name.get(n, 0)
                for n in sorted(names)
                if later.native_by_name.get(n, 0) != self.native_by_name.get(n, 0)
            },
        }


class _CountingProxy:
    """Forwards every attribute to ``target``; counts calls of the named native RPCs.

    ``Tag(...)`` on the CTE module returns a proxy too, so ``GetBlobSize`` / ``GetBlob``
    / ``GetContainedBlobs`` on a tag handle are counted.
    """

    def __init__(self, target: Any, counter: Counter[str], names: frozenset[str]) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_counter", counter)
        object.__setattr__(self, "_names", names)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if name == "Tag" and callable(attr):
            counter = self._counter

            def tag(*args: Any, **kwargs: Any) -> _CountingProxy:
                return _CountingProxy(attr(*args, **kwargs), counter, _NATIVE_TAG_RPCS)

            return tag
        if name in self._names and callable(attr):
            counter = self._counter

            def counted(*args: Any, **kwargs: Any) -> Any:
                counter[name] += 1
                return attr(*args, **kwargs)

            return counted
        return attr


def instrument_store(store: ClioCoreStore, counters: RpcCounters) -> None:
    """Count ``store``'s ARCStore ops (``put_many`` per record), bytes and native RPCs."""
    orig_put = store.put
    orig_get = store.get
    orig_scan = store.scan
    orig_delete = store.delete

    def put(
        kind: str, name: str, data: bytes, *, tier: str = "warm", search_text: str | None = None
    ) -> None:
        counters.ops["put"] += 1
        counters.raw_bytes_put += len(data)
        counters.wire_bytes_put += 4 * math.ceil(len(data) / 3)
        if search_text is not None:
            counters.wire_bytes_put += len(search_text.encode("utf-8"))
        orig_put(kind, name, data, tier=tier, search_text=search_text)

    orig_put_many = store.put_many

    def put_many(kind: str, records: Any) -> None:
        for rec in records:
            counters.ops["put"] += 1
            counters.raw_bytes_put += len(rec.data)
            counters.wire_bytes_put += 4 * math.ceil(len(rec.data) / 3)
            if rec.search_text is not None:
                counters.wire_bytes_put += len(rec.search_text.encode("utf-8"))
        orig_put_many(kind, records)

    def get(kind: str, name: str) -> bytes | None:
        counters.ops["get"] += 1
        value = orig_get(kind, name)
        counters.bytes_got += len(value) if value else 0
        return value

    def scan(kind: str, prefix: str = "") -> Any:
        counters.ops["scan"] += 1
        return orig_scan(kind, prefix)

    def delete(kind: str, name: str) -> None:
        counters.ops["delete"] += 1
        orig_delete(kind, name)

    store.put = put  # type: ignore[method-assign]
    store.put_many = put_many  # type: ignore[method-assign]
    store.get = get  # type: ignore[method-assign]
    store.scan = scan  # type: ignore[method-assign]
    store.delete = delete  # type: ignore[method-assign]
    store._client = _CountingProxy(store._client, counters.native, _NATIVE_CLIENT_RPCS)
    store._cte = _CountingProxy(store._cte, counters.native, frozenset())


@dataclass
class LaneProbe:
    """Atoms folded: one entry per ``fold_atoms`` call since :meth:`reset`."""

    calls: int = 0
    atoms: int = 0
    last: list[Any] = field(default_factory=list)

    def reset(self) -> None:
        """Start a new observation window."""
        self.calls = 0
        self.atoms = 0
        self.last = []


_PROBES: list[LaneProbe] = []


def instrument_fold(arc: ARCMemory, probe: LaneProbe) -> None:
    """Record every ``fold_atoms`` input (the fold's atoms) into ``probe``.

    The fold is one module function used by the view (rebuilds) and the store (as-of /
    history reads); both names are wrapped once per process, and each probe sees every
    fold while it is the active one.
    """
    segments = arc._segments
    if not isinstance(segments, FoldingSegmentStore):
        raise BenchInvariantError(
            f"the live plane is {type(segments).__name__}, not the production fold"
        )
    if not _PROBES:
        orig = arc_context_view.fold_atoms

        def fold_atoms(atoms: Any, scope: str, **kw: Any) -> list[Any]:
            out = orig(atoms, scope, **kw)
            for active in _PROBES[-1:]:
                active.calls += 1
                active.atoms += len(atoms)
                active.last = list(atoms)
            return out

        arc_context_view.fold_atoms = fold_atoms  # type: ignore[assignment]
        arc_fold.fold_atoms = fold_atoms  # type: ignore[assignment]
    _PROBES.append(probe)


@dataclass(frozen=True)
class AppendSample:
    """One ``ARCMemory.append_segment`` as the recorder issued it."""

    scope: str
    kind: str
    ms: float
    folds: int
    atoms_touched: int
    rpc: dict[str, Any]


def instrument_appends(
    arc: ARCMemory, counters: RpcCounters, probe: LaneProbe, out: list[AppendSample]
) -> None:
    """Sample every ``append_segment`` (the recorder's one write call)."""
    orig = arc.append_segment

    def append_segment(
        session_id: str, scope: str, kind: str, content: dict[str, Any], **kw: Any
    ) -> Any:
        before = counters.snapshot()
        probe.reset()
        t0 = time.perf_counter()
        seg = orig(session_id, scope, kind, content, **kw)
        ms = (time.perf_counter() - t0) * 1e3
        out.append(
            AppendSample(
                scope=scope,
                kind=kind,
                ms=ms,
                folds=probe.calls,
                atoms_touched=probe.atoms,
                rpc=before.delta(counters.snapshot()),
            )
        )
        return seg

    arc.append_segment = append_segment  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# Workload                                                                    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Shape:
    """The synthetic conversation shape (sizes in characters)."""

    steps_per_turn: int
    calls_per_step: int
    sub_steps: int
    obs_chars: int
    thought_chars: int
    reasoning_chars: int
    seed: int


def read_file(path: str) -> str:
    """Read a file from the workspace."""
    return path


def grep(pattern: str, path: str) -> str:
    """Search files for a pattern."""
    return pattern + path


@dataclass
class Forward:
    """One expert forward in flight: its scope, recorder and Codex conversation state."""

    scope: str
    recorder: record.StepRecorder
    held: Any = None  # the engine's kept-conversation record after the last call
    step: int = 0


@dataclass
class StepRow:
    """Per-step measurements (warm read + Codex build)."""

    scope: str
    session_atoms: int
    render_ms: float
    fold_ms: float
    read_rpc: int
    atoms_touched: int
    live_segments: int
    messages: int
    codex_ms: float
    codex_builds: int
    codex_delta: bool
    codex_input_items: int
    codex_frame_items: int
    step_open_ms: float


class Bench:
    """Drives one size N through the real recording/reading path and collects samples."""

    def __init__(
        self,
        *,
        n_atoms: int,
        shape: Shape,
        store: ClioCoreStore,
        counters: RpcCounters,
        data_dir: Path,
    ) -> None:
        self.n_atoms = n_atoms
        self.shape = shape
        self.store = store
        self.counters = counters
        self.data_dir = data_dir
        self.rng = random.Random(shape.seed)
        self.session = f"bench-{uuid.uuid4().hex[:12]}"
        self.probe = LaneProbe()
        self.appends: list[AppendSample] = []
        self.rows: list[StepRow] = []
        self.arc = ARCMemory(data_dir=str(data_dir / "warm"), store=store)
        instrument_fold(self.arc, self.probe)
        instrument_appends(self.arc, counters, self.probe, self.appends)
        agent = react.ClioReAct("question -> answer", tools=[read_file, grep])
        self.signature = agent.signature
        self.tools = tuple(react._function_tool(t) for t in agent.tools.values())
        self.wire = OpenAICodexLM(
            api_key="bench-no-network",
            account_id="bench",
            originator=codex_constants.ORIGINATOR,
        )
        self.builds = [0]
        real_build = self.wire.build_request

        def counted_build(request: Request, stream: bool) -> Any:
            self.builds[0] += 1
            return real_build(request, stream)

        self.wire.build_request = counted_build  # type: ignore[method-assign]
        self.system_prompt = self._text(4000)
        self.compaction: dict[str, Any] = {}
        self.compacted = False
        self.post_compaction_read: dict[str, Any] = {}
        self.turns = 0

    # ---- content -----------------------------------------------------------

    def _text(self, chars: int) -> str:
        words = ("the", "frame", "column", "value", "result", "query", "index", "file")
        out: list[str] = []
        size = 0
        while size < chars:
            word = self.rng.choice(words) + str(self.rng.randint(0, 999))
            out.append(word)
            size += len(word) + 1
        return " ".join(out)[:chars]

    def content_atoms(self) -> int:
        """Content atoms (user/thought/tool_call/observation/summary) written so far."""
        return sum(1 for a in self.appends if a.kind != "step_open")

    # ---- forwards ------------------------------------------------------------

    def open_forward(self, scope: str, question: str) -> Forward:
        """What ``_Loop.run`` does before its first step (minus the LM)."""
        inputs = {"question": question, "system_prompt": self.system_prompt}
        recorder = record.StepRecorder(self.arc, self.session, scope, expert_id=scope)
        recorder.started(uuid.uuid4().hex[:16], inputs)
        recorder.carry_over([])
        recorder.injections([("tool_use", react.TOOL_USE_NOTE)])
        recorder.user_message(react._head(self.signature, inputs))
        return Forward(scope=scope, recorder=recorder)

    def run_step(self, fwd: Forward) -> None:
        """One ReAct step: read the context, build the Codex request, record the reply."""
        inputs = {"question": "", "system_prompt": self.system_prompt}
        system = react._system(self.signature, inputs)
        session_atoms = self.content_atoms()

        before = self.counters.snapshot()
        self.probe.reset()
        t0 = time.perf_counter()
        segments = self.arc.context_view(self.session, fwd.scope).segments
        t1 = time.perf_counter()
        messages = fwd.recorder.read_steps()
        t2 = time.perf_counter()
        read_rpc = before.delta(self.counters.snapshot())["native_rpcs"]
        atoms_touched = self.probe.atoms
        lane_last = self.probe.last

        codex = self._codex_build(fwd, messages, system)

        if self.compacted and not self.post_compaction_read and fwd.scope == _MAIN_SCOPE:
            summary_lt = self.compaction["summary_logical_time"]
            self.post_compaction_read = {
                "atoms_touched": atoms_touched,
                "pre_compaction_atoms_scanned": sum(
                    1 for a in lane_last if a.logical_time < summary_lt
                ),
                "live_segments": len(segments),
                "messages": len(messages),
                "render_ms": (t1 - t0) * 1e3,
                "fold_ms": (t2 - t1) * 1e3,
                "codex_delta": codex["delta"],
            }

        span = uuid.uuid4().hex[:16]
        text = self._text(self.shape.thought_chars)
        thinking = [
            ThinkingPart(
                text=self._text(200),
                continuation=(
                    ContinuationState(
                        provider="openai",
                        kind="reasoning_item",
                        data={
                            "id": f"rs_{uuid.uuid4().hex}",
                            "encrypted_content": self._text(self.shape.reasoning_chars),
                        },
                    ),
                ),
            )
        ]
        calls = [
            ToolCallPart(
                id=f"call_{uuid.uuid4().hex[:12]}",
                name="read_file" if i % 2 == 0 else "grep",
                input={"path": f"src/mod_{fwd.step}_{i}.py"}
                if i % 2 == 0
                else {"pattern": "def ", "path": "src/"},
            )
            for i in range(self.shape.calls_per_step)
        ]
        t3 = time.perf_counter()
        fwd.recorder.step_open(fwd.step, span, text, calls)
        step_open_ms = (time.perf_counter() - t3) * 1e3
        results = {c.id: (self._text(self.shape.obs_chars), False) for c in calls}
        fwd.recorder.step_done(
            fwd.step, span, text=text, thinking=thinking, calls=calls, results=results
        )
        fwd.step += 1
        self.rows.append(
            StepRow(
                scope=fwd.scope,
                session_atoms=session_atoms,
                render_ms=(t1 - t0) * 1e3,
                fold_ms=(t2 - t1) * 1e3,
                read_rpc=read_rpc,
                atoms_touched=atoms_touched,
                live_segments=len(segments),
                messages=len(messages),
                codex_ms=codex["ms"],
                codex_builds=codex["builds"],
                codex_delta=codex["delta"],
                codex_input_items=codex["input_items"],
                codex_frame_items=codex["frame_items"],
                step_open_ms=step_open_ms,
            )
        )

    def _codex_build(self, fwd: Forward, messages: list[Message], system: str) -> dict[str, Any]:
        """The Codex-direct engine's per-step request work, minus the socket.

        ``_Loop._one_step`` builds the Request; ``AsyncCodexDirectEngine._call/_run``
        adds the reasoning summary, builds the wire request once (``prepare_request``)
        and checks the kept conversation (``continuation``: item count + running hash).
        Every ``wire.build_request`` is counted.
        """
        config = config_from_lm_kwargs({})
        config = dataclasses.replace(
            config,
            cache=dataclasses.replace(
                config.cache or CacheConfig(), key=f"clio:{self.session}:{fwd.scope}"
            ),
        )
        builds_before = self.builds[0]
        t0 = time.perf_counter()
        request = Request(
            model=_CODEX_MODEL,
            system=system,
            messages=tuple(react._place_tool_media(messages, "native")),
            tools=self.tools,
            config=config,
        )
        request = _with_reasoning_summary(request)
        prepared = prepare_request(self.wire, request)
        delta = False
        frame_items = len(prepared.items)
        if fwd.held is not None:
            first_new = continuation(fwd.held, request, prepared)
            if first_new is not None:
                delta = True
                frame_items = len(prepared.items) - first_new
        held = direct_engine._Conversation(
            socket=None,
            system=prepared.system,
            tools=prepared.tools,
            held=len(request.messages),
            sent_items=len(prepared.items),
            digest=prepared.digest_at(len(prepared.items)),
            response_id="bench",
        )
        ms = (time.perf_counter() - t0) * 1e3
        fwd.held = held
        return {
            "ms": ms,
            "builds": self.builds[0] - builds_before,
            "delta": delta,
            "input_items": len(prepared.items),
            "frame_items": frame_items,
        }

    def compact(self) -> None:
        """``gact.compaction._fold_scopes`` on the main scope (fixed summary text, no LM)."""
        live = self.arc.render_working_set(self.session, _MAIN_SCOPE)
        self.probe.reset()
        segments = self.arc._segments
        if not isinstance(segments, FoldingSegmentStore):
            raise BenchInvariantError("the live plane is not the production fold")
        lane_before = len(segments.raw_lane_atoms(self.session))
        summary = self._text(2000)
        before = self.counters.snapshot()
        self.probe.reset()
        t0 = time.perf_counter()
        seg = self.arc.summarize_segments(
            self.session,
            _MAIN_SCOPE,
            [s.id for s in live],
            {"text": summary},
            token_count=max(1, len(summary) // 4),
        )
        ms = (time.perf_counter() - t0) * 1e3
        self.compaction = {
            "at_content_atoms": self.content_atoms(),
            "summarized_segments": len(live),
            "lane_atoms_before": lane_before,
            "summarize_ms": ms,
            "folds": self.probe.calls,
            "rpc": before.delta(self.counters.snapshot()),
            "summary_logical_time": int(seg.logical_time),
        }
        self.compacted = True

    def run(self) -> None:
        """Write and read until ``n_atoms`` content atoms are on the plane."""
        turn = 0
        while self.content_atoms() < self.n_atoms:
            main = self.open_forward(_MAIN_SCOPE, f"turn {turn}: " + self._text(300))
            for i in range(self.shape.steps_per_turn):
                if self.content_atoms() >= self.n_atoms:
                    break
                if not self.compacted and self.content_atoms() >= self.n_atoms // 2:
                    self.compact()
                if i == self.shape.steps_per_turn // 2 and self.shape.sub_steps > 0:
                    sub = self.open_forward(_SUB_SCOPE, "delegated: " + self._text(300))
                    for _ in range(self.shape.sub_steps):
                        if self.content_atoms() >= self.n_atoms:
                            break
                        self.run_step(sub)
                    if self.content_atoms() >= self.n_atoms:
                        break
                self.run_step(main)
            turn += 1
        self.turns = turn
        if not self.compacted:
            raise BenchInvariantError("the run ended before the midway compaction")
        if not self.post_compaction_read:
            raise BenchInvariantError("no main-scope read happened after the compaction")

    # ---- cold read -------------------------------------------------------------

    def cold_read(self) -> dict[str, Any]:
        """A restart: a new ARCMemory over the same daemon/namespace reads main first."""
        warm = record.fold_steps(self.arc.render_segments(self.session, _MAIN_SCOPE))
        t0 = time.perf_counter()
        cold_arc = ARCMemory(data_dir=str(self.data_dir / "cold"), store=self.store)
        init_ms = (time.perf_counter() - t0) * 1e3
        probe = LaneProbe()
        instrument_fold(cold_arc, probe)
        before = self.counters.snapshot()
        t1 = time.perf_counter()
        segments = cold_arc.render_segments(self.session, _MAIN_SCOPE)
        t2 = time.perf_counter()
        messages = record.fold_steps(segments)
        t3 = time.perf_counter()
        rpc = before.delta(self.counters.snapshot())
        cold_atoms_touched = probe.atoms
        if messages != warm:
            raise BenchInvariantError(
                f"cold read folded {len(messages)} messages, warm read {len(warm)}"
            )
        before2 = self.counters.snapshot()
        t4 = time.perf_counter()
        record.fold_steps(cold_arc.render_segments(self.session, _MAIN_SCOPE))
        second_ms = (time.perf_counter() - t4) * 1e3
        second_rpc = before2.delta(self.counters.snapshot())["native_rpcs"]
        del cold_arc
        gc.collect()
        return {
            "arc_init_ms": init_ms,
            "render_ms": (t2 - t1) * 1e3,
            "fold_ms": (t3 - t2) * 1e3,
            "total_ms": (t3 - t1) * 1e3,
            "atoms_touched": cold_atoms_touched,
            "rpc": rpc,
            "second_read_ms": second_ms,
            "second_read_native_rpcs": second_rpc,
        }

    def close(self) -> None:
        """Drop the warm ARCMemory and free this size's blobs on the daemon."""
        del self.arc
        gc.collect()
        self.store.clear()


# --------------------------------------------------------------------------- #
# Summaries                                                                   #
# --------------------------------------------------------------------------- #
def pct(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..1); 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))])


def dist(values: Sequence[float]) -> dict[str, float]:
    """p50/p90/max/mean of ``values``."""
    return {
        "p50": pct(values, 0.5),
        "p90": pct(values, 0.9),
        "max": max(values) if values else 0.0,
        "mean": statistics.fmean(values) if values else 0.0,
    }


def summarize(bench: Bench, cold: dict[str, Any], wall_s: float) -> dict[str, Any]:
    """The JSON record for one size."""
    rows = bench.rows
    content = [a for a in bench.appends if a.kind != "step_open"]
    tail = content[-max(1, len(content) // 10) :]

    def append_stats(samples: Sequence[AppendSample]) -> dict[str, Any]:
        return {
            "count": len(samples),
            "ms": dist([s.ms for s in samples]),
            "ops_per_atom": statistics.fmean(s.rpc["ops"] for s in samples),
            "native_rpcs_per_atom": statistics.fmean(s.rpc["native_rpcs"] for s in samples),
            "puts_per_atom": statistics.fmean(s.rpc["puts"] for s in samples),
            "wire_bytes_put_per_atom": statistics.fmean(s.rpc["wire_bytes_put"] for s in samples),
            "raw_bytes_put_per_atom": statistics.fmean(s.rpc["raw_bytes_put"] for s in samples),
            "folds_per_atom": statistics.fmean(s.folds for s in samples),
            "atoms_touched_per_atom": statistics.fmean(s.atoms_touched for s in samples),
        }

    by_scope: dict[str, Any] = {}
    for scope in (_MAIN_SCOPE, _SUB_SCOPE):
        scoped = [r for r in rows if r.scope == scope]
        if scoped:
            by_scope[scope] = {
                "steps": len(scoped),
                "build_ms": dist([r.render_ms + r.fold_ms for r in scoped]),
                "atoms_touched_last": scoped[-1].atoms_touched,
                "live_segments_last": scoped[-1].live_segments,
            }
    return {
        "n_target": bench.n_atoms,
        "content_atoms": len(content),
        "step_open_records": len(rows),
        "turns": bench.turns,
        "steps": len(rows),
        "wall_s": wall_s,
        "context_build": {
            "ms": dist([r.render_ms + r.fold_ms for r in rows]),
            "render_ms": dist([r.render_ms for r in rows]),
            "fold_ms": dist([r.fold_ms for r in rows]),
            "native_rpcs_per_read": statistics.fmean(r.read_rpc for r in rows),
            "atoms_touched": dist([float(r.atoms_touched) for r in rows]),
            "by_scope": by_scope,
        },
        "append": {
            "all": append_stats(content),
            "last_decile": append_stats(tail),
            "step_open_ms": dist([r.step_open_ms for r in rows]),
        },
        "cold_read": cold,
        "codex_build": {
            "ms": dist([r.codex_ms for r in rows]),
            "builds_per_step": statistics.fmean(r.codex_builds for r in rows),
            "delta_steps": sum(1 for r in rows if r.codex_delta),
            "full_steps": sum(1 for r in rows if not r.codex_delta),
            "input_items_max": max(r.codex_input_items for r in rows),
        },
        "compaction": {**bench.compaction, "next_main_read": bench.post_compaction_read},
        "series": [dataclasses.asdict(r) for r in rows],
    }


def markdown(results: Sequence[dict[str, Any]]) -> str:
    """A short table of the headline numbers."""
    head = (
        "| N | steps | build p50/p90 ms | atoms touched/read (max) | append p50/p90 ms "
        "| RPC/atom | KB put/atom | cold ms (RPC) | codex p50/p90 ms (builds) "
        "| post-compaction pre-atoms scanned |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    lines = [head]
    for r in results:
        cb, ap, cold, cx, comp = (
            r["context_build"],
            r["append"]["all"],
            r["cold_read"],
            r["codex_build"],
            r["compaction"]["next_main_read"],
        )
        lines.append(
            f"| {r['content_atoms']} | {r['steps']} "
            f"| {cb['ms']['p50']:.1f}/{cb['ms']['p90']:.1f} "
            f"| {cb['atoms_touched']['max']:.0f} "
            f"| {ap['ms']['p50']:.1f}/{ap['ms']['p90']:.1f} "
            f"| {ap['native_rpcs_per_atom']:.1f} "
            f"| {ap['wire_bytes_put_per_atom'] / 1024:.1f} "
            f"| {cold['total_ms']:.1f} ({cold['rpc']['native_rpcs']}) "
            f"| {cx['ms']['p50']:.1f}/{cx['ms']['p90']:.1f} ({cx['builds_per_step']:.1f}) "
            f"| {comp['pre_compaction_atoms_scanned']}/{comp['atoms_touched']} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Private daemon                                                              #
# --------------------------------------------------------------------------- #
def start_private_daemon(base: Path) -> CteIsolation:
    """Write the private config under ``base`` and attach a fresh private daemon."""
    if not cte_isolation_available():
        raise PrivateDaemonError("the clio-core binding or its launcher is not installed")
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    root = base / f"clio-agent-cte-{os.getpid()}-bench-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    isolation = isolate_cte_env(root, os.environ)
    if any(isolation.port + off in _FORBIDDEN_PORTS for off in range(5)):
        raise PrivateDaemonError(f"reserved port block {isolation.port} overlaps 17991/17996")
    try:
        attached = eagerly_attach_private_daemon()
    except ArcStoreUnavailableError as exc:
        raise PrivateDaemonError(f"private clio-core daemon did not come up: {exc}") from exc
    if not attached:
        raise PrivateDaemonError("private clio-core daemon did not come up")
    if Path(arc_storage._active_config_path) != isolation.config_path:
        raise PrivateDaemonError(
            f"attached to {arc_storage._active_config_path!r}, not the private config "
            f"{isolation.config_path}"
        )
    return isolation


def stop_private_daemon(isolation: CteIsolation, base: Path) -> None:
    """Release this client, reap the daemon, delete its root (and ``base`` if empty)."""
    release_this_process_client()
    reap_private_daemon(isolation.state_dir)
    try:
        remove_private_cte_root(isolation.root, attempts=50, retry_delay_seconds=0.2)
    except RuntimeError as exc:
        raise CleanupError(str(exc)) from exc
    if base.is_dir() and not any(base.iterdir()):
        shutil.rmtree(base)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the command line."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    p.add_argument("--sizes", default="100,1000,10000", help="comma-separated atom counts")
    p.add_argument("--out", required=True, type=Path, help="JSON results path")
    p.add_argument("--root", default=Path("D:/t/bench-cv"), type=Path, help="daemon/data root")
    p.add_argument("--steps-per-turn", type=int, default=20)
    p.add_argument("--calls-per-step", type=int, default=2)
    p.add_argument("--sub-steps", type=int, default=5, help="subagent steps per main turn")
    p.add_argument("--obs-chars", type=int, default=1500)
    p.add_argument("--thought-chars", type=int, default=300)
    p.add_argument("--reasoning-chars", type=int, default=1200)
    p.add_argument("--file-tier", default="512MB", help="private daemon file-tier capacity")
    p.add_argument("--seed", type=int, default=7)
    return p.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """Run every size on one private daemon; write JSON; print the table."""
    args = parse_args(argv)
    sizes = [int(s) for s in str(args.sizes).split(",") if s.strip()]
    if not sizes or any(n < 20 for n in sizes):
        raise SystemExit("--sizes must be integers >= 20")
    shape = Shape(
        steps_per_turn=args.steps_per_turn,
        calls_per_step=args.calls_per_step,
        sub_steps=args.sub_steps,
        obs_chars=args.obs_chars,
        thought_chars=args.thought_chars,
        reasoning_chars=args.reasoning_chars,
        seed=args.seed,
    )
    os.environ["CLIO_TEST_CTE_FILE_TIER_CAPACITY"] = args.file_tier
    base: Path = args.root
    isolation = start_private_daemon(base)
    print(f"[bench-cv] private daemon port={isolation.port} root={isolation.root}", flush=True)
    results: list[dict[str, Any]] = []
    try:
        for n in sizes:
            store = make_arc_store(backend="cte", namespace=f"bench-cv-n{n}")
            if not isinstance(store, ClioCoreStore):
                raise PrivateDaemonError(f"ARC store is {type(store).__name__}, not clio-core")
            counters = RpcCounters()
            instrument_store(store, counters)
            data_dir = isolation.root / f"arc-n{n}"
            t0 = time.perf_counter()
            bench = Bench(n_atoms=n, shape=shape, store=store, counters=counters, data_dir=data_dir)
            bench.run()
            cold = bench.cold_read()
            result = summarize(bench, cold, time.perf_counter() - t0)
            result["shape"] = dataclasses.asdict(shape)
            result["native_rpcs_total"] = dict(counters.native)
            bench.close()
            results.append(result)
            print(
                f"[bench-cv] N={n}: {result['content_atoms']} atoms, {result['steps']} steps, "
                f"{result['wall_s']:.1f}s",
                flush=True,
            )
    finally:
        stop_private_daemon(isolation, base)
    payload = {
        "bench": "context_view_phase11a",
        "created": _dt.datetime.now(tz=_dt.UTC).isoformat(),
        "python": sys.version.split()[0],
        "dspy": getattr(dspy, "__version__", ""),
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(markdown(results))
    print(f"[bench-cv] wrote {args.out}")
    return 0


def _entry() -> int:
    try:
        return main(sys.argv[1:])
    except BenchError as exc:
        print(f"FATAL ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_entry())
