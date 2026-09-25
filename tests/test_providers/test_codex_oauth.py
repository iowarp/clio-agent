"""Unit tests for the Codex OAuth protocol mechanics (A.9).

Covers PKCE generation, the authorize URL, all four paste forms, state
mismatch, JWT account-id extraction, and token response validation.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex import oauth


def test_generate_pkce_verifier_is_url_safe_and_in_range() -> None:
    pair = oauth.generate_pkce()
    assert 43 <= len(pair.verifier) <= 128
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(pair.verifier) <= allowed
    assert "=" not in pair.challenge


def test_generate_pkce_challenge_matches_verifier_sha256() -> None:
    import hashlib

    pair = oauth.generate_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode("ascii")).digest())
    expected = expected.decode("ascii").rstrip("=")
    assert pair.challenge == expected


def test_generate_pkce_state_is_16_bytes_hex() -> None:
    pair = oauth.generate_pkce()
    assert len(pair.state) == 32
    int(pair.state, 16)  # raises ValueError if not hex


def test_build_authorize_url_has_every_a3_parameter() -> None:
    pair = oauth.generate_pkce()
    url = oauth.build_authorize_url(pair)
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == c.AUTHORIZE_URL
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [c.CLIENT_ID]
    assert query["redirect_uri"] == [c.REDIRECT_URI]
    assert query["scope"] == [c.SCOPE]
    assert query["code_challenge"] == [pair.challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [pair.state]
    assert query["id_token_add_organizations"] == ["true"]
    assert query["codex_cli_simplified_flow"] == ["true"]
    assert query["originator"] == [c.ORIGINATOR]


class TestParsePaste:
    def test_full_url_form(self) -> None:
        code, state = oauth.parse_paste(f"{c.REDIRECT_URI}?code=abc123&state=xyz")
        assert (code, state) == ("abc123", "xyz")

    def test_code_hash_state_form(self) -> None:
        code, state = oauth.parse_paste("abc123#xyz")
        assert (code, state) == ("abc123", "xyz")

    def test_raw_query_string_form(self) -> None:
        code, state = oauth.parse_paste("code=abc123&state=xyz")
        assert (code, state) == ("abc123", "xyz")

    def test_bare_code_form(self) -> None:
        code, state = oauth.parse_paste("abc123")
        assert (code, state) == ("abc123", None)

    def test_empty_paste_raises(self) -> None:
        with pytest.raises(oauth.OAuthError):
            oauth.parse_paste("   ")

    def test_missing_code_raises(self) -> None:
        with pytest.raises(oauth.OAuthError):
            oauth.parse_paste(f"{c.REDIRECT_URI}?state=xyz")

    def test_state_mismatch_raises(self) -> None:
        with pytest.raises(oauth.StateMismatchError):
            oauth.parse_paste("abc123#xyz", expected_state="expected")

    def test_matching_state_passes(self) -> None:
        code, state = oauth.parse_paste("abc123#expected", expected_state="expected")
        assert (code, state) == ("abc123", "expected")

    def test_no_state_present_is_not_a_mismatch(self) -> None:
        code, state = oauth.parse_paste("abc123", expected_state="expected")
        assert (code, state) == ("abc123", None)


def _jwt_with_payload(payload: dict) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64(json.dumps({"alg": "none"}).encode("utf-8"))
    body = b64(json.dumps(payload).encode("utf-8"))
    return f"{header}.{body}.signature"


class TestDecodeAccountId:
    def test_extracts_account_id_from_claim(self) -> None:
        token = _jwt_with_payload({c.JWT_AUTH_CLAIM: {"codex_account_id": "acct_123"}})
        assert oauth.decode_account_id(token) == "acct_123"

    def test_missing_claim_raises(self) -> None:
        token = _jwt_with_payload({"other": "value"})
        with pytest.raises(oauth.OAuthError):
            oauth.decode_account_id(token)

    def test_missing_account_id_field_raises(self) -> None:
        token = _jwt_with_payload({c.JWT_AUTH_CLAIM: {}})
        with pytest.raises(oauth.OAuthError):
            oauth.decode_account_id(token)

    def test_malformed_jwt_raises(self) -> None:
        with pytest.raises(oauth.OAuthError):
            oauth.decode_account_id("not-a-jwt")


class TestTokenExchange:
    def test_exchange_code_validates_full_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/oauth/token")
            body = request.read().decode("utf-8")
            assert "grant_type=authorization_code" in body
            return httpx.Response(
                200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = oauth.exchange_code(
            code="c", code_verifier="v", redirect_uri=c.REDIRECT_URI, client=client
        )
        assert result.access_token == "at"
        assert result.refresh_token == "rt"
        assert result.expires_in == 3600

    def test_exchange_code_missing_field_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "at"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(oauth.TokenExchangeError):
            oauth.exchange_code(
                code="c", code_verifier="v", redirect_uri=c.REDIRECT_URI, client=client
            )

    def test_exchange_code_http_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(oauth.TokenExchangeError):
            oauth.exchange_code(
                code="c", code_verifier="v", redirect_uri=c.REDIRECT_URI, client=client
            )

    def test_refresh_token_rotates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode("utf-8")
            assert "grant_type=refresh_token" in body
            return httpx.Response(
                200, json={"access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = oauth.refresh_token("old_rt", client=client)
        assert result.access_token == "new_at"
        assert result.refresh_token == "new_rt"

    def test_refresh_token_omitted_keeps_old_refresh_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "new_at", "expires_in": 3600})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = oauth.refresh_token("old_rt", client=client)
        assert result.refresh_token == "old_rt"


class TestDeviceLogin:
    def test_start_device_login_parses_string_interval(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"device_auth_id": "d1", "user_code": "ABCD-1234", "interval": "5"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        login = oauth.start_device_login(client=client)
        assert login.device_auth_id == "d1"
        assert login.user_code == "ABCD-1234"
        assert login.interval_s == 5.0

    def test_start_device_login_404_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(oauth.DeviceLoginUnavailableError):
            oauth.start_device_login(client=client)

    def test_poll_device_login_pending_then_success(self) -> None:
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] < 3:
                return httpx.Response(403, json={"error": "deviceauth_authorization_pending"})
            return httpx.Response(
                200, json={"authorization_code": "code", "code_verifier": "verifier"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        login = oauth.DeviceLogin(
            device_auth_id="d1",
            user_code="ABCD-1234",
            verification_url=c.DEVICE_VERIFY_URL,
            interval_s=0.0,
        )
        result = oauth.poll_device_login(login, client=client, sleep=lambda _s: None)
        assert result.authorization_code == "code"
        assert result.code_verifier == "verifier"
        assert calls["count"] == 3

    def test_poll_device_login_slow_down_increases_interval(self) -> None:
        seen_sleeps: list[float] = []
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(400, json={"error": "slow_down"})
            return httpx.Response(
                200, json={"authorization_code": "code", "code_verifier": "verifier"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        login = oauth.DeviceLogin(
            device_auth_id="d1",
            user_code="ABCD-1234",
            verification_url=c.DEVICE_VERIFY_URL,
            interval_s=1.0,
        )
        oauth.poll_device_login(login, client=client, sleep=seen_sleeps.append)
        assert seen_sleeps == [1.0, 1.5]

    def test_poll_device_login_unrecognized_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_client"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        login = oauth.DeviceLogin(
            device_auth_id="d1",
            user_code="ABCD-1234",
            verification_url=c.DEVICE_VERIFY_URL,
            interval_s=0.0,
        )
        with pytest.raises(oauth.OAuthError):
            oauth.poll_device_login(login, client=client, sleep=lambda _s: None)


class TestLoopbackListener:
    """End-to-end coverage of the real HTTP listener (A.3 Method 1).

    Binds an OS-assigned port (never the fixed 1455) so this never collides
    with a real login attempt or another test process.
    """

    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(oauth.c, "LOOPBACK_PORT", 0)
        listener = oauth.LoopbackListener("expected-state")
        listener.start()
        try:
            port = listener._server.server_address[1]  # noqa: SLF001 - test-only introspection
            response = httpx.get(
                f"http://127.0.0.1:{port}/auth/callback?code=abc123&state=expected-state"
            )
            assert response.status_code == 200
            code, state = listener.wait_for_code(5.0)
            assert (code, state) == ("abc123", "expected-state")
        finally:
            listener.close()

    def test_state_mismatch_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(oauth.c, "LOOPBACK_PORT", 0)
        listener = oauth.LoopbackListener("expected-state")
        listener.start()
        try:
            port = listener._server.server_address[1]  # noqa: SLF001
            response = httpx.get(f"http://127.0.0.1:{port}/auth/callback?code=abc123&state=wrong")
            assert response.status_code == 400
            with pytest.raises(oauth.StateMismatchError):
                listener.wait_for_code(5.0)
        finally:
            listener.close()

    def test_missing_code_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(oauth.c, "LOOPBACK_PORT", 0)
        listener = oauth.LoopbackListener("expected-state")
        listener.start()
        try:
            port = listener._server.server_address[1]  # noqa: SLF001
            response = httpx.get(f"http://127.0.0.1:{port}/auth/callback?state=expected-state")
            assert response.status_code == 400
            with pytest.raises(oauth.OAuthError):
                listener.wait_for_code(5.0)
        finally:
            listener.close()

    def test_unknown_path_is_404_and_does_not_resolve_the_flow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(oauth.c, "LOOPBACK_PORT", 0)
        listener = oauth.LoopbackListener("expected-state")
        listener.start()
        try:
            port = listener._server.server_address[1]  # noqa: SLF001
            response = httpx.get(f"http://127.0.0.1:{port}/other-path")
            assert response.status_code == 404
            with pytest.raises(oauth.OAuthError):
                listener.wait_for_code(0.2)
        finally:
            listener.close()

    def test_close_is_always_safe(self) -> None:
        listener = oauth.LoopbackListener("expected-state")
        listener.close()  # never started -- must not raise
