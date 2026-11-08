"""Locate Clio Coder, give each instance a scoped home, and spawn it as an owned process tree."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from clio_agent.errors import ClioError

#: Environment variable naming the Clio Coder entry point: an executable or a ``dist/cli/index.js``.
CLIO_CODER_BIN_ENV = "CLIO_AGENT_CLIO_CODER_BIN"

#: Typed failure reasons, carried as ``ClioCoderError.error_type``.
REASON_NOT_FOUND = "clio_coder_not_found"
REASON_HOME_REFUSED = "clio_coder_home_refused"
REASON_SPAWN_FAILED = "clio_coder_spawn_failed"
REASON_PROTOCOL = "clio_coder_protocol_error"
REASON_WORKSPACE = "clio_coder_workspace_error"

#: Parent credentials and control tokens that must never reach a delegated coding agent.
_STRIPPED_ENV_KEYS = ("CLIO_AUTH_TOKEN", "CLIO_GACT_BEARER_TOKEN")
_STRIPPED_ENV_PREFIXES = ("CLIO_SECRET_", "CLIO_CODER_")

#: Grace period between the polite stop signal and killing the owned tree.
STOP_GRACE_SECONDS = 5.0


class ClioCoderError(ClioError):
    """A delegation to Clio Coder could not start, talk or finish; ``error_type`` says why."""

    def __init__(self, reason: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type=reason, details=details)


@dataclass(frozen=True)
class ClioCoderInstall:
    """How to launch Clio Coder: the argv prefix that precedes a subcommand."""

    argv: tuple[str, ...]

    @classmethod
    def resolve(cls, explicit: str | None = None) -> ClioCoderInstall:
        """Use ``explicit``, then ``$CLIO_AGENT_CLIO_CODER_BIN``, then ``clio-coder`` on PATH."""
        candidate = explicit or os.environ.get(CLIO_CODER_BIN_ENV) or shutil.which("clio-coder")
        if not candidate:
            raise ClioCoderError(
                REASON_NOT_FOUND,
                f"Clio Coder was not found: install clio-coder or set {CLIO_CODER_BIN_ENV}",
            )
        path = Path(candidate)
        if path.suffix in {".js", ".mjs", ".cjs"}:
            node = shutil.which("node")
            if node is None or not path.is_file():
                raise ClioCoderError(
                    REASON_NOT_FOUND,
                    "Clio Coder entry point needs node and an existing script",
                    {"entry": candidate, "node": node},
                )
            return cls((node, str(path.resolve())))
        return cls((candidate,))


@dataclass(frozen=True)
class ClioCoderTarget:
    """The model route an instance uses; the API key is read by Clio Coder from ``api_key_env``."""

    model: str
    target_id: str = "default"
    runtime: str = "openai-compatible"
    url: str | None = None
    api_key_env: str | None = None
    thinking: str = "off"

    def settings(self) -> dict[str, Any]:
        """Render the version-2 Clio Coder settings document for this one target."""
        target: dict[str, Any] = {
            "id": self.target_id,
            "runtime": self.runtime,
            "defaultModel": self.model,
        }
        if self.url:
            target["url"] = self.url
        if self.api_key_env:
            target["auth"] = {"apiKeyEnvVar": self.api_key_env}
        route = {"target": self.target_id, "model": self.model, "thinkingLevel": self.thinking}
        return {"version": 2, "targets": [target], "chat": dict(route), "fleet": {"default": route}}


def _default_clio_coder_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home,
        home / ".config" / "clio-coder",
        home / ".local" / "share" / "clio-coder",
        home / ".local" / "state" / "clio-coder",
        home / ".cache" / "clio-coder",
    )


@dataclass(frozen=True)
class InstanceHome:
    """A scoped Clio Coder home: every config, data, state, cache and temp write lands here."""

    root: Path

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ClioCoderError(REASON_HOME_REFUSED, "instance home must be an absolute path")
        resolved = self.root.resolve()
        for real in _default_clio_coder_roots():
            real = real.resolve()
            if resolved == real or (real != Path.home().resolve() and real in resolved.parents):
                raise ClioCoderError(
                    REASON_HOME_REFUSED,
                    "instance home must not be the operator's real Clio Coder home",
                    {"root": str(resolved)},
                )

    def dirs(self) -> dict[str, Path]:
        return {name: self.root / name for name in ("config", "data", "state", "cache", "tmp")}

    def prepare(self, target: ClioCoderTarget | None, *, template: Path | None = None) -> None:
        """Create the home; seed it from ``template`` or write settings for ``target``."""
        if template is not None and not (self.root / "config").exists():
            shutil.copytree(template, self.root, dirs_exist_ok=True)
        for path in self.dirs().values():
            path.mkdir(parents=True, exist_ok=True)
        settings = self.root / "config" / "settings.yaml"
        if target is not None and not settings.exists():
            settings.write_text(yaml.safe_dump(target.settings(), sort_keys=False), "utf-8")

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child environment: parent minus secrets and stray Clio Coder overrides, plus home."""
        env = {
            key: value
            for key, value in (os.environ if base is None else base).items()
            if key not in _STRIPPED_ENV_KEYS and not key.startswith(_STRIPPED_ENV_PREFIXES)
        }
        dirs = self.dirs()
        env.update(
            {
                "CLIO_CODER_HOME": str(self.root),
                "CLIO_CODER_CONFIG_DIR": str(dirs["config"]),
                "CLIO_CODER_DATA_DIR": str(dirs["data"]),
                "CLIO_CODER_STATE_DIR": str(dirs["state"]),
                "CLIO_CODER_CACHE_DIR": str(dirs["cache"]),
                # Never read the operator's other agent installs (Claude, Codex, Pi homes).
                "CLIO_CODER_INTEROP_HOME": "none",
                "TMPDIR": str(dirs["tmp"]),
                "TMP": str(dirs["tmp"]),
                "TEMP": str(dirs["tmp"]),
            }
        )
        return env


