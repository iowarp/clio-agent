#!/usr/bin/env python3
"""Measure a real server's startup, memory and shutdown on a generated store.

Boots ``clio-agent serve`` on a copy of a store made by ``scripts/create_reference_store.py``
(the copy keeps the reference store unchanged between runs), then reads the server's
own startup timeline from ``GET /v1/health`` (``boot``: steps and marks, see
``gact/boot_timing.py``) until the task runtime is ready, records the server's memory,
optionally opens conversations and measures memory again after an idle period, and
finally stops the server and times the shutdown.

Prints one JSON object and, with ``--out``, writes it to a file. Without an explicit
LM provider in the environment the server starts without an agent, which measures
everything startup does with the store; set ``CLIO_LM_PROVIDER`` to include the agent.
``--scripted-model`` instead serves the store with the reference store's scripted model
(``create_reference_store.py --serve``), so the obligations lane really runs the
interrupted work it resumes: the backlog measurement (``--wait-obligations``) then
reports the server's peak memory while it drains, and ``--open-during-drain`` times the
first page of conversations opened while the drain is still running.

Usage::

    uv run python scripts/measure_startup.py --store /path/to/refstore --out startup.json
    uv run python scripts/measure_startup.py --store /path/to/refstore --open 50 --idle 60
    uv run python scripts/measure_startup.py --store /path/to/backlog --scripted-model \
        --wait-obligations --open-during-drain 5
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil

SCRIPTS = Path(__file__).resolve().parent


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, path: str, timeout: float = 5.0) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def _rss_mb(pid: int) -> float:
    return round(psutil.Process(pid).memory_info().rss / 2**20, 1)


class _PeakRss:
    """Sample a process's resident memory until stopped; keep the peak per phase."""

    def __init__(self, pid: int, interval: float = 0.2) -> None:
        self.pid, self.interval = pid, interval
        self.phase = "boot"
        self.peaks: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                rss = _rss_mb(self.pid)
            except psutil.Error:
                return
            self.peaks[self.phase] = max(rss, self.peaks.get(self.phase, 0.0))

    def stop(self) -> dict[str, float]:
        self._stop.set()
        self._thread.join(timeout=5)
        return dict(self.peaks)


def _first_pages(port: int, count: int, prefix: str) -> list[dict[str, Any]]:
    """Open conversations whose obligations are still draining and time their first page.

    The newest matching conversations drain last (oldest first), so they are the ones
    most likely still waiting when they are opened.
    """
    _status, body = _get(port, "/v1/sessions?limit=1000000", timeout=60)
    rows = body.get("sessions", body.get("items", [])) if isinstance(body, dict) else []
    chosen = [row for row in rows if str(row.get("title", "")).startswith(prefix)][:count]
    opened = []
    for row in chosen:
        _health, health = _get(port, "/v1/health")
        steps = (health or {}).get("boot", {}).get("steps", []) if isinstance(health, dict) else []
        draining = not any(step["name"] == "obligations" for step in steps)
        started = time.monotonic()
        status, page = _get(port, f"/v1/sessions/{row['id']}/messages?limit=50", timeout=120)
        opened.append(
            {
                "session": row["id"],
                "title": row.get("title"),
                "status": status,
                "messages": len(page.get("messages", [])) if isinstance(page, dict) else None,
                "seconds": round(time.monotonic() - started, 2),
                "obligations_running": draining,
            }
        )
    return opened


