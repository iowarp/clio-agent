"""Install and verify the official Obsidian client inside CLIO's managed environment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock

from clio_agent import paths
from clio_agent.runtime.document_stack.node_paths import node_path

CLIENT_VERSION = "0.0.14"
CLIENT_FILES = Path(__file__).with_name("obsidian_client")


class ObsidianClientError(ValueError):
    """A bounded, safe failure from managed installation or official client execution."""

    def __init__(self, message: str, *, code: str = "obsidian_client_failed") -> None:
        super().__init__(message)
        self.code = code


def _hidden_process_options() -> dict[str, Any]:
    """Keep Windows client processes hidden while preserving portable subprocess options."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def prepare_client_environment(workspace: Path) -> dict[str, str]:
    """Prepare managed Node in an isolated installer, without widening agent permissions."""
    environment = {**os.environ, "CLIO_ALLOWED_ROOTS": str(workspace.resolve())}
    flags = _hidden_process_options()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "clio_agent.runtime.obsidian_node", str(workspace)],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            check=False,
            **flags,
        )
        if result.returncode:
            raise ObsidianClientError("CLIO's managed Node.js environment could not be prepared.")
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("node"), str):
            raise ValueError("Invalid managed Node.js inventory")
        return {"node": data["node"]}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ObsidianClientError(
            "CLIO's managed Node.js environment could not be prepared."
        ) from exc


class ManagedObsidianClient:
    """Own a pinned npm installation while reusing CLIO's locked Node.js environment."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.user_cache_dir() / "managed-clients" / "obsidian"
        self.project = self.root / CLIENT_VERSION

    def command(self) -> list[str]:
        """Return the verified private client, never a user's global ob executable."""
        try:
            receipt = json.loads((self.project / "installed.json").read_text(encoding="utf-8"))
            node = Path(receipt["node"])
            entry = self.project / "node_modules" / "obsidian-headless" / "cli.js"
            if not node.is_file() or not entry.is_file() or receipt["version"] != CLIENT_VERSION:
                raise ValueError("Incomplete installation")
            for name in ("package.json", "package-lock.json"):
                cached = json.loads((self.project / name).read_text(encoding="utf-8"))
                shipped = json.loads((CLIENT_FILES / name).read_text(encoding="utf-8"))
                if cached != shipped:
                    raise ValueError("Changed managed installation")
            return [node_path(str(node)), node_path(str(entry))]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ObsidianClientError(
                "Install the Obsidian Sync client in CLIO first.", code="client_not_installed"
            ) from exc

    def installed(self) -> bool:
        """Inspect the installation without downloads or account access."""
        try:
            self.command()
            return True
        except ObsidianClientError:
            return False

    def install(self, progress: Callable[[str], None]) -> None:
        """Install the locked official package and verify its native SQLite dependency."""
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / "install.lock"), timeout=600):
            progress("Preparing CLIO's managed Node.js environment…")
            environment_root = self.root / "environment"
            environment_root.mkdir(parents=True, exist_ok=True)
            runtime = prepare_client_environment(environment_root)
            node = node_path(str(runtime["node"]))
            node_root = Path(node).parent
            if node_root.name == "bin":
                node_root = node_root.parent
            npm = node_root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
            if not npm.is_file():
                raise ObsidianClientError("CLIO's Node.js environment needs repair.")
            self.project.mkdir(parents=True, exist_ok=True)
            (self.project / "installed.json").unlink(missing_ok=True)
            for name in ("package.json", "package-lock.json"):
                (self.project / name).write_bytes((CLIENT_FILES / name).read_bytes())
            progress("Installing the official Obsidian Sync client…")
            environment = {
                **os.environ,
                "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", ""),
            }
            flags = _hidden_process_options()
            try:
                installed = subprocess.run(
                    [node, node_path(str(npm)), "ci", "--no-audit", "--no-fund"],
                    cwd=self.project,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=600,
                    check=False,
                    **flags,
                )
                if installed.returncode:
                    raise ObsidianClientError(
                        "The official client installation failed. Retry installation."
                    )
                progress("Verifying the client and its database…")
                verified = subprocess.run(
                    [
                        node,
                        "-e",
                        "require('better-sqlite3')(':memory:').close(); require('obsidian-headless/package.json')",
                    ],
                    cwd=self.project,
                    env=environment,
                    capture_output=True,
                    timeout=30,
                    check=False,
                    **flags,
                )
                if verified.returncode:
                    raise ObsidianClientError(
                        "The installed client could not start. Retry installation."
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ObsidianClientError(
                    "The client installation could not finish. Retry installation."
                ) from exc
            (self.project / "installed.json").write_text(
                json.dumps({"version": CLIENT_VERSION, "node": node}), encoding="utf-8"
            )
            progress("Obsidian Sync client is ready.")

    def run(
        self,
        args: list[str],
        *,
        environment: dict[str, str],
        secrets: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        """Run the official CLI with private stdin and no secret-bearing process argv."""
        command = self.command()
        payload = json.dumps({"args": args, "secrets": secrets or {}})
        flags = _hidden_process_options()
        try:
            return subprocess.run(
                [command[0], node_path(str(CLIENT_FILES / "launch.mjs")), command[1]],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                cwd=self.project,
                timeout=timeout,
                check=False,
                **flags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ObsidianClientError(
                "The Obsidian client could not finish. Retry the operation."
            ) from exc
