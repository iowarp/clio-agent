"""Why an agent's browser call failed, in terms it can act on (browser addendum B).

Chromium names a failed navigation with its own error code (``net::ERR_CERT_AUTHORITY_INVALID``,
``net::ERR_NAME_NOT_RESOLVED``). The code is read from Playwright's error, or from the
page's own failed main-frame request when Playwright reports the load as interrupted by
Chromium's error page; nothing else in the text is interpreted.

- A certificate error is ``browser_certificate_untrusted`` (:mod:`certificates`).
- A host that did not answer is ``browser_host_unreachable``: a page loads from the
  computer the agent runs on, so the error names that computer (and the infrastructure
  target it was started through), and for a private, link-local or loopback address, or a
  ``.local`` or ``.lan`` name, says that such an address is resolved on the agent's
  network, not the person's. Nothing is guessed about where the person is, and nothing is
  retried.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from clio_agent import conf
from clio_agent.gact.browser import certificates
from clio_agent.gact.browser.settings import BrowserError

if TYPE_CHECKING:
    from clio_agent.gact.browser.service import BrowserService, Inflight, PageRecord

__all__ = ["UNREACHABLE", "classify", "denied", "private_address"]

_NET_ERROR = re.compile(r"net::(ERR_[A-Z0-9_]+)")
#: The errors of a host that did not answer the agent's computer (Phase 0).
UNREACHABLE = frozenset(
    {
        "ERR_ADDRESS_UNREACHABLE",
        "ERR_CONNECTION_TIMED_OUT",
        "ERR_CONNECTION_REFUSED",
        "ERR_NAME_NOT_RESOLVED",
    }
)
_LOCAL_SUFFIXES = (".local", ".lan", ".localhost")


def denied(inflight: "Inflight") -> BrowserError:
    """A navigation of the call led to a host the person refused."""
    return BrowserError(
        "browser_host_denied",
        f"the person did not allow loading {inflight.denied_host}",
        host=inflight.denied_host,
    )


def private_address(host: str) -> bool:
    """A private, link-local or loopback address, or a name only a local network resolves."""
    name = host.strip("[]").lower()
    if name == "localhost" or name.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_private or address.is_link_local or address.is_loopback


def _error_name(text: str, record: "PageRecord") -> str:
    found = _NET_ERROR.search(text)
    if found:
        return found.group(1)
    if "chrome-error://" not in text:  # only a load Chromium replaced with its error page
        return ""
    failure = record.load_error or {}
    found = _NET_ERROR.search(str(failure.get("error") or ""))
    return found.group(1) if found else ""


def _unreachable(record: "PageRecord", error: str) -> BrowserError:
    failure = record.load_error or {}
    url = str(failure.get("url") or record.main_url)
    host = (urlsplit(url).hostname or "").lower()
    agent_host = socket.gethostname()
    target = str(
        conf.resolve("deployment.target", env="CLIO_DEPLOYMENT_TARGET", default="") or ""
    ).strip()
    where = agent_host + (f" (the infrastructure target {target})" if target else "")
    message = (
        f"{host} did not answer the browser ({error}). The browser runs on the agent's "
        f"computer, {where}, and loads pages from there."
    )
    if private_address(host):
        message += (
            " A private, link-local or loopback address, or a .local or .lan name, is "
            "resolved on the agent's network, not the person's: a device on the person's "
            "network is reachable only when the agent runs on that network."
        )
    return BrowserError(
        "browser_host_unreachable",
        message + " Nothing was retried.",
        host=host,
        error=error,
        agent_host=agent_host,
        target=target,
        private=private_address(host),
        page=record.id,
    )


async def classify(
    service: "BrowserService", record: "PageRecord", inflight: "Inflight", exc: Exception
) -> BrowserError:
    """A typed reason for a Playwright error raised during an agent call."""
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    if inflight.denied_host:
        return denied(inflight)
    if "strict mode violation" in text:
        return BrowserError("browser_target_ambiguous", text)
    error = _error_name(text, record)
    if error.startswith("ERR_CERT_"):
        return certificates.untrusted_error(record, await certificates.wait_seen(record), error)
    if error in UNREACHABLE:
        return _unreachable(record, error)
    return BrowserError("browser_action_failed", text)
