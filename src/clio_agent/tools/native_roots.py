"""Independent native FastMCP/Docket resources, without a CLIO execution queue."""

from __future__ import annotations

import inspect
import typing
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar
from uuid import uuid4

from fastmcp import FastMCP
from fastmcp_tasks import TasksExtension

from clio_agent import conf

NativeRootKind = Literal["shell", "storage"]
ToolBody = TypeVar("ToolBody", bound=Callable[..., Awaitable[Any]])


@dataclass(frozen=True)
class NativeRootSettings:
    """Native backend configuration resolved through CLIO's existing settings."""

    url: str
    queue: str
    concurrency: int = 10


@dataclass(frozen=True)
class NativeRoot:
    """Server resources the application lifespan owns; this holds no task state."""

    server: FastMCP
    extension: TasksExtension
    settings: NativeRootSettings
    bodies: list[Callable[..., Awaitable[Any]]] = field(default_factory=list)

    def tool(self, **options: Any) -> Callable[[ToolBody], ToolBody]:
        """Register a native tool body that library caches cannot keep alive.

        FastMCP, Docket and uncalled_for memoize per tool function, uncalled_for without
        a bound. A native tool body closes over its application, so registering the body
        itself kept every application the process built reachable after it shut down
        (a test worker held hundreds and was OOM-killed). The server gets a forwarder
        that reaches the body through a weak reference; this root, which the
        application owns, holds the body.
        """

        def register(body: ToolBody) -> ToolBody:
            self.bodies.append(body)
            self.server.tool(**options)(_weak_forwarder(body))
            return body

        return register


def _weak_forwarder(body: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """An async function with ``body``'s name and signature that holds it only weakly."""
    target = weakref.ref(body)

    async def forward(*args: Any, **kwargs: Any) -> Any:
        live = target()
        if live is None:
            raise RuntimeError(f"Native tool {body_name} outlived the application that owned it")
        return await live(*args, **kwargs)

    body_name = body.__name__
    forward.__name__ = body_name
    forward.__qualname__ = body.__qualname__
    forward.__module__ = body.__module__
    forward.__doc__ = body.__doc__
    # Resolved here: the forwarder's globals cannot resolve the body's string annotations.
    forward.__annotations__ = typing.get_type_hints(body)
    forward.__signature__ = inspect.signature(body, eval_str=True)  # type: ignore[attr-defined]
    return forward


def resolve_native_root(kind: NativeRootKind) -> NativeRootSettings:
    """Keep native URL/queue/capacity settings and distinct default queue identities."""
    if kind not in {"shell", "storage"}:
        raise ValueError("Unknown native root")
    if kind == "shell":
        url = conf.resolve(
            "gact.tasks.shell.url", env="CLIO_TASKS_SHELL_URL", default="memory://", cast=str
        )
        queue = conf.resolve(
            "gact.tasks.shell.queue", env="CLIO_TASKS_SHELL_QUEUE", default="", cast=str
        )
        concurrency = conf.resolve(
            "gact.tasks.shell.concurrency",
            env="CLIO_TASKS_SHELL_CONCURRENCY",
            default=10,
            cast=conf.as_int,
        )
    else:
        url = conf.resolve(
            "gact.tasks.storage.url", env="CLIO_TASKS_STORAGE_URL", default="memory://", cast=str
        )
        queue = conf.resolve(
            "gact.tasks.storage.queue", env="CLIO_TASKS_STORAGE_QUEUE", default="", cast=str
        )
        concurrency = conf.resolve(
            "gact.tasks.storage.concurrency",
            env="CLIO_TASKS_STORAGE_CONCURRENCY",
            default=10,
            cast=conf.as_int,
        )
    if not url or concurrency < 1:
        raise ValueError("Native roots require a backend URL and positive worker concurrency")
    if not queue:
        queue = f"clio-{kind}"
        if url.startswith("memory://"):
            queue += "-" + uuid4().hex
    return NativeRootSettings(url=url, queue=queue, concurrency=concurrency)


def build_native_roots() -> dict[NativeRootKind, NativeRoot]:
    """Build separate native roots; starting their SDK lifespans remains app-owned."""
    shell, storage = resolve_native_root("shell"), resolve_native_root("storage")
    if (shell.url, shell.queue) == (storage.url, storage.queue):
        raise ValueError("Shell and storage must use independent native queue identities")
    roots: dict[NativeRootKind, NativeRoot] = {}
    configured: tuple[tuple[NativeRootKind, NativeRootSettings], ...] = (
        ("shell", shell),
        ("storage", storage),
    )
    for kind, settings in configured:
        server = FastMCP(f"CLIO {kind}", tasks=True, strict_input_validation=True)
        extension = TasksExtension(
            url=settings.url, name=settings.queue, concurrency=settings.concurrency
        )
        server.add_extension(extension)
        roots[kind] = NativeRoot(server=server, extension=extension, settings=settings)
    return roots
