"""The obligations doc's verification-probe shortlist (#1285, C1-S5 item 5).

``docs/design/mcp-client-obligations-2026-07-28.md`` lists items the original
sweep could not verify from source inspection alone: "A7 network-$ref
refusal; B2 sentinel encoding; B3 header emission + invalid-tool rejection;
D1 resources/updated; E6 legacy -32002 acceptance; F2 enums/defaults; F8
per-request logLevel; H2/H3/H8 auth specifics." B2/B3 were verified and
closed by #1285 item 1 (see test_mcp_header_family.py); E10 pagination by
item 4's adversarial fixture (test_mcp_adversarial.py). This file verifies
the remaining auth (H3/H4/H5/H8) + JSON-Schema (A7) + SSE resumability (B5)
items directly against the installed SDK source -- pinning what is ACTUALLY
true today, including one genuine gap (H3).
"""

from __future__ import annotations

import inspect
from pathlib import Path


def _oauth2_source() -> str:
    import mcp.client.auth.oauth2 as oauth2

    return Path(inspect.getfile(oauth2)).read_text(encoding="utf-8")


def test_h5_issuer_validated_before_code_redemption() -> None:
    """H5 (SEP-2468): the SDK compares the authorization response's `iss`
    against the discovered metadata before redeeming the code."""

    source = _oauth2_source()
    assert "validate_authorization_response_iss" in source
    assert "validate_metadata_issuer" in source


def test_h8_credentials_bound_to_issuer_never_reused_across_authorization_servers() -> None:
    """H8 (SEP-2352): stored credentials are keyed by the issuer that
    registered them; the SDK checks this before reusing a stored registration."""

    source = _oauth2_source()
    assert "credentials_match_issuer" in source


def test_h4_resource_indicator_present_on_authz_and_token_requests() -> None:
    """H4 (RFC 8707): the `resource` parameter rides BOTH the authorization
    request and every token request (initial + refresh)."""

    source = _oauth2_source()
    assert 'auth_params["resource"]' in source
    assert 'token_data["resource"]' in source
    assert 'refresh_data["resource"]' in source


def test_h3_pkce_absence_refusal_is_a_verified_sdk_gap() -> None:
    """H3 says the client MUST refuse when the AS's own metadata omits
    `code_challenge_methods_supported` (no way to confirm S256 is accepted).
    VERIFIED GAP, not a clio decision: the installed SDK never reads that
    field at all -- it unconditionally sends `code_challenge_method: S256`
    regardless of what (or whether) the AS advertises. `code_challenge_
    methods_supported` exists only as a model FIELD (mcp/shared/auth.py) and
    on the SERVER's own advertised metadata (mcp/server/auth/routes.py); the
    client never reads it. Implementing the refusal is out of scope for a
    C1-S5 "smalls" item (it needs intercepting the SDK's own OAuth flow, not
    a client-construction-time knob) -- pinned here as a finding for a future
    slice, not silently assumed compliant."""

    source = _oauth2_source()
    assert "code_challenge_methods_supported" not in source, (
        "if this becomes True, the SDK started reading the field -- update this "
        "test to assert the REFUSAL behavior instead of the gap, and close the finding"
    )
    assert '"code_challenge_method": "S256"' in source, (
        "the SDK still unconditionally sends S256 without checking AS support"
    )


def test_a7_no_network_json_schema_dereferencing_in_clio_agent() -> None:
    """A7 (SEP-2106): clients MUST NOT auto-dereference a network `$ref` when
    validating JSON Schema. clio_agent never implements its own JSON-Schema
    validator or resolver at all (repo-wide grep: zero jsonschema/Registry/
    RefResolver hits) -- every typed validation goes through pydantic's
    TypeAdapter against LOCALLY Python-type-derived schemas (never a raw
    JSON-Schema document with a $ref clio would need to resolve), so this is
    vacuously satisfied by construction, not by an explicit refusal check."""

    src_root = Path("src/clio_agent")
    offenders = []
    for path in src_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "jsonschema" in text or "RefResolver" in text:
            offenders.append(str(path))
    assert not offenders, (
        f"clio_agent started using a JSON-Schema library directly: {offenders} -- "
        "verify it never auto-dereferences a network $ref before allowing this"
    )


def test_b5_sse_streams_are_never_resumed_only_reissued() -> None:
    """B5 (SEP-2575): a broken stream must be RE-ISSUED as a new request id,
    never resumed via Last-Event-ID/a resumption token (removed in 2026-07-28).
    clio never passes ClientMessageMetadata.resumption_token/on_resumption_
    token_update anywhere (repo-wide grep: zero hits), so every wait ladder
    relies entirely on the SDK's own default re-issue behavior rather than
    attempting to resume a broken stream itself."""

    src_root = Path("src/clio_agent")
    offenders = []
    for path in src_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "resumption_token" in text or "on_resumption_token" in text:
            offenders.append(str(path))
    assert not offenders, (
        f"clio_agent started passing SSE resumption metadata: {offenders} -- "
        "B5 removed stream resumability; this must re-issue, never resume"
    )


def test_e9_completions_capability_exists_but_clio_never_surfaces_it() -> None:
    """E9: client.complete() is library-covered. clio never calls it -- no
    @server.prompt/argument-completion UI surface exists anywhere in this
    repo (matches the C1-S4 mrtr-methods finding: no resources/prompts
    surface at all), so this is a capability CLIO has available but has not
    built a UI for -- not a gap in what the library offers."""

    from fastmcp import Client

    assert hasattr(Client, "complete")
    assert callable(Client.complete)

    # Unambiguous MCP-specific signal, not the bare ".complete(" substring
    # (which also matches unrelated completions, e.g. the Claude Agent SDK's
    # own session.complete() for LM calls in providers/claude_code_sdk_pool.py).
    src_root = Path("src/clio_agent")
    call_sites = [
        str(path)
        for path in src_root.rglob("*.py")
        if "CompletionArgument" in path.read_text(encoding="utf-8")
        or "completion/complete" in path.read_text(encoding="utf-8")
    ]
    assert call_sites == [], (
        "clio_agent now uses MCP completion/complete somewhere -- this test's "
        "premise ('never surfaced') is stale; update or remove it"
    )
