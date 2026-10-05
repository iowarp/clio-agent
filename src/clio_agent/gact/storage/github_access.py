"""Bounded, credential-separated caches around fsspec's GitHub filesystem."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Iterator, Literal

import requests

from clio_agent.gact.storage.github_filesystem import WritableGithubFileSystem as GithubFileSystem


class GitHubAccess:
    """Keep fsspec listings between reads and pause all GitHub reads after a rate limit."""

    def __init__(self) -> None:
        self._filesystems: OrderedDict[
            tuple[str, str, str, str], tuple[float, GithubFileSystem]
        ] = OrderedDict()
        self._lock = threading.RLock()
        self._retry_at = 0.0
        self._backoff = 60.0
        self._rate_limits: dict[str, tuple[float, float]] = {}
        self._metadata: OrderedDict[tuple[str, str, str, int, str], tuple[float, Any]] = (
            OrderedDict()
        )

    def clear(self) -> None:
        """Drop cached authenticated clients when the user signs out."""
        with self._lock:
            self._filesystems.clear()
            self._metadata.clear()
            self._rate_limits.clear()
            self._retry_at, self._backoff = 0, 60

    def _limited(self) -> ValueError:
        minutes = max(1, math.ceil((self._retry_at - time.time()) / 60))
        duration = f"{minutes} minute" + ("s" if minutes != 1 else "")
        return ValueError(
            f"GitHub is temporarily limiting requests. Try again in about {duration}. "
            "Your saved sources are still available."
        )

    @contextmanager
    def filesystem(
        self, org: str, repo: str, ref: str, *, refresh: bool = False, token: str | None = None
    ) -> Iterator[GithubFileSystem]:
        """Reuse library caches for five minutes; explicit refresh clears their listings."""
        credential = hashlib.sha256(token.encode()).hexdigest() if token else "anonymous"
        key = (org, repo, ref, credential)
        with self._access(credential):
            cached = self._filesystems.get(key)
            if cached is None or cached[0] <= time.monotonic():
                fs = GithubFileSystem(
                    org=org,
                    repo=repo,
                    sha=ref or None,
                    timeout=30,
                    skip_instance_cache=True,
                    username="x-access-token" if token else None,
                    token=token,
                )
                self._filesystems[key] = (time.monotonic() + 300, fs)
            else:
                fs = cached[1]
                if refresh:
                    fs.invalidate_cache()
            self._filesystems.move_to_end(key)
            while len(self._filesystems) > 16:
                self._filesystems.popitem(last=False)
            yield fs

    def metadata(
        self,
        org: str,
        repo: str,
        resource: Literal["", "branches", "tags", "commits"] = "",
        *,
        page: int = 1,
        token: str | None = None,
    ) -> Any:
        """Cache one bounded revision-list page using the same account rate-limit gate."""
        credential = hashlib.sha256(token.encode()).hexdigest() if token else "anonymous"
        key = (org, repo, resource, page, credential)
        with self._access(credential):
            cached = self._metadata.get(key)
            if cached is not None and cached[0] > time.monotonic():
                self._metadata.move_to_end(key)
                return cached[1]
            headers = {"Accept": "application/vnd.github+json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            response = requests.get(
                f"https://api.github.com/repos/{org}/{repo}" + (f"/{resource}" if resource else ""),
                headers=headers,
                params={"per_page": 100, "page": page} if resource else {},
                timeout=30,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise ValueError("This repository moved. Paste its current GitHub address.")
            response.raise_for_status()
            value = response.json()
            self._metadata[key] = (time.monotonic() + 300, value)
            self._metadata.move_to_end(key)
            while len(self._metadata) > 128:
                self._metadata.popitem(last=False)
            return value

    @contextmanager
    def _access(self, credential: str) -> Iterator[None]:
        # Serialize mutable caches and prevent duplicate reads across concurrent callers.
        with self._lock:
            self._retry_at, self._backoff = self._rate_limits.get(credential, (0, 60))
            if time.time() < self._retry_at:
                raise self._limited()
            try:
                yield
                self._backoff = 60.0
            except requests.HTTPError as exc:
                response = exc.response
                if (
                    response is None
                    or response.status_code not in {403, 429}
                    or (
                        response.status_code != 429
                        and response.headers.get("x-ratelimit-remaining") != "0"
                        and "rate limit" not in response.text.lower()
                    )
                ):
                    raise
                now = time.time()
                deadlines = [now + self._backoff]
                for name, absolute in (("retry-after", False), ("x-ratelimit-reset", True)):
                    try:
                        value = float(response.headers.get(name, ""))
                    except ValueError:
                        continue
                    if math.isfinite(value):
                        deadlines.append(value if absolute else now + value)
                self._retry_at = max(deadlines)
                self._backoff = min(self._backoff * 2, 3600)
                self._rate_limits[credential] = (self._retry_at, self._backoff)
                raise self._limited() from exc
