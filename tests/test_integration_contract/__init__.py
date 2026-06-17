"""Integration tests against a live clio-agent-gact backed by a
real LM provider.

Skipped automatically when:
  - CLIO_INTEGRATION_BASE not set, or
  - GET /v1/health on that base fails.

Each test exercises one v0.2 capability END-TO-END through the
real ClioAgent + LM. Pass = the capability is genuinely usable,
not just wire-shaped.

Run:
    CLIO_INTEGRATION_BASE=http://127.0.0.1:17779 \\
      uv run --extra dev pytest tests/test_integration_contract/ -v

Optional:
    Set CLIO_INTEGRATION_STREAM_PROVIDER, CLIO_INTEGRATION_STREAM_MODEL,
    CLIO_INTEGRATION_STREAM_API_BASE, and CLIO_INTEGRATION_STREAM_API_KEY
    to make the streaming test hot-swap providers before the turn. When
    unset, the suite uses the backend's current provider.
"""
