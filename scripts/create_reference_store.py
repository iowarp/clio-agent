#!/usr/bin/env python3
"""Generate a long-lived, flow-heavy clio-core store for startup and memory measurements.

The startup that matters is not a clean first start but the restart after months of
use: thousands of conversations, most of them flow children, long transcripts with
``lm.call`` records, plus work left open when Clio stopped -- open tasks, unread mail,
interrupted turns. This script builds such a store through Clio's real write paths:
the GACT app and task layer run for real against a private clio-core, and only the
model is scripted (deterministic answers of a chosen size), as in ``tests/test_flows``.

What it writes, under ``--out``:

* ``home/``: the Clio home (``CLIO_AGENT_HOME``) holding the clio-core store, the
  session registry, the plugin store and config;
* ``runtime/``: the clio-core runtime state (``CLIO_RUNTIME_STATE_DIR``);
* ``marketplace/``: the default marketplace the conversations' agents come from
  (the hermetic test fixture plus a ``refstore`` plugin of flows);
* ``workspace/``: the default workspace;
* ``env.sh``: the environment a server needs to open this store
  (``CLIO_AGENT_HOME``, ``CLIO_RUNTIME_STATE_DIR``, ``CLIO_DEFAULT_MARKETPLACE``, ...);
* ``summary.json``: what was generated.

Shape, per ``--scale`` unit (defaults):

* ``--flows`` fan-out conversations, each with ``--children`` child conversations;
* ``--chats`` plain conversations of ``--chat-turns`` turns each;
* ``--open`` children left awaiting a follow-up (open tasks);
* ``--interrupted`` fan-outs whose children are still working when the generator
  stops Clio (interrupted turns, and mail that arrives on the next start).

Every scripted turn records one ``lm.call`` event of about ``--lm-call-kb`` and
answers with about ``--answer-kb`` of text.

Usage::

    uv run python scripts/create_reference_store.py --out /path/to/refstore
    uv run python scripts/create_reference_store.py --out /path/to/refstore4x --scale 4

Then start a server on it with ``source /path/to/refstore/env.sh`` before
``clio-agent serve``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

FLOWS = """
from clio_agent.sdk import Flow


class Fanout(Flow):
    id = "fanout"

    async def forward(self, task, ctx):
        head, _, rest = task.partition(" ")
        count = int(head) if head.isdigit() else 3
        calls = [ctx.call("self", f"part {i}: {rest}") for i in range(count)]
        results = await ctx.gather(calls, return_exceptions=True)
        return " | ".join(getattr(r, "answer", type(r).__name__) for r in results)
"""

AGENTS = """
from clio_agent.sdk import Agent


class Fanouter(Agent):
    id = "refstore-fanout"
    version = "1.0.0"
    role = "fans a task out to child conversations"
    flow = "fanout"
    plugins = Agent.plugins + ["workspace", "delegation", "refstore"]


class Chatter(Agent):
    id = "refstore-chat"
    version = "1.0.0"
    role = "answers in a plain conversation"
    plugins = Agent.plugins + ["workspace", "refstore"]
