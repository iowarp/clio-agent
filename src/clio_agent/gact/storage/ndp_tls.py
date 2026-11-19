"""TLS trust for NDP HTTP clients: the operating system's trust store.

NDP clients pass ``trust_env=False`` so environment proxies and CA variables
cannot redirect catalog or credential traffic. httpx then trusts only certifi's
bundle, which fails on networks that inspect TLS with an organization CA that
the OS trusts (common at labs and universities, and in managed containers):
every catalog search fails with "self-signed certificate in chain". The OS trust
store (via ``truststore``, as plugin fetches already use) is what the person's
browser and system tools trust, so CLIO verifies NDP the same way.
"""

from __future__ import annotations

import ssl
from functools import lru_cache


@lru_cache(maxsize=1)
def system_tls() -> ssl.SSLContext:
    """A verifying client context backed by the OS trust store (never unverified)."""
    import truststore  # noqa: PLC0415 - a core dependency; loaded on first NDP request

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
