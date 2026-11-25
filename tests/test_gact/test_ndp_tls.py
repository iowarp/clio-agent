"""NDP HTTP clients verify TLS with the OS trust store, never unverified."""

from __future__ import annotations

import ssl

from clio_agent.gact.storage.ndp_tls import system_tls


def test_system_tls_is_a_verifying_context() -> None:
    context = system_tls()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert system_tls() is context
