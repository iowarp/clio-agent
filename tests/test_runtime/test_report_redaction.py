"""The problem report's redaction: real-looking secrets in a log never survive it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.runtime.report_redaction import (
    REDACTED,
    Redactor,
    environment_secrets,
    stored_secrets,
)

HOME = "/home/alice"
# Real-looking, but generated for this test: none of these is a live credential.
# Fake values, split so secret scanners do not mistake them for real ones.
ANTHROPIC = "sk" + "-ant-api03-Zq8vT1xY2wK9mN4pR7sL0bC3dF6gH5jE-AbCdEfGh"
OPENAI = "sk" + "-proj-4fJ9kL2mN8pQ1rS5tU7vW3xY6zA0bC"
GITHUB = "gh" + "p_1A2b3C4d5E6f7G8h9I0jK1lM2nO3pQ4rS5tU"
GITHUB_PAT = "gi" + "thub_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"
SLACK = "xo" + "xb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
AWS = "AK" + "IAIOSFODNN7EXAMPLE"
GOOGLE = "AI" + "zaSyD-1234567890abcdefghijklmnopqrstu"
HF = "hf" + "_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvZSJ9"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
CAPABILITY = "clio-cap-7d1f0c2e9b8a4f63a1e5d2c4b6a8f0e1"
CONNECTOR_SECRET = "ndp-connector-Q9w8E7r6T5y4U3i2"


def _redactor(*known: str) -> Redactor:
    return Redactor(known, home=HOME)


FIXTURE_LOG = f"""\
2026-11-15 10:00:01 INFO uvicorn started on http://127.0.0.1:17800
2026-11-15 10:00:02 DEBUG request headers: {{'Authorization': 'Bearer {CAPABILITY}', 'Accept': 'application/json'}}
2026-11-15 10:00:03 DEBUG > Authorization: Bearer {OPENAI}
2026-11-15 10:00:03 DEBUG > Cookie: session=abc123def456; csrftoken=zyx987
2026-11-15 10:00:04 WARNING provider call failed api_key={ANTHROPIC} model=claude
2026-11-15 10:00:05 INFO config {{"api_key": "{OPENAI}", "password": "hunter2hunter2", "max_tokens": 4096}}
2026-11-15 10:00:06 INFO cloning https://alice:{GITHUB}@github.com/acme/repo.git
2026-11-15 10:00:07 INFO oauth callback GET /oauth/mcp?code=4/0AbCdEfGhIjKl&state=xyz123abc
2026-11-15 10:00:08 ERROR slack {SLACK} aws {AWS} google {GOOGLE} hf {HF}
2026-11-15 10:00:09 DEBUG id_token={JWT}
2026-11-15 10:00:10 INFO connector env NDP_TOKEN set to {CONNECTOR_SECRET}
2026-11-15 10:00:11 INFO reading {HOME}/.config/clio-agent/config.yaml and {HOME}/data
2026-11-15 10:00:12 INFO pat {GITHUB_PAT}
2026-11-15 10:00:13 INFO prompt_tokens: 1200 completion_tokens: 88 tokens_input=5000
"""


def test_a_log_with_real_looking_secrets_keeps_none_of_them() -> None:
    redactor = _redactor(CAPABILITY, CONNECTOR_SECRET)
    out = redactor.apply(FIXTURE_LOG)

    for secret in (
        ANTHROPIC,
        OPENAI,
        GITHUB,
        GITHUB_PAT,
        SLACK,
        AWS,
        GOOGLE,
        HF,
        JWT,
        CAPABILITY,
        CONNECTOR_SECRET,
        "hunter2hunter2",
        "abc123def456",
        "4/0AbCdEfGhIjKl",
    ):
        assert secret not in out, secret
    assert HOME not in out
    assert "~/.config/clio-agent/config.yaml" in out
    # Ordinary diagnostics stay readable.
    assert "uvicorn started on http://127.0.0.1:17800" in out
    assert "max_tokens" in out and "4096" in out
    assert "prompt_tokens: 1200 completion_tokens: 88 tokens_input=5000" in out
    assert "github.com/acme/repo.git" in out
    counts = {item["rule"]: item["count"] for item in redactor.summary()}
    assert counts["known_value"] >= 2
    assert counts["home_path"] == 2
    assert counts["credential_header"] >= 2
    assert counts["url_credentials"] >= 1


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("Authorization: Basic YWxpY2U6cGFzc3dvcmQxMjM=", "Authorization:"),
        ('{"headers": {"x-api-key": "k-12345678abcdef"}}', '"x-api-key": "'),
        ("Set-Cookie: sid=s%3Aabcdef1234567890; Path=/; HttpOnly", "Set-Cookie:"),
        ("postgres://admin:pa55w0rd!@db.internal:5432/clio", "postgres://"),
        ("CLIENT_SECRET = 'cs_live_abcdefgh12345678'", "CLIENT_SECRET"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----", ""),
        ("token=abcd1234efgh5678", "token="),
    ],
)
def test_tricky_shapes_are_redacted(line: str, kept: str) -> None:
    out = _redactor().apply(line)
    assert REDACTED in out
    assert kept in out
    for fragment in (
        "YWxpY2U6cGFzc3dvcmQxMjM=",
        "k-12345678abcdef",
        "abcdef1234567890",
        "pa55w0rd",
        "cs_live_abcdefgh12345678",
        "MIIEowIBAAKCAQEA",
        "abcd1234efgh5678",
    ):
        assert fragment not in out


def test_ordinary_identifiers_in_web_addresses_survive() -> None:
    line = 'GET /v1/fleet?session=sess_ba7c9817efb6&limit=30 HTTP/1.1" 200 OK'
    assert _redactor().apply(line) == line


def test_home_path_is_replaced_only_at_a_path_boundary() -> None:
    out = _redactor().apply("/home/alice/x and /home/alicia/y and /home/alice")
    assert out == "~/x and /home/alicia/y and ~"


def test_known_values_are_removed_longest_first() -> None:
    out = _redactor("abcdefgh", "abcdefgh-ijklmnop").apply("v=abcdefgh-ijklmnop!")
    assert out == f"v={REDACTED}!"


def test_short_known_values_are_ignored_so_common_words_survive() -> None:
    assert _redactor("on", "ok").apply("status ok, server on") == "status ok, server on"


def test_environment_secrets_take_secret_named_values_only() -> None:
    env = {
        "OPENAI_API_KEY": OPENAI,
        "CLIO_GACT_BEARER_TOKEN": CAPABILITY,
        "MY_PASSWORD": "correct-horse-battery",
        "CLIO_LM_PROVIDER": "claude_code",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "TOKEN_CACHE_DIR": "/var/cache/tokens",
        "AUTH_ENABLED": "true",
        "PATH": "/usr/bin",
    }
    assert environment_secrets(env) == {OPENAI, CAPABILITY, "correct-horse-battery"}


def test_stored_secrets_reads_the_credential_stores_and_config(tmp_path: Path) -> None:
    (tmp_path / "provider_api_keys.json").write_text(
        json.dumps({"schema": "x", "entries": {"openai": {"api_key": OPENAI}}})
    )
    (tmp_path / "mcp_oauth_tokens.json").write_text(
        json.dumps(
            {
                "entries": {
                    "clio-connector-secrets": {"secrets": {"ndp_key": CONNECTOR_SECRET}},
                    "https://mcp.example.org": {
                        "tokens": {"access_token": JWT, "token_type": "Bearer"},
                        "client_info": {"client_id": "public-id", "client_secret": "cs-abcdefgh"},
                    },
                }
            }
        )
    )
    (tmp_path / "config.yaml").write_text(
        "lm:\n  provider: openai\n  api_key: sk-from-config-123456\nui:\n  theme: dark\n"
    )
    found = stored_secrets(tmp_path)
    assert {OPENAI, CONNECTOR_SECRET, JWT, "cs-abcdefgh", "sk-from-config-123456"} <= found
    assert "public-id" not in found
    assert "Bearer" not in found
    assert "dark" not in found


def test_stored_secrets_tolerates_missing_and_malformed_files(tmp_path: Path) -> None:
    (tmp_path / "provider_api_keys.json").write_text("{not json")
    (tmp_path / "config.yaml").write_text("a: [unclosed\n")
    assert stored_secrets(tmp_path) == set()
