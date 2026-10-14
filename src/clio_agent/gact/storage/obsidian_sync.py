"""Supervise the official Headless Sync client without handling account credentials."""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Any

from clio_agent import paths
from clio_agent.gact.storage.models import SourceRecord
from clio_agent.gact.storage.obsidian import ObsidianSource
from clio_agent.gact.storage.obsidian_account import ObsidianAccount
from clio_agent.runtime.obsidian_client_runtime import ManagedObsidianClient


def client_command() -> list[str]:
    """Select CLIO's managed official client, independent of the user's PATH."""
    return ManagedObsidianClient().command()


class ObsidianSync:
    """Own source-scoped sync processes, independently from agent edit permissions."""

    def __init__(self, account: ObsidianAccount | None = None) -> None:
        self.account = account or ObsidianAccount(
            paths.user_config_dir() / "storage-auth" / "obsidian",
            paths.user_data_dir() / "knowledge-vaults",
        )
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()

    def status(self, record: SourceRecord) -> dict[str, Any]:
        """Report process state only; account details and client output stay private."""
        with self._lock:
            process = self._processes.get(record.source.id)
            code = process.poll() if process else None
            return {
                "state": "running"
                if process and code is None
                else "failed"
                if process and code
                else "stopped",
                "mode": record.configuration.obsidian_sync_mode,
                "connection": record.source.connection,
                "error": "Obsidian Sync stopped. Check the account in Settings → Data sources."
                if process and code
                else None,
            }

    def start(self, record: SourceRecord) -> dict[str, Any]:
        """Apply the user's sync direction and launch a configured dedicated vault copy."""
        if not record.connected or record.source.connection != "headless_sync":
            raise ValueError("Select a connected Headless Sync vault")
        command = client_command()
        # The official client syncs the enclosing copy. Knowledge reads and edits
        # stay confined to source.root, which can be a selected vault subfolder.
        sync_root = str(ObsidianSource(record.source.root).vault_root)
        with self._lock:
            if self.status(record)["state"] == "running":
                return self.status(record)
            environment = self.account.environment()
            checked = subprocess.run(
                [*command, "sync-status", "--path", sync_root],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                env=environment,
            )
            if checked.returncode:
                raise ValueError("Connect this Sync vault through CLIO's Obsidian setup first.")
            configured = subprocess.run(
                [
                    *command,
                    "sync-config",
                    "--path",
                    sync_root,
                    "--mode",
                    record.configuration.obsidian_sync_mode,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                env=environment,
            )
            if configured.returncode:
                raise ValueError("The official client could not configure this vault's sync mode")
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW
            self._processes[record.source.id] = subprocess.Popen(
                [*command, "sync", "--path", sync_root, "--continuous"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                env=environment,
            )
            return self.status(record)

    def stop(self, source_id: str) -> None:
        """Stop the owned process and reap it; leave the user's configured copy intact."""
        with self._lock:
            process = self._processes.pop(source_id, None)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def shutdown(self) -> None:
        """Reap all supervised clients when CLIO shuts down."""
        for source_id in list(self._processes):
            self.stop(source_id)
