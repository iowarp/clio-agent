"""fsspec GitHub reads plus authenticated, single-commit file publication."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fsspec.implementations.github import GithubFileSystem

from .models import FileEntry


class WritableGithubFileSystem(GithubFileSystem):
    """Keep upstream reads and publish selected files through one Git tree and commit."""

    def commit_files(
        self, changes: list[tuple[str, Path | bytes | None, str | None]]
    ) -> dict[str, str | None]:
        """Reject stale blobs and branch races; attribute the commit to the signed-in user."""
        if not self.token:
            raise PermissionError("Sign in to GitHub before publishing edits")
        base = f"https://api.github.com/repos/{self.org}/{self.repo}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        for path, _, _ in changes:
            FileEntry(path=path, kind="file")
            if path.startswith(".github/workflows/"):
                raise PermissionError(
                    "This connection does not have permission to change GitHub workflows"
                )
        with httpx.Client(headers=headers, timeout=30, follow_redirects=False) as client:

            def request(method: str, suffix: str, **kwargs: Any) -> Any:
                response = client.request(method, base + suffix, **kwargs)
                if response.status_code == 401:
                    raise PermissionError("GitHub sign-in expired or was revoked; sign in again")
                if response.status_code == 403:
                    raise PermissionError(
                        "GitHub refused the change. Check repository write access and branch rules."
                    )
                if response.status_code in {404, 409, 422}:
                    raise ValueError(
                        "The GitHub branch changed or cannot be edited; review again before publishing"
                    )
                response.raise_for_status()
                return response.json()

            branch = quote(str(self.root), safe="")
            head = request("GET", "/git/ref/heads/" + branch)["object"]["sha"]
            tree = request("GET", "/git/commits/" + head)["tree"]["sha"]
            listing = request("GET", "/git/trees/" + tree + "?recursive=1")
            if listing.get("truncated"):
                raise ValueError(
                    "This repository is too large to safely review a single publication"
                )
            files = {row["path"]: row for row in listing["tree"]}
            for path, _, expected in changes:
                current = files.get(path)
                if (current.get("sha") if current else None) != expected or (
                    current and current.get("mode") not in {"100644", "100755"}
                ):
                    raise ValueError("The GitHub file changed; review again before publishing")
            rows = []
            result: dict[str, str | None] = {}
            for path, content, _ in changes:
                sha = None
                if content is not None:
                    if isinstance(content, Path) and content.stat().st_size > 100 * 1024 * 1024:
                        raise ValueError("GitHub file updates must be smaller than 100 MB")
                    data = content.read_bytes() if isinstance(content, Path) else content
                    if len(data) > 100 * 1024 * 1024:
                        raise ValueError("GitHub file updates must be smaller than 100 MB")
                    sha = request(
                        "POST",
                        "/git/blobs",
                        json={
                            "encoding": "base64",
                            "content": base64.b64encode(data).decode("ascii"),
                        },
                    )["sha"]
                result[path] = sha
                rows.append(
                    {
                        "path": path,
                        "mode": files.get(path, {}).get("mode", "100644"),
                        "type": "blob",
                        "sha": sha,
                    }
                )
            updated = request("POST", "/git/trees", json={"base_tree": tree, "tree": rows})["sha"]
            commit = request(
                "POST",
                "/git/commits",
                json={"message": "Publish edits from CLIO", "tree": updated, "parents": [head]},
            )["sha"]
            # Recheck before the non-forced ref update; GitHub also rejects divergent heads.
            if request("GET", "/git/ref/heads/" + branch)["object"]["sha"] != head:
                raise ValueError("The GitHub branch changed; review again before publishing")
            request("PATCH", "/git/refs/heads/" + branch, json={"sha": commit, "force": False})
            self.invalidate_cache()
            return result

    def pipe_file(self, path: str, value: bytes, **kwargs: Any) -> None:
        """Publish one file using the same Git transaction as a reviewed batch."""
        try:
            expected = self.info(path)["sha"]
        except FileNotFoundError:
            expected = None
        self.commit_files([(path, value, expected)])

    def put_file(self, lpath: str, rpath: str, **kwargs: Any) -> None:
        """Upload one local file through the fsspec interface."""
        if Path(lpath).stat().st_size > 100 * 1024 * 1024:
            raise ValueError("GitHub file updates must be smaller than 100 MB")
        self.pipe_file(rpath, Path(lpath).read_bytes())

    def rm_file(self, path: str, **kwargs: Any) -> None:
        """Delete one existing file with a conditional commit."""
        self.commit_files([(path, None, self.info(path)["sha"])])
