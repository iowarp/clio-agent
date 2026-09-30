"""Tests for the draft-first release lifecycle (``scripts/github_release.py``).

A release must not be "latest" until it is complete: the v0.9.4.19 tag build
made the release latest on its first asset upload, and for over an hour
``releases/latest/download/latest-lite.json`` 404'd for every installed
desktop. These tests drive the lifecycle against an in-memory fake of the
GitHub releases API (the only fake is the ``gh api`` transport; every
decision under test is the real code).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts.github_release import (
    ReleaseError,
    Version,
    ensure_draft,
    main,
    publish,
    should_mark_latest,
)

REPO = "iowarp/clio-agent"


class FakeGitHub:
    """In-memory GitHub releases API speaking the exact ``gh api`` argv the script sends."""

    def __init__(self, releases: list[dict[str, Any]] | None = None) -> None:
        self.releases: list[dict[str, Any]] = list(releases or [])
        self.latest_id: int | None = None
        self.calls: list[list[str]] = []
        self._next_id = 1000
        # Test hooks for failure injection.
        self.ignore_publish = False
        self.ignore_make_latest = False
        self.create_hook: Any = None

    def add(self, tag: str, *, draft: bool, latest: bool = False) -> dict[str, Any]:
        """Seed one release."""
        self._next_id += 1
        release = {
            "id": self._next_id,
            "tag_name": tag,
            "name": tag,
            "draft": draft,
            "prerelease": False,
        }
        self.releases.append(release)
        if latest:
            self.latest_id = release["id"]
        return release

    @staticmethod
    def _fields(args: Sequence[str]) -> dict[str, str]:
        fields: dict[str, str] = {}
        for flag, value in zip(args, args[1:], strict=False):
            if flag in {"-f", "-F"}:
                key, _, val = value.partition("=")
                fields[key] = val
        return fields

    def __call__(self, args: Sequence[str]) -> str:
        args = list(args)
        self.calls.append(args)
        if args[0] == "--paginate":
            assert args[1] == f"repos/{REPO}/releases?per_page=100"
            tag = json.loads(args[3].split("==", 1)[1].split(")", 1)[0].strip())
            return "".join(
                json.dumps({k: r[k] for k in ("id", "tag_name", "name", "draft", "prerelease")})
                + "\n"
                for r in self.releases
                if r["tag_name"] == tag and r["id"] not in getattr(self, "hidden_ids", set())
            )
        if args[0] == f"repos/{REPO}/releases/latest":
            for release in self.releases:
                if release["id"] == self.latest_id:
                    return release["tag_name"] + "\n"
            raise ReleaseError("gh api repos/.../releases/latest failed: gh: Not Found (HTTP 404)")
        if args[:3] == ["--method", "POST", f"repos/{REPO}/releases"]:
            fields = self._fields(args)
            assert fields["draft"] == "true"
            assert fields["make_latest"] == "false"
            assert fields["name"] == fields["tag_name"]  # title is the bare version
            if self.create_hook is not None:
                self.create_hook(self)
            created = self.add(fields["tag_name"], draft=True)
            if getattr(self, "list_lag", False):
                self.hidden_ids = getattr(self, "hidden_ids", set()) | {created["id"]}
            return json.dumps(created)
        if args[:2] == ["--method", "PATCH"]:
            release_id = int(args[2].rsplit("/", 1)[1])
            fields = self._fields(args)
            release = next(r for r in self.releases if r["id"] == release_id)
            if not self.ignore_publish:
                release["draft"] = fields["draft"] == "true"
                release["prerelease"] = fields["prerelease"] == "true"
            if fields["make_latest"] == "true" and not self.ignore_make_latest:
                self.latest_id = release_id
            return "{}"
        raise AssertionError(f"unexpected gh api call: {args}")

    def by_tag(self, tag: str) -> list[dict[str, Any]]:
        return [r for r in self.releases if r["tag_name"] == tag]

    def latest_tag(self) -> str | None:
        return next((r["tag_name"] for r in self.releases if r["id"] == self.latest_id), None)


# --- ensure ------------------------------------------------------------------


def test_ensure_creates_a_non_latest_draft_titled_with_the_bare_version() -> None:
    """No release yet: exactly one DRAFT is created, and latest does not move."""
    gh = FakeGitHub()
    gh.add("v0.9.4.18", draft=False, latest=True)

    release = ensure_draft(gh, REPO, "v0.9.4.19")

    assert release["draft"] is True
    assert release["name"] == "v0.9.4.19"
    assert len(gh.by_tag("v0.9.4.19")) == 1
    assert gh.latest_tag() == "v0.9.4.18"  # released desktops keep a working manifest


def test_ensure_reuses_the_existing_draft_on_rerun() -> None:
    """A re-run finds the draft it (or an earlier attempt) made and creates nothing."""
    gh = FakeGitHub()
    existing = gh.add("v0.9.4.19", draft=True)

    release = ensure_draft(gh, REPO, "v0.9.4.19")

    assert release["id"] == existing["id"]
    assert not any(call[:2] == ["--method", "POST"] for call in gh.calls)


def test_ensure_reuses_an_already_published_release_without_touching_it() -> None:
    """Re-running a whole tag build after publish uploads into the public release."""
    gh = FakeGitHub()
    gh.add("v0.9.4.19", draft=False, latest=True)

    release = ensure_draft(gh, REPO, "v0.9.4.19")

    assert release["draft"] is False
    assert len(gh.calls) == 1  # one lookup, no writes


def test_ensure_refuses_duplicate_releases_for_one_tag() -> None:
    """GitHub allows two drafts on one tag; uploads would scatter, so fail loud."""
    gh = FakeGitHub()
    gh.add("v0.9.4.19", draft=True)
    gh.add("v0.9.4.19", draft=True)

    with pytest.raises(ReleaseError, match="2 releases carry tag v0.9.4.19"):
        ensure_draft(gh, REPO, "v0.9.4.19")


def test_ensure_detects_a_racing_creator() -> None:
    """If another writer creates a draft between our lookup and our create, the
    post-create read-back sees two and fails instead of letting uploads split."""
    gh = FakeGitHub()
    gh.create_hook = lambda fake: fake.add("v0.9.4.19", draft=True)

    with pytest.raises(ReleaseError, match="2 releases carry tag"):
        ensure_draft(gh, REPO, "v0.9.4.19")


def test_ensure_rejects_a_malformed_tag_before_any_write() -> None:
    gh = FakeGitHub()
    with pytest.raises(ReleaseError, match="not a CLIO release tag"):
        ensure_draft(gh, REPO, "nightly")
    assert gh.calls == []


# --- publish -----------------------------------------------------------------


def test_publish_flips_the_draft_and_marks_it_latest() -> None:
    gh = FakeGitHub()
    gh.add("v0.9.4.18", draft=False, latest=True)
    gh.add("v0.9.4.19", draft=True)

    result = publish(gh, REPO, "v0.9.4.19")

    assert (result.published, result.made_latest) == (True, True)
    assert gh.by_tag("v0.9.4.19")[0]["draft"] is False
    assert gh.latest_tag() == "v0.9.4.19"


def test_publish_is_a_noop_on_an_already_published_release() -> None:
    """The workflow_dispatch escape hatch re-runs release-check on OLD tags; it must
    never steal latest back from a newer release."""
    gh = FakeGitHub()
    gh.add("v0.9.4.17", draft=False)
    gh.add("v0.9.4.19", draft=False, latest=True)

    result = publish(gh, REPO, "v0.9.4.17")

    assert (result.published, result.made_latest) == (False, False)
    assert gh.latest_tag() == "v0.9.4.19"
    assert not any(call[:2] == ["--method", "PATCH"] for call in gh.calls)


def test_publishing_an_older_draft_late_does_not_roll_latest_back() -> None:
    gh = FakeGitHub()
    gh.add("v0.9.4.20", draft=False, latest=True)
    gh.add("v0.9.4.19", draft=True)

    result = publish(gh, REPO, "v0.9.4.19")

    assert (result.published, result.made_latest) == (True, False)
    assert "v0.9.4.20 is a higher version" in result.detail
    assert gh.latest_tag() == "v0.9.4.20"


def test_publishing_a_prerelease_never_makes_it_latest() -> None:
    gh = FakeGitHub()
    gh.add("v0.9.4.19", draft=False, latest=True)
    gh.add("v0.9.5-rc1", draft=True)

    result = publish(gh, REPO, "v0.9.5-rc1")

    assert (result.published, result.made_latest) == (True, False)
    assert gh.by_tag("v0.9.5-rc1")[0]["prerelease"] is True
    assert gh.latest_tag() == "v0.9.4.19"


def test_first_ever_release_becomes_latest() -> None:
    gh = FakeGitHub()
    gh.add("v0.1.0", draft=True)

    assert publish(gh, REPO, "v0.1.0").made_latest is True
    assert gh.latest_tag() == "v0.1.0"


def test_publish_without_a_release_fails() -> None:
    with pytest.raises(ReleaseError, match="no release carries tag"):
        publish(FakeGitHub(), REPO, "v0.9.4.19")


def test_publish_fails_when_the_draft_flag_does_not_take() -> None:
    gh = FakeGitHub()
    gh.add("v0.9.4.19", draft=True)
    gh.ignore_publish = True

    with pytest.raises(ReleaseError, match="still reads back as a draft"):
        publish(gh, REPO, "v0.9.4.19")


def test_publish_fails_when_latest_does_not_move() -> None:
    gh = FakeGitHub()
    gh.add("v0.9.4.18", draft=False, latest=True)
    gh.add("v0.9.4.19", draft=True)
    gh.ignore_make_latest = True

    with pytest.raises(ReleaseError, match="latest is 'v0.9.4.18'"):
        publish(gh, REPO, "v0.9.4.19")


# --- version ordering ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "latest", "expected"),
    [
        ("v0.9.4.19", "v0.9.4.18", True),
        ("v0.9.4.19", "v0.9.4.19", True),  # re-publishing the same version
        ("v0.9.4.9", "v0.9.4.10", False),  # numeric, not lexicographic
        ("v0.9.5", "v0.9.4.19", True),  # X.Y.Z > X.Y.(Z-1).N
        ("v0.9.4.1", "v0.9.4", True),  # maintenance > base
        ("v0.9.5", "v0.9.5-rc1", True),  # release > its pre-release
        ("v1.0.0-rc1", None, False),  # pre-release never latest
        ("v0.9.4.19", None, True),
    ],
)
def test_should_mark_latest(tag: str, latest: str | None, expected: bool) -> None:
    assert should_mark_latest(tag, latest) is expected


def test_unparseable_current_latest_fails_loud() -> None:
    with pytest.raises(ReleaseError):
        should_mark_latest("v0.9.4.19", "some-old-name")


def test_version_parse_rejects_five_parts() -> None:
    with pytest.raises(ReleaseError):
        Version.parse("v0.9.4.1.2")


# --- CLI -----------------------------------------------------------------------


def test_main_ensure_then_publish_writes_github_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """End to end through the CLI: ensure -> (draft, not latest) -> publish -> latest."""
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    gh = FakeGitHub()
    gh.add("v0.9.4.18", draft=False, latest=True)

    assert main(["ensure", "--tag", "v0.9.4.19", "--repo", REPO], api=gh) == 0
    assert gh.latest_tag() == "v0.9.4.18"
    assert main(["publish", "--tag", "v0.9.4.19", "--repo", REPO], api=gh) == 0
    assert gh.latest_tag() == "v0.9.4.19"

    outputs = out.read_text(encoding="utf-8")
    assert "state=draft" in outputs
    assert "published=true" in outputs
    assert "made_latest=true" in outputs
    assert "marked it latest" in capsys.readouterr().out


def test_main_reports_invariant_failures_with_exit_1(capsys: Any) -> None:
    gh = FakeGitHub()
    gh.add("v0.9.4.19", draft=True)
    gh.add("v0.9.4.19", draft=True)

    assert main(["ensure", "--tag", "v0.9.4.19", "--repo", REPO], api=gh) == 1
    assert "FAIL: 2 releases carry tag v0.9.4.19" in capsys.readouterr().err


def test_main_requires_a_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert main(["ensure", "--tag", "v0.9.4.19"], api=FakeGitHub()) == 1


def test_ensure_draft_trusts_the_create_response_when_the_listing_lags() -> None:
    """v0.9.4.20: the list endpoint omitted a draft created a moment earlier."""

    gh = FakeGitHub()
    gh.list_lag = True
    release = ensure_draft(gh, REPO, "v9.9.9")
    assert release["tag_name"] == "v9.9.9"
    assert release["draft"] is True
