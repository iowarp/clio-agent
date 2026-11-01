"""The ``run_code`` kernel: a persistent Python process that runs the agent's scripts.

Standard library only: the agent's Python environment supplies numpy, h5py and the rest.
The host (:mod:`clio_agent.gact.code_mode.kernels`) starts it as ``python -I
kernel_main.py`` inside the shell's write fence and talks newline-delimited JSON over
stdin and the original stdout:

- host to kernel: ``{"type": "bind", "stubs": {...}}`` (the tools a script may call),
  ``{"type": "exec", "id": n, "code": "..."}``, and ``{"type": "result", "id": k,
  "ok": bool, "value": ..., "error": "..."}`` answering a call;
- kernel to host: ``{"type": "ready"}``, ``{"type": "call", "id": k, "tool": name,
  "args": {...}}`` and ``{"type": "done", "id": n, "ok": bool, "output": "...",
  "value": "...", "error": "..."}``.

At start the kernel keeps a duplicate of its stdout for the protocol; each script's
file descriptors 1 and 2 point at a temporary file, so its prints and those of its
subprocesses are captured and never mix with the protocol. Variables and imports
persist between scripts in one namespace. A script may use top-level ``await``; the
value of a final expression is returned like a notebook cell's.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
import os
import queue
import sys
import tempfile
import threading
import traceback
from typing import Any

#: How much of one script's output the kernel returns (the host bounds it further).
OUTPUT_LIMIT = 2_000_000
_FLAGS = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


class ToolError(Exception):
    """A tool call made by the script failed; the message is the tool's error."""


class _Host:
    """The protocol channel: writes to the saved stdout, futures for pending calls."""

    def __init__(self, out: io.TextIOBase) -> None:
        self.out = out
        self.lock = threading.Lock()
        self.pending: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future[Any]]] = {}
        self.next_id = 0

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, default=repr)
        with self.lock:
            self.out.write(line + "\n")
            self.out.flush()

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        with self.lock:
            self.next_id += 1
            call_id = self.next_id
            self.pending[call_id] = (loop, future)
        self.send({"type": "call", "id": call_id, "tool": tool, "args": args})
        return await future

    def answer(self, message: dict[str, Any]) -> None:
        with self.lock:
            entry = self.pending.pop(int(message.get("id", -1)), None)
        if entry is None:
            return
        loop, future = entry

        def settle() -> None:
            if future.done():
                return
            if message.get("ok"):
                future.set_result(message.get("value"))
            else:
                future.set_exception(ToolError(str(message.get("error") or "tool call failed")))

        loop.call_soon_threadsafe(settle)


def _function(host: _Host, tool: str, spec: dict[str, Any]) -> Any:
    """One callable tool: ``await tools.fs.read_file(filepath=...)``."""

    async def stub(**kwargs: Any) -> Any:
        return await host.call(tool, kwargs)

    stub.__name__ = stub.__qualname__ = str(spec.get("function") or tool)
    stub.__doc__ = f"{spec.get('signature') or tool}\n\n{spec.get('doc') or ''}".strip()
    return stub


class _Namespace:
    """A group of tools, by server: ``tools.fs``, ``tools.synthetic_home``."""

    def __init__(self, name: str, doc: str = "") -> None:
        self.__name__ = name
        self.__doc__ = doc

    def __repr__(self) -> str:
        names = sorted(k for k in vars(self) if not k.startswith("_"))
        return f"<tools.{self.__name__}: {', '.join(names)}>"


class _Tools(_Namespace):
    """Every tool this agent may call from a script, grouped by server."""

    def __init__(self, host: _Host, stubs: dict[str, Any]) -> None:
        super().__init__("tools", "Tools callable from this script; help(tools.<group>).")
        self._host = host
        for group, functions in (stubs.get("groups") or {}).items():
            target: _Namespace = self if group == "" else _Namespace(group)
            for function, spec in functions.items():
                setattr(target, function, _function(host, str(spec["tool"]), spec))
            if group:
                setattr(self, group, target)

    async def search(self, query: str, limit: int = 5) -> Any:
        """Find tools by name or keywords (the same catalog as search_tools)."""
        return await self._host.call("__search__", {"query": query, "limit": limit})

    async def call(self, name: str, **args: Any) -> Any:
        """Call a tool by its full name, for names found with tools.search."""
        return await self._host.call(name, args)


