"""Integration tests against a live clio-agent-gact backed by a
real LM provider (OpenAI or Anthropic by default; OpenRouter free
models for cross-provider sanity).

Skipped automatically when:
  - CLIO_INTEGRATION_BASE not set, or
  - GET /v1/health on that base fails.

Each test exercises one v0.2 capability END-TO-END through the
real ClioAgent + LM. Pass = the capability is genuinely usable,
not just wire-shaped.

Run:
    CLIO_INTEGRATION_BASE=http://127.0.0.1:17779 \\
      uv run --extra dev pytest tests/test_integration_v0_2/ -v
"""
