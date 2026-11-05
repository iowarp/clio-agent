"""The ``run_code`` kernel: a persistent Python process that runs the agent's scripts.

Standard library only: the agent's Python environment supplies numpy, h5py and the rest.
The host (:mod:`clio_agent.gact.code_mode.kernels`) starts it as ``python -I
kernel_main.py <output limit>`` inside the shell's write fence and talks
newline-delimited JSON over stdin and the original stdout:

- host to kernel: ``{"type": "bind", "stubs": {...}, "bindings": {name: {"source",
  "digest"}}, "unbind": [name]}`` (the tools a script may call, and the plugins' kernel
  bindings that changed), ``{"type": "exec", "id": n, "code": "..."}``, and
  ``{"type": "result", "id": k, "ok": bool, "value": ..., "error": "..."}`` answering a
  call;
- kernel to host: ``{"type": "ready"}``, ``{"type": "bound", "failed": {name: error}}``,
  ``{"type": "call", "id": k, "tool": name, "args": {...}}`` and ``{"type": "done", "id":
  n, "ok": bool, "output": "...", "value": "...", "error": "..."}``.

The script's names come from one binding registry: ``tools`` is its first entry, generated
from the stubs; each plugin binding is its source run in a module that holds ``tools`` and
``ToolError``, bound under its name as the module's ``export``
(:mod:`clio_agent.plugins.kernel_bindings`).

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
import types
from typing import Any, cast

#: How much of one script's output the kernel returns (the host bounds it further) when
#: the host passes no limit; the host passes ``code.max_output_bytes`` as the first argument.
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

    def forget_settled(self) -> None:
        """Drop pending entries whose futures a stopped task already cancelled."""
        with self.lock:
            for call_id in [k for k, (_, f) in self.pending.items() if f.done()]:
                del self.pending[call_id]


def _function(host: _Host, tool: str, spec: dict[str, Any]) -> Any:
    """One callable tool: ``await tools.fs.read_file(path=...)``."""

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


class _Bindings:
    """The binding registry: the names a script gets, ``tools`` first."""

    def __init__(self, host: _Host, namespace: dict[str, Any]) -> None:
        self.host = host
        self.namespace = namespace
        self.modules: dict[str, types.ModuleType] = {}

    def _tools(self, stubs: dict[str, Any]) -> Any:
        return _Tools(self.host, stubs)

    def _plugin(self, name: str, spec: dict[str, Any]) -> Any:
        module = types.ModuleType(f"clio_binding_{name}")
        module.__dict__.update(tools=self.namespace.get("tools"), ToolError=ToolError)
        code = compile(str(spec.get("source") or ""), f"<binding {name}>", "exec")
        exec(code, module.__dict__)  # noqa: S102 - a trusted plugin's binding (plugin check)
        if "export" not in module.__dict__:
            raise NameError(f"binding {name!r} defines no export")
        self.modules[name] = module
        return module.__dict__["export"]

    def bind(self, message: dict[str, Any]) -> dict[str, str]:
        """Apply one ``bind`` message; the bindings that failed, with their errors."""
        if "stubs" in message:
            self.namespace["tools"] = self._tools(message.get("stubs") or {})
            for module in self.modules.values():  # bindings call the current tools
                module.__dict__["tools"] = self.namespace["tools"]
        for name in message.get("unbind") or ():
            self.modules.pop(str(name), None)
            self.namespace.pop(str(name), None)
        failed: dict[str, str] = {}
        for name, spec in (message.get("bindings") or {}).items():
            try:
                self.namespace[str(name)] = self._plugin(str(name), dict(spec))
            except Exception as exc:  # noqa: BLE001 - forwards: to the host, typed there
                self.namespace.pop(str(name), None)
                failed[str(name)] = "".join(traceback.format_exception_only(type(exc), exc))
        return failed


def _compile(code: str) -> tuple[Any, Any]:
    """The script's body and, when it ends in an expression, that expression."""
    tree = ast.parse(code, "<script>", "exec")
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = ast.Expression(cast(ast.Expr, tree.body.pop()).value)
    body = compile(tree, "<script>", "exec", flags=_FLAGS)
    tail = compile(last, "<script>", "eval", flags=_FLAGS) if last is not None else None
    return body, tail


def _run(loop: asyncio.AbstractEventLoop, code_object: Any, namespace: dict[str, Any]) -> Any:
    result = eval(code_object, namespace)  # noqa: S307 - running the agent's script is the point
    if inspect.iscoroutine(result):
        result = loop.run_until_complete(result)
    return result


def _stop_leftovers(loop: asyncio.AbstractEventLoop) -> int:
    """Cancel the tasks a script started and left running; how many there were.

    The loop outlives the script, so a task a failed ``gather`` left behind would
    otherwise keep calling tools during the next script, against that script's budget.
    A script's tasks end with it; its variables persist.
    """
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    return len(pending)


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


def _read_output(capture: Any, limit: int = OUTPUT_LIMIT, *, keep: bool = False) -> str:
    """The script's output, its first ``limit`` bytes; a kept file (a background script's
    output file) is left whole for the person and the agent to read."""
    capture.flush()
    size = capture.seek(0, os.SEEK_END)
    capture.seek(0)
    data = capture.read(limit)
    if not keep:
        capture.seek(0)
        capture.truncate()
    text = data.decode("utf-8", errors="replace")
    if size > limit:
        text += f"\n[output cut at {limit:,} of {size:,} bytes]"
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
    loop: asyncio.AbstractEventLoop,
    namespace: dict[str, Any],
    code: str,
    capture: Any,
    host: _Host | None = None,
    output_limit: int = OUTPUT_LIMIT,
    *,
    keep: bool = False,
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
    except BaseException as exc:  # noqa: BLE001 - forwards: the traceback to the agent
        ok = False
        error = _script_traceback(exc)
    finally:
        stopped = _stop_leftovers(loop)
        if host is not None:
            host.forget_settled()
        _restore(saved)
    output = _read_output(capture, output_limit, keep=keep)
    if stopped:
        output += (
            f"\n[{stopped} task{'s' if stopped != 1 else ''} this script started "
            f"{'were' if stopped != 1 else 'was'} still running and stopped with it.]"
        )
    return {"ok": ok, "output": output, "value": value, "error": error}


def main() -> None:
    """Serve scripts until stdin closes."""
    output_limit = int(sys.argv[1]) if len(sys.argv) > 1 else OUTPUT_LIMIT
    protocol = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"), encoding="utf-8")
    host = _Host(protocol)
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[union-attr]
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
    bindings = _Bindings(host, namespace)
    while (message := work.get()) is not None:
        if message.get("type") == "bind":
            host.send({"type": "bound", "failed": bindings.bind(message)})
        elif message.get("type") == "exec":
            code = str(message.get("code") or "")
            path = str(message.get("output_path") or "")
            if path:  # a background script: output goes to its file as it is written
                with open(path, "ab+") as written:
                    done = _execute(loop, namespace, code, written, host, output_limit, keep=True)
            else:
                done = _execute(loop, namespace, code, capture, host, output_limit)
            host.send({"type": "done", "id": message.get("id"), **done})


if __name__ == "__main__":
    main()
