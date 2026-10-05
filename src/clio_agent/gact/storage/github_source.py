"""Reuse fsspec reads and the shared review workflow for GitHub working copies."""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

import httpx
from clio_schemas.connected_resources import SourceCapabilities

from clio_agent.gact.storage.linked import FsspecFolder
from clio_agent.gact.storage.models import FileEntry


class GitHubSignInRequired(PermissionError):
    """GitHub rejected the user token during a write request."""


class GitHubSource(FsspecFolder):
    """Publish reviewed files with GitHub's conditional Contents API as the signed-in user."""

    capabilities = SourceCapabilities(
        link_folder=True,
        download=False,
        search=True,
        revision_check=True,
        conditional_write=True,
        writable_folder=True,
        supported_modes=["read_only", "working_copy", "write_enabled"],
        unavailable_reasons={},
    )

    def __init__(self, fs: Any, root: str, *, writable: bool) -> None:
        super().__init__(fs, root)
        self.writable = writable

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Create, replace or delete one reviewed file without overriding upstream changes."""
        try:
            return self._apply(path, content, expected)
        except httpx.HTTPError as exc:
            raise ValueError(
                "GitHub could not be reached; inspect the repository before retrying"
            ) from exc

    def _apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        return self.apply_many([(path, content, expected)])[path]

    def apply_many(
        self, changes: list[tuple[str, Path | None, str | None]]
    ) -> dict[str, str | None]:
        """Publish the selected paths together as one user-authored commit."""
        if not self.writable or not self.fs.token:
            raise PermissionError("This GitHub link does not allow publishing edits")
        for path, _, _ in changes:
            FileEntry(path=path, kind="file")
        updates = [
            (posixpath.join(self.root, path), content, expected)
            for path, content, expected in changes
        ]
        result = self.fs.commit_files(updates)
        return {path: result[posixpath.join(self.root, path)] for path, _, _ in changes}

    @staticmethod
    def _checked(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code == 401:
            raise GitHubSignInRequired("GitHub sign-in expired or was revoked; sign in again")
        if response.status_code == 403:
            raise PermissionError(
                "GitHub refused this change. Check repository write access, branch rules, and the app's Contents permission."
            )
        if response.status_code in {409, 422}:
            raise ValueError(
                "GitHub could not save this change. The file or branch may have changed; review again."
            )
        if response.status_code == 404:
            raise ValueError(
                "Select an accessible GitHub branch, not a tag or commit, to save changes"
            )
        raise ValueError(
            "GitHub could not save this change; try again when the service is available"
        )