def _compile(code: str) -> tuple[Any, Any]:
    """The script's body and, when it ends in an expression, that expression."""
    tree = ast.parse(code, "<script>", "exec")
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = ast.Expression(tree.body.pop().value)
    body = compile(tree, "<script>", "exec", flags=_FLAGS)
    tail = compile(last, "<script>", "eval", flags=_FLAGS) if last is not None else None
    return body, tail


def _run(loop: asyncio.AbstractEventLoop, code_object: Any, namespace: dict[str, Any]) -> Any:
    result = eval(code_object, namespace)  # noqa: S307 - running the agent's script is the point
    if inspect.iscoroutine(result):
        result = loop.run_until_complete(result)
    return result


def _redirect(target: int) -> tuple[int, int]:
    sys.stdout.flush()
    sys.stderr.flush()
    saved = (os.dup(1), os.dup(2))
    os.dup2(target, 1)
    os.dup2(target, 2)
    return saved


def _restore(saved: tuple[int, int]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(saved[0], 1)
    os.dup2(saved[1], 2)
    os.close(saved[0])
    os.close(saved[1])


def _read_output(capture: Any) -> str:
    capture.flush()
    size = capture.seek(0, os.SEEK_END)
    capture.seek(0)
    data = capture.read(OUTPUT_LIMIT)
    capture.seek(0)
    capture.truncate()
    text = data.decode("utf-8", errors="replace")
    if size > OUTPUT_LIMIT:
        text += f"\n[output cut at {OUTPUT_LIMIT:,} of {size:,} bytes]"
    return text


def _script_traceback(exc: BaseException) -> str:
    """The traceback from the script's own frames on (the kernel's are noise)."""
    trace = exc.__traceback__
    while trace is not None and trace.tb_frame.f_code.co_filename != "<script>":
        trace = trace.tb_next
    if isinstance(exc, SyntaxError):
        trace = None
    return "".join(traceback.format_exception(type(exc), exc, trace))


def _execute(
    loop: asyncio.AbstractEventLoop, namespace: dict[str, Any], code: str, capture: Any
) -> dict[str, Any]:
    saved = _redirect(capture.fileno())
    ok, value, error = True, None, ""
    try:
        body, tail = _compile(code)
        _run(loop, body, namespace)
        if tail is not None:
            result = _run(loop, tail, namespace)
            if result is not None:
                value = repr(result)
                namespace["_"] = result
    except SystemExit as exc:
        ok, error = False, f"SystemExit({exc.code!r}): the kernel keeps running"
    except BaseException as exc:  # noqa: BLE001 - every script failure is the agent's to read
        ok = False
        error = _script_traceback(exc)
    finally:
        _restore(saved)
    return {"ok": ok, "output": _read_output(capture), "value": value, "error": error}


def main() -> None:
    """Serve scripts until stdin closes."""
    protocol = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"), encoding="utf-8")
    host = _Host(protocol)
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    work: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def read() -> None:
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "result":
                host.answer(message)
            else:
                work.put(message)
        work.put(None)

    threading.Thread(target=read, name="kernel-stdin", daemon=True).start()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    namespace: dict[str, Any] = {"__name__": "__main__", "ToolError": ToolError}
    capture = tempfile.TemporaryFile()
    host.send({"type": "ready", "pid": os.getpid(), "python": sys.version.split()[0]})
    while (message := work.get()) is not None:
        if message.get("type") == "bind":
            namespace["tools"] = _Tools(host, message.get("stubs") or {})
            host.send({"type": "bound"})
        elif message.get("type") == "exec":
            done = _execute(loop, namespace, str(message.get("code") or ""), capture)
            host.send({"type": "done", "id": message.get("id"), **done})


if __name__ == "__main__":
    main()
