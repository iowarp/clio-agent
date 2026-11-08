"""The read-once memo shares a derivation within one catalog check, never beyond it."""

from __future__ import annotations

from clio_agent.providers.handshake_memo import forget, handshake_pass, read_once


def test_outside_a_pass_every_read_builds() -> None:
    builds: list[int] = []
    assert read_once("k", lambda: builds.append(1) or len(builds)) == 1
    assert read_once("k", lambda: builds.append(1) or len(builds)) == 2


def test_a_pass_builds_each_key_once_and_forgets_on_request() -> None:
    builds: list[str] = []

    def build(key: str) -> str:
        builds.append(key)
        return f"{key}-{len(builds)}"

    with handshake_pass():
        assert read_once("a", lambda: build("a")) == "a-1"
        assert read_once("a", lambda: build("a")) == "a-1"
        assert read_once("b", lambda: build("b")) == "b-2"
        forget("a")
        assert read_once("a", lambda: build("a")) == "a-3"
    with handshake_pass():
        assert read_once("a", lambda: build("a")) == "a-4"  # nothing outlives a pass
    forget("a")  # outside a pass: no-op
    assert builds == ["a", "b", "a", "a"]
