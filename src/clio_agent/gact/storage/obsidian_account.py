"""Trusted account and managed-copy operations for the official Obsidian client."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from clio_agent.runtime.obsidian_client_runtime import (
    CLIENT_VERSION,
    ManagedObsidianClient,
    ObsidianClientError,
)


class ObsidianAccount:
    """Keep account credentials private and account setup separate from workspace access."""

    def __init__(self, private_root: Path, vaults_root: Path) -> None:
        self.private_root = private_root
        self.vaults_root = vaults_root
        self.client = ManagedObsidianClient()
        self._lock = threading.RLock()
        self._account_lock = threading.RLock()
        self._installer: threading.Thread | None = None
        self._installation: dict[str, str] = {"state": "idle", "message": ""}

    def environment(self) -> dict[str, str]:
        """Give the official client a CLIO-owned home, independent of personal ob state."""
        home = self.private_root / "home"
        config = self.private_root / "config"
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = dict(os.environ)
        # An ambient token must never select a different account than this CLIO login.
        environment.pop("OBSIDIAN_AUTH_TOKEN", None)
        environment.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(config))
        return environment

    @property
    def token_path(self) -> Path:
        """Return the official client's private token location without reading its value."""
        return (
            self.private_root / "config" / "obsidian-headless" / "auth_token"
            if sys.platform == "linux"
            else self.private_root / "home" / ".obsidian-headless" / "auth_token"
        )

    def status(self) -> dict[str, Any]:
        """Expose installation progress and sign-in presence, never client output or secrets."""
        with self._lock:
            ready = self.client.installed()
            state = self._installation["state"]
            return {
                "installed": ready,
                "version": CLIENT_VERSION if ready else "",
                "state": state
                if state in {"installing", "failed"}
                else "ready"
                if ready
                else "missing",
                "message": self._installation["message"],
                "authenticated": ready and self.token_path.is_file(),
            }

    def install(self) -> dict[str, Any]:
        """Start one supervised installation; polling does not restart it."""
        with self._lock:
            if self._installation["state"] == "installing":
                return self.status()
            if self.client.installed() and self._installation["state"] != "failed":
                return self.status()
            self._installation = {"state": "installing", "message": "Preparing Obsidian Sync…"}

        def progress(message: str) -> None:
            with self._lock:
                self._installation["message"] = message

        def perform() -> None:
            try:
                with self._account_lock:
                    self.client.install(progress)
            except (ObsidianClientError, OSError, ValueError, TimeoutError, RuntimeError):
                with self._lock:
                    self._installation = {
                        "state": "failed",
                        "message": "Installation failed. Retry to repair the managed client.",
                    }
            else:
                with self._lock:
                    self._installation = {
                        "state": "ready",
                        "message": "Obsidian Sync client is ready.",
                    }

        self._installer = threading.Thread(
            target=perform, name="clio-obsidian-install", daemon=True
        )
        self._installer.start()
        return self.status()

    def login(self, email: str, password: str, mfa: str = "") -> dict[str, Any]:
        """Pass credentials privately to the official login command, including optional MFA."""
        with self._account_lock:
            result = self.client.run(
                ["login"],
                environment=self.environment(),
                secrets={"email": email, "password": password, "mfa": mfa},
            )
            if result.returncode:
                if "2FA" in result.stderr or "MFA" in result.stderr:
                    raise ObsidianClientError(
                        "Enter the current two-factor authentication code.", code="mfa_required"
                    )
                raise ObsidianClientError(
                    "Obsidian sign-in failed. Check your account details.", code="sign_in_failed"
                )
            if not self.token_path.is_file():
                raise ObsidianClientError("The official client did not save the sign-in.")
            return self.status()

    def logout(self) -> dict[str, Any]:
        """Sign out only this managed client, preserving vault contents."""
        with self._account_lock:
            result = self.client.run(["logout"], environment=self.environment())
            if result.returncode:
                raise ObsidianClientError("Obsidian could not sign out. Retry the operation.")
            return self.status()

    def vaults(self) -> list[dict[str, str]]:
        """List selectable vault identities without exposing encryption or account material."""
        result = self.client.run(["sync-list-remote", "--json"], environment=self.environment())
        if result.returncode:
            raise ObsidianClientError(
                "Could not list Sync vaults. Check your sign-in and Obsidian Sync subscription."
            )
        try:
            payload = json.loads(result.stdout)
            return [
                {"id": str(row["id"]), "name": str(row["name"])}
                for row in [*payload.get("vaults", []), *payload.get("shared", [])]
            ]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ObsidianClientError(
                "The official client returned an invalid vault list."
            ) from exc

    def setup(self, vault_id: str, password: str = "") -> dict[str, str]:
        """Configure a dedicated CLIO copy; knowledge access is approved separately."""
        with self._account_lock:
            if not vault_id or len(vault_id) > 256:
                raise ValueError("Select an Obsidian Sync vault")
            # Stable ownership prevents a second setup from choosing an unrelated copy.
            root = self.vaults_root / ("sync-" + hashlib.sha256(vault_id.encode()).hexdigest()[:24])
            root.mkdir(parents=True, exist_ok=True)
            (root / ".obsidian").mkdir(exist_ok=True)
            result = self.client.run(
                ["sync-setup", "--vault", vault_id, "--path", str(root), "--json"],
                environment=self.environment(),
                secrets={"password": password},
            )
            if result.returncode:
                raise ObsidianClientError(
                    "Could not unlock this Sync vault. Check its encryption password.",
                    code="vault_setup_failed",
                )
            return {"root": str(root)}