"""


def _free_core_port_block(span: int = 5) -> int:
    """Five adjacent free ports below the ephemeral range (as ``install/arc_smoke.py``)."""
    floor, ceiling = 20_000, 32_000
    for _ in range(400):
        base = floor + secrets.randbelow(ceiling - floor - span)
        with ExitStack() as probes:
            try:
                for offset in range(span):
                    probes.enter_context(socket.socket()).bind(("0.0.0.0", base + offset))  # noqa: S104
            except OSError:
                continue
            return base
    raise RuntimeError("no free five-port block for the private clio-core")


def _environment(out: Path, capacity: str) -> dict[str, str]:
    return {
        "CLIO_AGENT_HOME": str(out / "home"),
        "CLIO_RUNTIME_STATE_DIR": str(out / "runtime"),
        "CLIO_DEFAULT_MARKETPLACE": str(out / "marketplace"),
        "CLIO_CORE_PORT": str(_free_core_port_block()),
        "CLIO_ARC_CTE_FILE_CAPACITY": capacity,
    }


def _write_marketplace(root: Path) -> None:
    sys.path.insert(0, str(REPO))
    from tests._plugin_fixtures import FIXTURE  # noqa: PLC0415
    from tests.test_plugins.helpers import plugin, write  # noqa: PLC0415

    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__"))
    plugin(
        root / "plugins",
        "refstore",
        files={"ai.iowarp.clio/flows/refstore_flows.py": FLOWS},
        ext={"requires": ["delegation"]},
    )
    write(root / "agents" / "refstore_agents.py", textwrap.dedent(AGENTS))


def _text(kb: int, seed: str) -> str:
    unit = f"{seed} lorem ipsum dolor sit amet, consectetur adipiscing elit. "
    return (unit * (kb * 1024 // len(unit) + 1))[: kb * 1024]


class _Program:
    """The scripted model: sized answers, one ``lm.call`` record per turn."""

    def __init__(self, answer_kb: int, lm_call_kb: int) -> None:
        self.answer_kb = answer_kb
        self.lm_call_kb = lm_call_kb
        self.stall = True

    def __call__(self, question: str = "", **_: Any) -> Any:
        import dspy  # noqa: PLC0415

        from clio_agent.gact import context  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        app, session = context.active_app(), context.active_session_id()
        if "STALL" in question:
            from clio_agent.agent import cancellation_requested  # noqa: PLC0415
            from clio_agent.gact.agents.clio_react import _turn_cancelled  # noqa: PLC0415

            while self.stall and not cancellation_requested():
                time.sleep(0.05)
            raise _turn_cancelled()
        if "YIELD" in question:
            return dspy.Prediction(answer="waiting for a follow-up", assignment_disposition="yield")
        prompt = _text(self.lm_call_kb, question[:40])
        answer = _text(self.answer_kb, f"answer to {question[:40]}")
        record = {
            "model": "scripted/refstore",
            "messages": [{"role": "user", "content": prompt}],
            "content": answer,
            "content_len": len(answer),
            "reasoning_content": "",
            "reasoning_len": 0,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(answer) // 4},
        }
        _emit_semantic_event(
            app,
            session,
            "lm.call",
            status="completed",
            summary="LM call (stop).",
            provider={"model_id": record["model"]},
            payload=record,
            detail_level="off",
        )
        return dspy.Prediction(answer=answer)


class _ScriptedModule:
    """The real flow runner over the pinned resolution; the program stands in for the model."""

    def __init__(self, agent_def: Any, resolution: Any, program: Any) -> None:
        self.agent_def, self.resolution, self.program, self.tools = (
            agent_def,
            resolution,
            program,
            [],
        )

    def __call__(self, question: str, session_id: str, **_: Any) -> Any:
        import dspy  # noqa: PLC0415

        from clio_agent.flows.runner import (  # noqa: PLC0415
            accept_flow_input,
            flow_selection,
            run_agent_flow,
        )
        from clio_agent.gact import context  # noqa: PLC0415
        from tests._harness import _commit_test_model_input  # noqa: PLC0415

        if flow_selection(context.active_app(), session_id, self.agent_def).get("id") == "react":
            _commit_test_model_input(self.agent_def, question, session_id)
        else:
            tokens = [
                context.set_react_scope(self.agent_def.id),
                context.set_react_session(session_id),
            ]
            try:
                accept_flow_input(self, question, session_id)
            finally:
                for token in reversed(tokens):
                    context.reset(token)
        prediction = run_agent_flow(self, {"question": question, "session_id": session_id})
        return dspy.Prediction(
            answer=str(getattr(prediction, "answer", "")),
            workflow_state=getattr(prediction, "workflow_state", ""),
            tools_called=list(getattr(prediction, "tools_called", None) or []),
            selected_expert=self.agent_def.id,
            assignment_disposition=getattr(prediction, "assignment_disposition", None),
        )


class _Host:
    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover
        raise AssertionError("the scripted module runs instead of the host")


def _install_scripted_model(program: _Program) -> None:
    from clio_agent.gact.agents import builders  # noqa: PLC0415

    def scripted(**kwargs: Any) -> Any:
        from clio_agent.gact import context  # noqa: PLC0415

        app = context.active_app()
        assignment = app.state.task_history.latest_assignment(context.active_session_id())
        if assignment is not None and assignment.owner.parent_handle is not None:
            kwargs["question"] = assignment.description
        return program(**kwargs)

    def build(base_agent: Any, agent_def: Any, resolution: Any = None) -> _ScriptedModule:
        return _ScriptedModule(agent_def, resolution, scripted)

    builders.build_agent_module = build


def _session(client: Any, agent: str, title: str) -> str:
    response = client.post("/v1/sessions", json={"title": title, "metadata": {"agent_ref": agent}})
    response.raise_for_status()
    return str(response.json()["id"])


def _post(client: Any, sid: str, text: str) -> None:
    response = client.post(
        f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": text}]}
    )
    if response.status_code != 202:
        raise RuntimeError(f"message to {sid} refused: {response.status_code} {response.text}")


_BUSY = {"running", "stopping", "queued"}


def _wait_idle(app: Any, timeout: float) -> None:
    """Wait until no conversation is busy, child conversations included."""
    deadline = time.monotonic() + timeout
    while True:
        busy = [s.id for s in app.state.sessions.list() if str(s.status) in _BUSY]
        if not busy:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"{len(busy)} conversations still running, e.g. {busy[:3]}")
        time.sleep(0.2)


def generate(args: argparse.Namespace) -> dict[str, Any]:
    """Run Clio on the private store and write the reference conversations."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from clio_agent.gact.app import build_app  # noqa: PLC0415

    program = _Program(args.answer_kb, args.lm_call_kb)
    _install_scripted_model(program)
    flows, chats = args.flows * args.scale, args.chats * args.scale
    opened, interrupted = args.open * args.scale, args.interrupted * args.scale
    started = time.monotonic()
    app = build_app(agent=_Host())
    summary: dict[str, Any] = {"scale": args.scale}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        batch: list[str] = []
        for index in range(flows):
            sid = _session(client, "refstore-fanout", f"fan-out {index}")
            _post(client, sid, f"{args.children} survey dataset {index}")
            batch.append(sid)
            if len(batch) >= args.batch:
                _wait_idle(app, args.timeout)
                batch = []
        for index in range(chats):
            sid = _session(client, "refstore-chat", f"chat {index}")
            for turn in range(args.chat_turns):
                _post(client, sid, f"chat {index} question {turn}")
                _wait_idle(app, args.timeout)
        _wait_idle(app, args.timeout)
        for index in range(opened):
            sid = _session(client, "refstore-fanout", f"open {index}")
            _post(client, sid, f"1 YIELD open task {index}")
            batch.append(sid)
        _wait_idle(app, args.timeout)
        for index in range(interrupted):
            sid = _session(client, "refstore-fanout", f"interrupted {index}")
            _post(client, sid, f"{args.children} STALL interrupted work {index}")
        time.sleep(args.settle)
        summary["conversations"] = len(app.state.sessions.list())
        summary["open_records"] = len(app.state.task_history.index.open_records(""))
        summary["seconds"] = round(time.monotonic() - started, 1)
        summary.update(
            flows=flows,
            children_per_flow=args.children,
            chats=chats,
            chat_turns=args.chat_turns,
            open_children=opened,
            interrupted_flows=interrupted,
            answer_kb=args.answer_kb,
            lm_call_kb=args.lm_call_kb,
        )
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", "utf-8")
        # Stop the way a crash does: the interrupted children are still working, so the
        # store holds interrupted turns and undelivered mail, as after a real kill.
        sys.stdout.flush()
        os._exit(0)
    raise AssertionError("unreachable")


