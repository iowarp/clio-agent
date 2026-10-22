"""``serve`` never listens beyond loopback without a bearer token."""

from __future__ import annotations

import pytest

from clio_agent.gact.auth import OpenBindRefused, refuse_open_bind


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost"])
def test_loopback_serves_without_a_token(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.delenv("CLIO_GACT_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CLIO_AUTH_TOKEN", raising=False)
    refuse_open_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5"])
def test_an_open_bind_needs_a_token(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.delenv("CLIO_GACT_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CLIO_AUTH_TOKEN", raising=False)
    with pytest.raises(OpenBindRefused, match="no bearer token"):
        refuse_open_bind(host)
    monkeypatch.setenv("CLIO_GACT_BEARER_TOKEN", "s3cret-token-value")
    refuse_open_bind(host)
