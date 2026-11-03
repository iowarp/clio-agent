"""One evaluation run against a GACT server, from a fresh workdir to a result JSON.

REST sequence (all ``/v1``):

1. ``POST /workspaces`` ``{name, root_path}`` with a fresh copy of the task's
   fixture folder (a run may edit files, so runs never share one).
2. ``POST /sessions`` ``{workspace_id, title, model, approval_mode, metadata:
   {agent_ref, agent_delta, ...}}``; ``agent_delta.add`` names the task's
   ``clio-eval`` plugins.
3. ``GET /fleet`` (before), then ``POST /sessions/{sid}/messages`` with the
   plain prompt.
4. Poll ``GET /sessions/{sid}`` every ``poll_s`` seconds. When the status is
   no longer ``running``/``stopping`` and the newest assistant message has a
   ``stop_reason``, wait ``settle_s`` and check again; the turn has ended when
   nothing changed (a goal or stop hook may start another turn in between).
   ``waiting_permission``/``waiting_user`` longer than ``interaction_timeout_s``,
   or the overall ``timeout_s``, cancels the session (``POST .../cancel``). A
   permission question raised while the turn runs (a child's does not change the
   root's status) is declined after ``decline_questions_after_s``, as a person who
   says no would; each is recorded in the result's ``declined_permissions``.
5. ``GET /fleet`` (after) and ``GET /sessions/{sid}/export`` (messages, full
   semantic events and every descendant session).
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling import collect, tasks
from scripts.eval.tool_calling.http_client import GactClient
from scripts.eval.tool_calling.usage_guard import window_deltas

RESULT_SCHEMA = "clio.eval.tool_calling.run.v1"
ACTIVE = frozenset({"running", "stopping"})
INTERACTION = frozenset({"waiting_permission", "waiting_user"})


@dataclass
class RunContext:
    """Settings shared by every run of one invocation."""

    client: GactClient
    fixtures: Path
    manifest: dict[str, Any]
    results: Path
    work_root: Path
    config: str
    provider: str = "claude_code"
    model: str = "claude-sonnet-5-5"
    agent_ref: str = tasks.STANDARD_AGENT
    approval_mode: str = "bypass"
    extra_metadata: dict[str, Any] = field(default_factory=dict)
    h3_autocompact_pct: float = tasks.H3_AUTOCOMPACT_PCT
    poll_s: float = 2.0
    settle_s: float = 6.0
    timeout_s: float = 3600.0
    interaction_timeout_s: float = 120.0
    decline_questions_after_s: float = 10.0
    stream_audit_log: Path | None = None
    call_log: Path | None = None
    shared_root: Path | None = None
    keep_export: bool = False
    fleet_settings: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunSpec:
    """One (task, run index) of a configuration."""

    task: str
    index: int


def result_path(results: Path, config: str, task: str, index: int) -> Path:
    return results / config / task / f"run-{index}.json"


def prepare_workdir(fixtures: Path, fixture: str, target: Path) -> Path:
    """A fresh copy of one fixture folder (git history included) at ``target``."""
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixtures / fixture, target, symlinks=True)
    return target


def reset_shared_root(root: Path, keep: Path | None) -> list[str]:
    """Empty ``root`` (the server's allowed root, shared by every run) before a run.

    Agents may write scratch files under every allowed root, and a later run found an
    earlier run's scratch copy of another repository there and reviewed it. Only the
    folder holding ``keep`` (the synthetic call log) stays. Returns what was removed.
    """
    kept = keep.resolve().relative_to(root.resolve()).parts[0] if keep else None
    removed = []
    for entry in sorted(root.iterdir()):
        if entry.name == kept:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed.append(entry.name)
    return removed


def ensure_workspace(client: GactClient, name: str, root: Path) -> dict[str, Any]:
    """The workspace pinned to ``root``: an existing one, else a new one."""
    wanted = str(root)
    for row in client.workspaces():
        if str(row.get("root_path") or "").rstrip("/") == wanted.rstrip("/"):
            return row
    return client.create_workspace(name, wanted)


def _newest_assistant(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [m for m in messages if m.get("role") == "assistant"]
    return max(rows, key=lambda m: str(m.get("created_at") or "")) if rows else None


def _decline_waiting(
    client: GactClient, seen: dict[str, float], now: float, after_s: float
) -> list[dict[str, Any]]:
    """Answer ``deny`` to each pending question that has waited ``after_s``."""
    declined = []
    for row in client.pending_permissions():
        pid = str(row.get("id") or "")
        if not pid:
            continue
        first = seen.setdefault(pid, now)
        if now - first < after_s:
            continue
        client.respond_permission(pid, "deny")
        seen.pop(pid, None)
        declined.append(
            {
                "id": pid,
                "session_id": row.get("session_id"),
                "tool": row.get("tool") or row.get("tool_name"),
                "waited_s": round(now - first, 1),
            }
        )
    return declined


def wait_for_turn(
    client: GactClient,
    sid: str,
    *,
    poll_s: float,
    settle_s: float,
    timeout_s: float,
    interaction_timeout_s: float,
    decline_after_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll until the session's work has ended; never busy-loops.

    Returns ``{"state": "settled" | "timed_out" | <interaction status>,
    "status", "polls", "first_done_wall", "declined"}``; ``first_done_wall`` is the
    wall time (epoch seconds) of the first poll that saw the end, before settling.
    With ``decline_after_s``, a pending permission question (runs are serial, so any
    is this run's, a child's included) is answered ``deny`` once it has waited that
    long; ``declined`` lists them.
    """
    start = clock()
    polls = 0
    marker: tuple[str, str, int] | None = None
    first_done_wall: float | None = None
    interaction_since: float | None = None
    status = ""
    seen: dict[str, float] = {}
    declined: list[dict[str, Any]] = []
    while True:
        if clock() - start > timeout_s:
            return {"state": "timed_out", "status": status, "polls": polls, "declined": declined}
        session = client.session(sid)
        polls += 1
        status = str(session.get("status") or "")
        if decline_after_s is not None and status in ACTIVE:
            declined += _decline_waiting(client, seen, clock(), decline_after_s)
        if status in ACTIVE:
            marker, first_done_wall, interaction_since = None, None, None
        elif status in INTERACTION:
            marker = first_done_wall = None
            interaction_since = interaction_since if interaction_since is not None else clock()
            if clock() - interaction_since > interaction_timeout_s:
                return {"state": status, "status": status, "polls": polls, "declined": declined}
        else:
            interaction_since = None
            newest = _newest_assistant(client.messages(sid, limit=5))
            if newest is not None and newest.get("stop_reason"):
                now = (
                    str(newest.get("id") or ""),
                    str(newest.get("updated_at") or ""),
                    int(session.get("message_count") or 0),
                )
                if marker == now:
                    return {
                        "state": "settled",
                        "status": status,
                        "polls": polls,
                        "first_done_wall": first_done_wall,
                        "declined": declined,
                    }
                marker, first_done_wall = now, time.time()
                sleep(settle_s)
                continue
        sleep(poll_s)


def read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    """Every JSON object line of ``path`` (empty when absent)."""
    if path is None or not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def slice_stream_audit(
    path: Path | None, start: float, end: float, session_ids: set[str]
) -> list[dict[str, Any]]:
    """``provider.stateful`` audit rows of this run's sessions.

    A row's ``conversation`` is ``<gact session>::<react scope>``, so rows are
    attributed to sessions exactly, also when runs overlap.
    """
    out = []
    for row in read_jsonl(path):
        if row.get("stage") != "provider.stateful":
            continue
        session = str(row.get("conversation") or "").split("::", 1)[0]
        ts = float(row.get("ts") or 0.0)
        if session in session_ids and start - 5 <= ts <= end + 5:
            out.append(row)
    return out


def summarize_io(
    path: Path | None, start: float, end: float, session_ids: set[str]
) -> dict[str, int]:
    """clio-core puts and bytes, and provider cache tokens, for this run's sessions.

    ``store.put`` rows name the record, which starts with or contains the session id;
    ``provider.call_usage`` rows carry the session id. Rows outside the run's window
    (with 5 s of slack) are ignored.
    """
    out = {
        "store_puts": 0,
        "store_put_bytes": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }
    for row in read_jsonl(path):
        ts = float(row.get("ts") or 0.0)
        if not start - 5 <= ts <= end + 5:
            continue
        stage = row.get("stage")
        if stage == "store.put":
            name = str(row.get("name") or "")
            if any(sid in name for sid in session_ids):
                out["store_puts"] += 1
                out["store_put_bytes"] += int(row.get("size") or 0)
        elif stage == "provider.call_usage" and row.get("session_id") in session_ids:
            out["cache_creation_tokens"] += int(row.get("usage_cache_creation_input_tokens") or 0)
            out["cache_read_tokens"] += int(row.get("usage_cache_read_input_tokens") or 0)
    return out


def slice_call_log(path: Path | None, start: float, end: float) -> list[dict[str, Any]]:
    """Synthetic-server calls in the run's time window (shared servers carry no session)."""
    return [r for r in read_jsonl(path) if start <= float(r.get("ts") or 0.0) <= end + 1]


def _activity_end(norm: dict[str, Any], messages: list[dict[str, Any]]) -> float | None:
    times = [
        t
        for s in norm["sessions"]
        for t in [
            *(c["ended_at"] for c in s["lm_calls"]),
            *(c["ended_at"] for c in s["tool_calls"]),
        ]
        if t
    ]
    times += [t for m in messages if (t := collect.epoch(m.get("updated_at")))]
    return max(times) if times else None


def _cancel_and_settle(ctx: RunContext, sid: str) -> None:
    try:
        ctx.client.cancel(sid)
    except Exception as exc:  # noqa: BLE001 - recorded, the run is already failed
        print(f"  cancel {sid}: {exc}")
    try:
        wait_for_turn(
            ctx.client,
            sid,
            poll_s=ctx.poll_s,
            settle_s=ctx.poll_s,
            timeout_s=120.0,
            interaction_timeout_s=10.0,
        )
    except Exception as exc:  # noqa: BLE001 - recorded, the run is already failed
        print(f"  settle {sid} after cancel: {exc}")


def execute_run(ctx: RunContext, spec: RunSpec) -> dict[str, Any]:
    """Run one task once and return its result (also written to disk)."""
    task = tasks.task(spec.task, ctx.manifest, h3_autocompact_pct=ctx.h3_autocompact_pct)
    if ctx.shared_root is not None:
        removed = reset_shared_root(ctx.shared_root, ctx.call_log)
        if removed:
            print(f"  emptied {ctx.shared_root}: {', '.join(removed)}")
    fixture = tasks.fixture_dir(spec.task, ctx.manifest)
    workdir = prepare_workdir(
        ctx.fixtures, fixture, ctx.work_root / ctx.config / spec.task / f"run-{spec.index}"
    )
    label = f"eval {ctx.config} {spec.task} run {spec.index}"
    workspace = ensure_workspace(ctx.client, label, workdir)
    metadata = {
        "agent_ref": ctx.agent_ref,
        "agent_delta": task.agent_delta(),
        **task.metadata,
        **ctx.extra_metadata,
    }
    body: dict[str, Any] = {
        "workspace_id": workspace["id"],
        "title": label,
        "approval_mode": ctx.approval_mode,
        "metadata": metadata,
    }
    if ctx.model:
        body["model"] = {"provider_id": ctx.provider, "model_id": ctx.model}
    session = ctx.client.create_session(body)
    sid = str(session["id"])
    fleet_before = ctx.client.fleet()
    started = time.time()
    ack = ctx.client.send(sid, task.prompt)
    try:
        waited = wait_for_turn(
            ctx.client,
            sid,
            poll_s=ctx.poll_s,
            settle_s=ctx.settle_s,
            timeout_s=ctx.timeout_s,
            interaction_timeout_s=ctx.interaction_timeout_s,
            decline_after_s=ctx.decline_questions_after_s,
        )
    except Exception:
        # A failed poll (the server stopped answering) must not leave the turn running
        # into the next run's measurements.
        _cancel_and_settle(ctx, sid)
        raise
    polled_end = waited.get("first_done_wall") or time.time()
    if waited["state"] != "settled":
        _cancel_and_settle(ctx, sid)
    fleet_after = ctx.client.fleet()
    export = ctx.client.export(sid)
    norm = collect.normalize_export(export)
    if waited["state"] != "settled":
        norm["outcome"] = {**norm["outcome"], "final": False, "reason": waited["state"]}
    # The last recorded activity is the precise end; a server clock behind ours
    # (a remote host) would put it before the start, so the poll time is used then.
    activity = _activity_end(norm, list(export.get("messages") or []))
    ended = (
        activity if activity is not None and started <= activity <= polled_end + 60 else polled_end
    )
    session_ids = {s["session_id"] for s in norm["sessions"]}
    result = {
        "schema": RESULT_SCHEMA,
        "source": "clio",
        "config": ctx.config,
        "task": spec.task,
        "run_index": spec.index,
        "prompt": task.prompt,
        "base_url": ctx.client.base_url,
        "session_id": sid,
        "handles": [s["session_id"] for s in norm["sessions"]],
        "workspace_id": workspace["id"],
        "workdir": str(workdir),
        "message_id": ack.get("message_id"),
        "agent": {"ref": ctx.agent_ref, "delta": task.agent_delta()},
        "model": body.get("model"),
        "approval_mode": ctx.approval_mode,
        "session_metadata": metadata,
        "started_at": started,
        "ended_at": ended,
        "wall_s": round(ended - started, 3),
        "wall_s_source": "activity" if ended is activity else "poll",
        "wall_s_polled": round(polled_end - started, 3),
        "wait": waited,
        "declined_permissions": waited.get("declined", []),
        "outcome": norm["outcome"],
        "sessions": norm["sessions"],
        "recording": {"root": norm["recording"], "children": norm["child_recordings"]},
        "fleet_before": fleet_before,
        "fleet_after": fleet_after,
        "usage_windows": window_deltas(fleet_before, fleet_after),
        "fleet_settings": ctx.fleet_settings,
        "stream_audit": slice_stream_audit(ctx.stream_audit_log, started, ended, session_ids),
        "stream_audit_enabled": ctx.stream_audit_log is not None,
        "io": summarize_io(ctx.stream_audit_log, started, ended, session_ids),
        "call_log": slice_call_log(ctx.call_log, started, ended),
    }
    target = result_path(ctx.results, ctx.config, spec.task, spec.index)
    write_json(target, result)
    if ctx.keep_export:
        write_json(target.with_suffix(".export.json"), export)
    return result


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (a crash never leaves half a result)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