def _stop_core(out: Path, env: dict[str, str]) -> None:
    """Stop the private clio-core after the generator process ended like a crash.

    The stop request must name the daemon's own config (its port and store), which is
    resolved exactly as the generator resolved it: same environment, same workspace.
    """
    os.environ.update(env)
    os.chdir(out / "workspace")
    from clio_agent.arc import runtime_stop  # noqa: PLC0415
    from clio_agent.arc.clio_core_config import _resolve_config_path  # noqa: PLC0415

    config = str(_resolve_config_path(os.environ))
    if not runtime_stop.cleanup_runtime_after_client_crash(config, wait_timeout_seconds=30.0):
        runtime_stop.kill_daemon_pidfile()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="new directory for the store")
    parser.add_argument("--scale", type=int, default=1, help="multiply every count")
    parser.add_argument("--flows", type=int, default=150)
    parser.add_argument("--children", type=int, default=4)
    parser.add_argument("--chats", type=int, default=100)
    parser.add_argument("--chat-turns", type=int, default=6)
    parser.add_argument("--open", type=int, default=20)
    parser.add_argument("--interrupted", type=int, default=10)
    parser.add_argument("--answer-kb", type=int, default=4)
    parser.add_argument("--lm-call-kb", type=int, default=24)
    parser.add_argument("--batch", type=int, default=10, help="fan-outs running at once")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--settle", type=float, default=5.0, help="seconds before stopping")
    parser.add_argument("--capacity", default="2GB", help="clio-core file-tier capacity")
    parser.add_argument("--generator", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.out = args.out.resolve()

    if args.generator:  # the child process: env is already set by the parent
        os.chdir(args.out / "workspace")
        sys.path.insert(0, str(REPO))
        generate(args)

    out = args.out
    if out.exists() and any(out.iterdir()):
        parser.error(f"{out} is not empty; pass a new directory")
    for name in ("home", "runtime", "workspace"):
        (out / name).mkdir(parents=True, exist_ok=True)
    _write_marketplace(out / "marketplace")
    env = _environment(out, args.capacity)
    (out / "env.sh").write_text(
        "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items()),
        encoding="utf-8",
    )
    child = subprocess.run(  # noqa: S603 - this script, fixed argv
        [sys.executable, "-B", __file__, *sys.argv[1:], "--generator"],
        env={**os.environ, **env},
        check=False,
    )
    _stop_core(out, env)
    summary_path = out / "summary.json"
    if child.returncode != 0 or not summary_path.is_file():
        print(f"generation failed (exit {child.returncode}); see the output above", file=sys.stderr)
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["environment"] = env
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
