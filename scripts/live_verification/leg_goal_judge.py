"""Live leg: the GOAL judge runs off the sync path and the loop stays live (#1333).

Reproduces the failing session shape (``sess_4893357bfe95``, generation
20260905-112411-20732): a user-armed ``/goal`` with the ORIGINAL condition text, one
turn whose shell command satisfies it, then the finalize judge. Before the fix the judge
crashed on codex (``asyncio.run() cannot be called from a running event loop``) and
redrove already-successful work until the goal was abandoned; on every provider it froze
the server loop for the judge's duration.

Asserts, per run:

* the goal clears with ``clear_reason == "goal_met"`` and ``met is True`` within two
  goal iterations;
* no message in the ledger carries ``judge unavailable``;
* ``GET /v1/health`` keeps answering while the JUDGE runs: the max latency of a 250 ms
  probe inside the judge window stays under ``--max-health-latency`` seconds (loop
  liveness). The window is read off the server's own SSE audit log (the last
  ``message.completed`` before the goal cleared -> the judge's ``provider.batch_response``
  carrying ``[[ ## met ## ]]``), so the assertion is about the judge, not the turn;
* the whole-run max latency and every slow probe (>= 0.5 s, wallclock-stamped) are
  RECORDED as evidence, not asserted: stalls inside the turn (tool execution, ARC
  writes) are pre-existing loop-blocking work outside this leg's claim and get their
  own attribution;
* the SSE stream's max inter-event gap is RECORDED as evidence (the 15 s heartbeat).

Run under the private real-CTE daemon (a gate never holds ARC-local)::

    uv run python scripts/live_verification/run_with_private_cte.py \\
        scripts/live_verification/leg_goal_judge.py --provider codex --model gpt-5.6-luna
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    OUT_ROOT,
    allow_all,
    bind_provider,
    boot_server,
    client,
    create_session,
    create_workspace,
    dump_json,
    expanding_wait,
    post_message,
    session_messages,
    terminate_server,
    wait_health,
    wait_turn,
    write_verdict,
)

CONDITION = (
    "The verification command has printed work-live-start, work-live-stderr, and "
    "work-live-end and exited successfully."
)
TASK = (
    "Run ONE shell command that prints the line work-live-start to stdout, waits about "
    "5 seconds, writes the line work-live-stderr to stderr, prints the line work-live-end "
    "to stdout, and exits with code 0. Then report the exact stdout, stderr, and exit code "
    "you observed."
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _session_meta(call: Callable[..., Any], wsid: str, sid: str) -> dict[str, Any]:
    rows = call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
    row = next((r for r in rows if r.get("id") == sid), {}) or {}
    return row.get("metadata", {}) or {}


class _HealthProbe(threading.Thread):
    """Poll ``/v1/health`` every 250 ms; the max latency is the loop-liveness measure."""

    def __init__(self, base: str) -> None:
        super().__init__(name="health-probe", daemon=True)
        self._base = base
        self.stop = threading.Event()
        self.max_latency_s = 0.0
        self.samples = 0
        self.failures = 0
        #: every sample as ``(wallclock_start, latency_s)`` for window attribution.
        self.rows: list[tuple[float, float]] = []

    def run(self) -> None:
        import requests

        while not self.stop.is_set():
            wall = time.time()
            t0 = time.monotonic()
            try:
                requests.get(f"{self._base}/v1/health", timeout=10)
            except Exception:  # noqa: BLE001 - a failed probe is itself the finding
                self.failures += 1
            latency = time.monotonic() - t0
            self.rows.append((wall, latency))
            self.max_latency_s = max(self.max_latency_s, latency)
            self.samples += 1
            self.stop.wait(0.25)

    def max_in_window(self, start: float, end: float) -> tuple[float, int]:
        """Max latency of probes whose wallclock start lies in ``[start, end]``."""

        inside = [lat for wall, lat in self.rows if start <= wall <= end]
        return (max(inside) if inside else 0.0), len(inside)

    def slow(self, threshold_s: float = 0.5) -> list[dict[str, Any]]:
        return [
            {
                "at": _dt.datetime.fromtimestamp(wall, tz=_dt.UTC).isoformat(),
                "latency_s": round(lat, 3),
            }
            for wall, lat in self.rows
            if lat >= threshold_s
        ]


class _SseGapProbe(threading.Thread):
    """Read the session SSE stream; record the max gap between any two events."""

    def __init__(self, base: str, sid: str) -> None:
        super().__init__(name="sse-probe", daemon=True)
        self._url = f"{base}/v1/sessions/{sid}/events"
        self.stop = threading.Event()
        self.max_gap_s = 0.0
        self.events = 0
        self.error = ""

    def run(self) -> None:
        import requests

        last = time.monotonic()
        try:
            with requests.get(self._url, stream=True, timeout=(10, 60)) as r:
                for line in r.iter_lines():
                    if self.stop.is_set():
                        break
                    if not line or not line.startswith(b"event:"):
                        continue
                    now = time.monotonic()
                    self.max_gap_s = max(self.max_gap_s, now - last)
                    last = now
                    self.events += 1
        except Exception as exc:  # noqa: BLE001 - recorded, not asserted
            self.error = f"{type(exc).__name__}: {exc}"


def _judge_window(sse_log: Path) -> tuple[float, float] | None:
    """Read the judge window off the SSE audit log: ``(message.completed ts, judge ts)``.

    The judge's provider response is the ``provider.batch_response`` row whose head starts
    with the judge signature's first output field; the window opens at the last
    ``message.completed`` write before it. ``None`` when either stamp is missing."""

    if not sse_log.exists():
        return None
    completed_ts: float | None = None
    for line in sse_log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("stage") == "sse.write" and row.get("event_type") == "message.completed":
            completed_ts = float(row.get("ts") or 0.0)
        elif row.get("stage") == "provider.batch_response" and str(
            row.get("head") or ""
        ).lstrip().startswith("[[ ## met ## ]]"):
            if completed_ts is not None:
                return completed_ts, float(row.get("ts") or 0.0)
    return None


def run_leg(provider: str, model: str, max_health_latency_s: float, out: Path) -> dict[str, Any]:
    """Boot an isolated server, arm the goal, drive the turn, judge the evidence."""

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    state = out / "state"
    ws_root = out / "workspace"
    state.mkdir(parents=True, exist_ok=True)
    extra_env = {
        "CLIO_USER_DIR": str(state),
        "CLIO_SESSIONS_PATH": str(state / "sessions.json"),
        "CLIO_ALLOWED_ROOTS": str(ws_root),
    }
    proc = boot_server(port, cwd=ws_root, sse_log=out / "sse.log", extra_env=extra_env)
    call = client(base)
    verdict: dict[str, Any] = {"provider": provider, "model": model, "port": port}
    health = _HealthProbe(base)
    sse: _SseGapProbe | None = None
    try:
        if not wait_health(call):
            raise RuntimeError("server never became healthy")
        verdict["provider_ready"] = bind_provider(call, provider=provider, model=model)
        allow_all(call)
        wsid = create_workspace(call, "goal-judge", ws_root)
        sid = create_session(call, wsid, "goal-judge-1333")
        verdict["session_id"] = sid
        call("POST", f"/v1/sessions/{sid}/commands/goal", {"input": CONDITION})
        armed = _session_meta(call, wsid, sid).get("goal", {}) or {}
        verdict["goal_armed"] = bool(armed.get("active"))
        sse = _SseGapProbe(base, sid)
        sse.start()
        health.start()
        t_start = time.monotonic()
        post_message(call, sid, TASK)
        verdict["first_turn_status"] = wait_turn(call, wsid, sid, max_elapsed=900.0)

        def _cleared() -> dict[str, Any] | None:
            gm = _session_meta(call, wsid, sid).get("goal", {}) or {}
            return gm if gm.get("cleared") else None

        goal = expanding_wait(_cleared, what="goal to clear (judge verdict)", max_elapsed=900.0)
        verdict["elapsed_s"] = round(time.monotonic() - t_start, 1)
        health.stop.set()
        health.join(timeout=15)
        if sse is not None:
            sse.stop.set()
        verdict["goal_meta"] = goal or _session_meta(call, wsid, sid).get("goal", {})
        messages = session_messages(call, sid)
        dump_json(out / "messages.json", messages)
        texts = [
            str(part.get("text") or "")
            for m in messages
            for part in (m.get("parts") or [])
            if isinstance(part, dict)
        ]
        verdict["judge_unavailable_hits"] = sum("judge unavailable" in t for t in texts)
        window = _judge_window(out / "sse.log")
        judge_max, judge_samples = (
            health.max_in_window(*window) if window else (health.max_latency_s, 0)
        )
        verdict["health"] = {
            "samples": health.samples,
            "failures": health.failures,
            "max_latency_overall_s": round(health.max_latency_s, 3),
            "slow_samples": health.slow(),
            "judge_window": (
                {
                    "start": _dt.datetime.fromtimestamp(window[0], tz=_dt.UTC).isoformat(),
                    "end": _dt.datetime.fromtimestamp(window[1], tz=_dt.UTC).isoformat(),
                    "duration_s": round(window[1] - window[0], 1),
                    "samples": judge_samples,
                    "max_latency_s": round(judge_max, 3),
                }
                if window
                else None
            ),
        }
        if sse is not None:
            verdict["sse"] = {
                "events": sse.events,
                "max_gap_s": round(sse.max_gap_s, 1),
                "error": sse.error,
            }
        gm = verdict["goal_meta"] or {}
        checks = {
            "goal_armed": verdict["goal_armed"],
            "goal_met_cleared": bool(gm.get("cleared"))
            and gm.get("clear_reason") == "goal_met"
            and bool(gm.get("met")),
            "goal_within_two_iters": int(gm.get("iters_elapsed", 99) or 0) <= 2,
            "no_judge_unavailable": verdict["judge_unavailable_hits"] == 0,
            "judge_window_found": window is not None,
            "loop_live_during_judge": window is not None
            and judge_samples > 0
            and health.failures == 0
            and judge_max < max_health_latency_s,
        }
        verdict["checks"] = checks
        verdict["pass"] = all(checks.values())
    except Exception as exc:  # noqa: BLE001 - the verdict carries the failure
        verdict["error"] = f"{type(exc).__name__}: {exc}"
        verdict["pass"] = False
    finally:
        health.stop.set()
        if sse is not None:
            sse.stop.set()
        terminate_server(proc)
    return verdict


def main() -> int:
    """CLI entry: run one provider/model leg and write the verdict."""

    ap = argparse.ArgumentParser(description="GOAL judge off-loop live leg (#1333)")
    ap.add_argument("--provider", default="codex")
    ap.add_argument("--model", default="gpt-5.6-luna")
    ap.add_argument("--max-health-latency", type=float, default=1.0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or (OUT_ROOT / f"goal-judge-{args.provider}-{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    verdict = run_leg(args.provider, args.model, args.max_health_latency, out)
    write_verdict(out / "verdict.json", verdict)
    print(json.dumps(verdict, indent=2, default=str))
    print(f"[leg_goal_judge] {'PASS' if verdict.get('pass') else 'FAIL'} evidence={out}")
    return 0 if verdict.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
