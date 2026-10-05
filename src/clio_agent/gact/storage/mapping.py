"""Check resource permissions without modifying remote data."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from .globus import GlobusSource
from .linked import linked_adapter
from .models import SourceRecord

if TYPE_CHECKING:
    from .service import StorageService


def mapping_options(service: StorageService, record: SourceRecord) -> dict[str, Any]:
    """Report connector support separately from the resource's known write permission."""
    provider = record.source.provider
    allowed: bool | None = None
    reason = "Your account must have permission to edit each file. The source checks permission when publishing."
    if record.origin == "desktop_upload" or (
        provider == "sftp"
        and record.configuration.target_id
        and record.configuration.ssh_origin != "clio"
    ):
        return {
            "link_access": [],
            "reason": "This connection supports downloaded copies.",
            "write_permission": False,
        }
    if provider == "local":
        allowed = os.access(record.source.root, os.W_OK)
    elif provider in {"github", "google_drive"} and not service.auth.connected(record):
        allowed = False
        reason = "Sign in with an account that can edit this folder to enable publishing."
    elif provider == "github":
        with linked_adapter(service, record) as adapter:
            fs = adapter.fs
            base = f"https://api.github.com/repos/{fs.org}/{fs.repo}"
            with httpx.Client(
                headers={"Authorization": f"Bearer {fs.token}"}, timeout=30, follow_redirects=False
            ) as client:
                repo = client.get(base)
                repo.raise_for_status()
                allowed = bool(repo.json().get("permissions", {}).get("push"))
                if allowed:
                    branch = client.get(base + "/git/ref/heads/" + quote(str(fs.root), safe=""))
                    allowed = branch.status_code == 200
            reason = "Publishing requires an editable branch and repository write access. Tags and commits are read only."
    elif provider == "google_drive":
        with linked_adapter(service, record) as adapter:
            info = adapter.fs.files.get(
                fileId=record.source.root,
                fields="capabilities(canEdit,canAddChildren)",
                supportsAllDrives=True,
            ).execute()
            permissions = info.get("capabilities", {})
            allowed = bool(permissions.get("canEdit") and permissions.get("canAddChildren"))
            reason = "This Google Drive folder is shared with view-only access."
    elif provider == "globus":
        with service.adapter(record) as adapter:
            if not isinstance(adapter, GlobusSource):
                raise ValueError("The Globus connection changed")
            info = adapter.client.operation_stat(
                record.configuration.collection_id, path=record.source.root
            )
            permissions = str(info.get("permissions", ""))
            if permissions and not int(permissions, 8) & 0o222:
                allowed = False
                reason = "This collection folder is read only."
    elif provider == "sftp":
        with linked_adapter(service, record) as adapter:
            info = adapter.fs.info(adapter.root)
            mode = info.get("mode")
            if isinstance(mode, int) and not mode & 0o222:
                allowed = False
                reason = "This SFTP folder is read only."
    if allowed is True:
        reason = "File permissions and source changes are checked again when publishing."
    modes = ["read_only"] if allowed is False else ["read_only", "publish_later", "write_through"]
    return {"link_access": modes, "write_permission": allowed, "reason": reason}
