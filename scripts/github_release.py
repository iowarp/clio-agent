#!/usr/bin/env python3
"""Own the GitHub release lifecycle for a clio-agent tag: draft first, publish last.

A tag push fans out into many ``clio-bundles.yml`` jobs that each upload assets.
The release used to be created as a side effect of the FIRST upload, which made
it the repository's "latest" release immediately -- for an hour or more while
the desktop bundles were still building, ``releases/latest/download/latest-lite.json``
404'd and every installed desktop's update check failed (found on v0.9.4.19).

This script splits the lifecycle into two explicit, single-owner steps:

``ensure``
    Run ONCE per tag, by the ``release`` job every uploading job ``needs``.
    Creates the release as a DRAFT (never "latest") when none exists, reuses it
    when exactly one exists (a re-run), and fails loud when more than one
    release carries the tag -- GitHub allows duplicate drafts for one tag, and
    uploads would then scatter across them.

``publish``
    Run by ``release-check`` only AFTER ``check_release_completeness.py``
    passed. Flips the draft to published and marks it latest -- unless a
    higher version is already latest (an older tag published late) or the
    tag is a pre-release (``-rc1`` style suffix). An already-published release
    is left exactly as it is: the ``workflow_dispatch`` frozen-at-tag escape
    hatch re-runs release-check on old tags, and must never steal "latest".

Every GitHub call goes through the ``gh`` CLI's ``api`` command (authenticated
by ``GH_TOKEN``), so drafts -- which the unauthenticated/tag-lookup endpoints
do not return -- are always visible.

Usage (CI)::

    python3 scripts/github_release.py ensure --tag "$TAG"
    python3 scripts/github_release.py publish --tag "$TAG"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: ``args -> stdout``: one ``gh api`` invocation. Injected so the
#: lifecycle logic is testable without the network.
GhApi = Callable[[Sequence[str]], str]

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?$")


class ReleaseError(RuntimeError):
    """A release-lifecycle invariant does not hold (duplicate drafts, bad tag, ...)."""


@dataclass(frozen=True)
class Version:
    """A parsed CLIO release tag: ``vX.Y.Z[.N][-pre]``, ordered like the updater orders it."""

    numbers: tuple[int, int, int, int]
    prerelease: str | None

    @classmethod
    def parse(cls, tag: str) -> Version:
        """Parse ``tag`` or raise :class:`ReleaseError` naming it."""

        match = _VERSION_RE.match(tag)
        if match is None:
            raise ReleaseError(f"not a CLIO release tag: {tag!r}")
        major, minor, patch, maintenance, pre = match.groups()
        return cls(
            numbers=(int(major), int(minor), int(patch), int(maintenance or 0)),
            prerelease=pre,
        )

    def sort_key(self) -> tuple[tuple[int, int, int, int], int, str]:
        """Order key: numbers first; a pre-release sorts below its release."""

        return (self.numbers, 0 if self.prerelease else 1, self.prerelease or "")


def run_gh_api(args: Sequence[str]) -> str:
    """Run ``gh api <args>`` and return stdout; raise :class:`ReleaseError` on failure."""

    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseError(f"gh api {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def find_releases(api: GhApi, repo: str, tag: str) -> list[dict[str, Any]]:
    """Return every release (drafts included) whose ``tag_name`` is ``tag``.

    Uses the authenticated list endpoint (the only one that returns drafts),
    paginated, filtered server-side by ``--jq`` to one compact JSON object per
    line.
    """

    out = api(
        [
            "--paginate",
            f"repos/{repo}/releases?per_page=100",
            "--jq",
            f".[] | select(.tag_name == {json.dumps(tag)}) "
            "| {id, tag_name, name, draft, prerelease}",
        ]
    )
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _single(releases: list[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    """Return the one release in ``releases``, ``None`` if empty; raise on duplicates."""

    if len(releases) > 1:
        ids = ", ".join(f"{r['id']} (draft={r['draft']})" for r in releases)
        raise ReleaseError(
            f"{len(releases)} releases carry tag {tag}: {ids}. Delete the stray draft(s) "
            "so every upload lands on one release."
        )
    return releases[0] if releases else None


def ensure_draft(api: GhApi, repo: str, tag: str) -> dict[str, Any]:
    """Return THE release for ``tag``, creating it as a non-latest draft if absent.

    Raises:
        ReleaseError: ``tag`` is malformed, or more than one release carries it
            (before or after creation).
    """

    Version.parse(tag)
    existing = _single(find_releases(api, repo, tag), tag)
    if existing is not None:
        return existing
    # Use the created release from the POST response itself: the list endpoint is
    # eventually consistent and can omit a draft created a moment ago (v0.9.4.20's
    # first run failed exactly that way).
    out = api(
        [
            "--method",
            "POST",
            f"repos/{repo}/releases",
            "-f",
            f"tag_name={tag}",
            "-f",
            f"name={tag}",
            "-f",
            "body=",
            "-F",
            "draft=true",
            "-f",
            "make_latest=false",
        ]
    )
    try:
        body = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"creating the draft for {tag} returned no release JSON") from exc
    if not isinstance(body, dict) or body.get("tag_name") != tag or "id" not in body:
        raise ReleaseError(f"creating the draft for {tag} returned an unexpected body")
    created = {key: body.get(key) for key in ("id", "tag_name", "name", "draft", "prerelease")}
    # A concurrent creator shows up as a second release once the listing catches up;
    # an empty listing here is only replication lag, so the POST body stands.
    listed = find_releases(api, repo, tag)
    if len(listed) > 1:
        _single(listed, tag)
    return created


def current_latest_tag(api: GhApi, repo: str) -> str | None:
    """Return the tag of the repository's current "latest" release, or ``None``."""

    try:
        out = api([f"repos/{repo}/releases/latest", "--jq", ".tag_name"])
    except ReleaseError as exc:
        if "HTTP 404" in str(exc) or "Not Found" in str(exc):
            return None
        raise
    return out.strip() or None