@dataclass
class SpawnedInstance:
    """A running Clio Coder process plus the process tree clio-agent owns for it."""

    process: asyncio.subprocess.Process
    tree: Any
    confinement: dict[str, Any] = field(default_factory=dict)

    async def stop(self, grace: float = STOP_GRACE_SECONDS) -> int | None:
        """Ask the tree to stop, then kill it; returns the exit code (``None`` if unknown)."""
        if self.process.returncode is None:
            if sys.platform != "win32":
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(self.process.wait(), grace)
            except TimeoutError:
                self.tree.terminate()
                await self.process.wait()
        # Descendants that outlived the leader (a stray bash child) are part of the owned tree.
        if self.tree.active():
            self.tree.terminate()
        self.tree.close()
        return self.process.returncode


async def spawn_instance(
    install: ClioCoderInstall,
    args: Sequence[str],
    *,
    cwd: Path,
    home: InstanceHome,
    stdin_pipe: bool,
    base_env: Mapping[str, str] | None = None,
) -> SpawnedInstance:
    """Spawn Clio Coder through the sandbox composer, as a fresh cancellable process tree."""
    from clio_agent.runtime import sandbox
    from clio_agent.runtime.task_process_tree import TaskProcessTree, task_spawn_options

    argv = [*install.argv, *args]
    try:
        confined = sandbox.wrap_confined(
            argv[0],
            argv[1:],
            write_roots=(cwd, home.root),
            net_policy=sandbox.NET_ALLOW_RECORD,
            profile=sandbox.PROFILE_SHELL,
            cwd=cwd,
        )
    except sandbox.SandboxCompositionError as exc:
        raise ClioCoderError(REASON_SPAWN_FAILED, f"confinement failed: {exc}") from exc
    env = {**home.environment(base_env), **confined.env_overlay}
    try:
        process = await asyncio.create_subprocess_exec(
            confined.command,
            *confined.args,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_pipe else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            **task_spawn_options(confined.popen_kwargs),
        )
    except OSError as exc:
        raise ClioCoderError(REASON_SPAWN_FAILED, f"could not start Clio Coder: {exc}") from exc
    return SpawnedInstance(
        process=process,
        tree=TaskProcessTree(process.pid),
        confinement={
            "mechanism": confined.result.mechanism,
            "active": confined.result.active,
            "reason": confined.result.reason,
        },
    )