def _server_argv(args: argparse.Namespace, copy: Path, port: int) -> list[str]:
    if args.scripted_model:
        return [
            sys.executable,
            "-B",
            str(SCRIPTS / "create_reference_store.py"),
            "--serve",
            "--out",
            str(copy),
            "--port",
            str(port),
            "--think",
            str(args.think),
        ]
    return [
        sys.executable,
        "-c",
        "from clio_agent.ui.cli import main; main()",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def _repoint(copy: Path, store: Path) -> None:
    """Rewrite the store's own absolute paths (clio-core config, workspaces) to the copy."""
    old, new = str(store), str(copy)
    for path in copy.rglob("*"):
        if not path.is_file() or path.suffix not in {".yaml", ".yml", ".json", ".toml", ".sh"}:
            continue
        if path.stat().st_size > 4 * 2**20:
            continue
        text = path.read_text(encoding="utf-8", errors="strict")
        if old in text:
            path.write_text(text.replace(old, new), encoding="utf-8")


def _store_environment(store: Path, copy: Path) -> dict[str, str]:
    summary = json.loads((store / "summary.json").read_text(encoding="utf-8"))
    env = dict(summary["environment"])
    for key in ("CLIO_AGENT_HOME", "CLIO_RUNTIME_STATE_DIR", "CLIO_DEFAULT_MARKETPLACE"):
        env[key] = env[key].replace(str(store), str(copy))
    return env


def measure(args: argparse.Namespace) -> dict[str, Any]:
    """Boot, observe, open, idle and stop one server on a copy of the store."""
    store = args.store.resolve()
    work = Path(tempfile.mkdtemp(prefix="clio-startup-", dir=args.work_dir))
    copy = store if args.in_place else work / "store"
    started_copy = time.monotonic()
    if not args.in_place:
        shutil.copytree(store, copy, symlinks=True)
        _repoint(copy, store)
    copy_s = round(time.monotonic() - started_copy, 1)
    env = {**os.environ, **_store_environment(store, copy)}
    port = _free_port()
    log = (work / "server.log").open("w", encoding="utf-8")
    t0 = time.monotonic()
    server = subprocess.Popen(  # noqa: S603 - fixed argv
        _server_argv(args, copy, port),
        env=env,
        cwd=copy / "workspace",
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    result: dict[str, Any] = {"store": str(store), "copy_seconds": copy_s}
    peaks = _PeakRss(server.pid)
    try:
        first_health = None
        boot: dict[str, Any] = {}
        deadline = t0 + args.timeout
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"server exited with {server.returncode}; see {work}/server.log")
            status, body = _get(port, "/v1/health")
            if status and first_health is None:
                first_health = round(time.monotonic() - t0, 2)
            if isinstance(body, dict) and body.get("boot"):
                boot = body["boot"]
                if "task_runtime_ready" in boot.get("marks", {}):
                    break
            time.sleep(0.25)
        else:
            raise TimeoutError(f"task runtime not ready within {args.timeout}s; see {work}")
        result["rss_at_ready_mb"] = _rss_mb(server.pid)
        if args.wait_obligations:
            # The obligations lane resumes open work after ready; wait for it to finish.
            peaks.phase = "obligations"
            obligations_started = time.monotonic()
            if args.open_during_drain:
                result["opened_during_drain"] = _first_pages(
                    port, args.open_during_drain, args.drain_title
                )
            while time.monotonic() < deadline:
                _status, body = _get(port, "/v1/health")
                steps = (
                    (body or {}).get("boot", {}).get("steps", []) if isinstance(body, dict) else []
                )
                if any(step["name"] == "obligations" for step in steps):
                    boot = body["boot"]
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError(f"obligations not finished within {args.timeout}s")
            result["rss_after_obligations_mb"] = _rss_mb(server.pid)
            result["obligations_wait_s"] = round(time.monotonic() - obligations_started, 1)
            peaks.phase = "after_obligations"
        status, sessions = _get(port, "/v1/sessions?limit=1000000", timeout=60)
        listed = (
            len(sessions.get("sessions", sessions.get("items", [])))
            if isinstance(sessions, dict)
            else None
        )
        result.update(
            first_health_s=first_health,
            marks=boot.get("marks", {}),
            steps=sorted(boot.get("steps", []), key=lambda row: row["start_s"]),
            sessions_listed=listed,
            rss_ready_mb=_rss_mb(server.pid),
        )
        if args.open and isinstance(sessions, dict):
            rows = sessions.get("sessions", sessions.get("items", []))
            for row in rows[: args.open]:
                _get(port, f"/v1/sessions/{row['id']}/messages", timeout=60)
            result["rss_after_open_mb"] = _rss_mb(server.pid)
            time.sleep(args.idle)
            result["rss_after_idle_mb"] = _rss_mb(server.pid)
            result["opened"] = min(args.open, len(rows))
    finally:
        result["rss_peak_mb"] = peaks.stop()
        stop_started = time.monotonic()
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=args.stop_timeout)
            result["shutdown_forced"] = False
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
            result["shutdown_forced"] = True
        result["shutdown_s"] = round(time.monotonic() - stop_started, 2)
        log.close()
        result["daemon_left_running"] = _stop_leftover_daemon(env, copy)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


def _stop_leftover_daemon(env: dict[str, str], copy: Path) -> bool:
    """Stop a clio-core daemon the server left behind; report whether there was one."""
    for key in ("CLIO_AGENT_HOME", "CLIO_RUNTIME_STATE_DIR", "CLIO_CORE_PORT"):
        os.environ[key] = env[key]
    previous = Path.cwd()
    os.chdir(copy / "workspace")  # the config resolves the way the server resolved it
    try:
        from clio_agent.arc import runtime_stop, storage  # noqa: PLC0415
        from clio_agent.arc.clio_core_config import _resolve_config_path  # noqa: PLC0415

        if not storage._daemon_pidfile().is_file():
            return False
        config = str(_resolve_config_path(os.environ))
        if not runtime_stop.cleanup_runtime_after_client_crash(config, wait_timeout_seconds=30.0):
            runtime_stop.kill_daemon_pidfile()
        return True
    finally:
        os.chdir(previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=float, default=1800.0, help="seconds to wait for ready")
    parser.add_argument("--open", type=int, default=0, help="conversations to open after ready")
    parser.add_argument("--idle", type=float, default=0.0, help="seconds to idle after opening")
    parser.add_argument("--stop-timeout", type=float, default=60.0)
    parser.add_argument("--work-dir", type=Path, default=None, help="where the store copy goes")
    parser.add_argument("--keep", action="store_true", help="keep the copy and server log")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="run on the store itself, not a copy (it changes: resumed work drains)",
    )
    parser.add_argument(
        "--wait-obligations",
        action="store_true",
        help="also wait for the background obligations lane to finish and time it",
    )
    parser.add_argument(
        "--scripted-model",
        action="store_true",
        help="serve with the reference store's scripted model, so resumed work really runs",
    )
    parser.add_argument("--think", type=float, default=0.0, help="scripted model seconds")
    parser.add_argument(
        "--open-during-drain",
        type=int,
        default=0,
        help="with --wait-obligations, time the first page of N conversations mid-drain",
    )
    parser.add_argument(
        "--drain-title",
        default="interrupted",
        help="title prefix of the conversations --open-during-drain opens",
    )
    args = parser.parse_args()
    args.out = args.out.resolve() if args.out else None  # measuring changes directory
    result = measure(args)
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