def should_mark_latest(tag: str, latest_tag: str | None) -> bool:
    """Decide whether publishing ``tag`` should make it the repository's latest release.

    A pre-release never becomes latest. Otherwise ``tag`` becomes latest unless
    the current latest is a strictly higher version (an older tag published
    after a newer one must not roll every installed desktop back).
    """

    version = Version.parse(tag)
    if version.prerelease:
        return False
    if latest_tag is None:
        return True
    return version.sort_key() >= Version.parse(latest_tag).sort_key()


@dataclass(frozen=True)
class PublishResult:
    """What :func:`publish` did, for the CI log and ``$GITHUB_OUTPUT``."""

    published: bool
    made_latest: bool
    detail: str


def publish(api: GhApi, repo: str, tag: str) -> PublishResult:
    """Publish ``tag``'s draft (marking it latest when appropriate); no-op if already public.

    Raises:
        ReleaseError: no release carries ``tag``, duplicates do, or the publish
            did not take (the release is still a draft, or latest did not move).
    """

    release = _single(find_releases(api, repo, tag), tag)
    if release is None:
        raise ReleaseError(f"no release carries tag {tag}; nothing to publish")
    if not release["draft"]:
        return PublishResult(
            published=False,
            made_latest=False,
            detail=f"{tag} is already published; its draft/latest state is left untouched",
        )

    latest_tag = current_latest_tag(api, repo)
    make_latest = should_mark_latest(tag, latest_tag)
    prerelease = Version.parse(tag).prerelease is not None
    api(
        [
            "--method",
            "PATCH",
            f"repos/{repo}/releases/{release['id']}",
            "-F",
            "draft=false",
            "-F",
            f"prerelease={'true' if prerelease else 'false'}",
            "-f",
            f"make_latest={'true' if make_latest else 'false'}",
        ]
    )

    after = _single(find_releases(api, repo, tag), tag)
    if after is None or after["draft"]:
        raise ReleaseError(f"published {tag} but it still reads back as a draft")
    if make_latest:
        now_latest = current_latest_tag(api, repo)
        if now_latest != tag:
            raise ReleaseError(f"published {tag} as latest but latest is {now_latest!r}")
        return PublishResult(True, True, f"published {tag} and marked it latest")
    reason = "pre-release" if prerelease else f"{latest_tag} is a higher version"
    return PublishResult(True, False, f"published {tag}; NOT marked latest ({reason})")


def _write_github_output(values: dict[str, str]) -> None:
    """Append ``key=value`` lines to ``$GITHUB_OUTPUT`` when running in Actions."""

    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None, api: GhApi = run_gh_api) -> int:
    """CLI entry point. Returns 0 on success, 1 on a violated release invariant."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["ensure", "publish"])
    parser.add_argument("--tag", required=True, help="Release tag, e.g. v0.9.4.19")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name (defaults to $GITHUB_REPOSITORY)",
    )
    args = parser.parse_args(argv)
    if not args.repo:
        print("FAIL: --repo not given and $GITHUB_REPOSITORY is unset", file=sys.stderr)
        return 1

    try:
        if args.command == "ensure":
            release = ensure_draft(api, args.repo, args.tag)
            state = "draft" if release["draft"] else "published"
            print(f"OK: release {args.tag} (id {release['id']}) exists as {state}")
            _write_github_output({"release_id": str(release["id"]), "state": state})
        else:
            result = publish(api, args.repo, args.tag)
            print(f"OK: {result.detail}")
            _write_github_output(
                {
                    "published": str(result.published).lower(),
                    "made_latest": str(result.made_latest).lower(),
                }
            )
    except ReleaseError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
