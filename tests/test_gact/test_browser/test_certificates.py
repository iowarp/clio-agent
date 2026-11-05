"""Untrusted certificates are the person's decision; unreachable hosts say where the agent is.

Browser addendum B, against a real Chromium and the fixture site's self-signed
``https://c.fixture.localhost``.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.browser import certificates, tools
from clio_agent.gact.browser.settings import BrowserError
from clio_agent.gact.runtime.grant_resolver import KIND_BROWSER_CERTIFICATE, resolve
from clio_agent.gact.runtime.permission_policies import _validate_permission_policies
from tests._marketplace import MARKETPLACE_ROOT, REPO_ROOT

_NOZZLE = {"steps": [{"by": "css", "css": "#nozzle"}]}


@pytest.fixture()
def conversation(browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A real app with one session, bound as the active conversation."""
    from clio_agent.gact import context
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json", arc=None)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "Printer"}).json()["id"]
    monkeypatch.setattr(app.state, "permission_policies_path", None, raising=False)
    tokens = [context.set_app(app), context.set_session_id(sid)]
    browser.app = app
    try:
        yield app, sid, client
    finally:
        for token in reversed(tokens):
            token.var.reset(token)


def _untrusted(url: str) -> BrowserError:
    with pytest.raises(BrowserError) as raised:
        tools.browser_open(url=url)
    assert raised.value.reason == "browser_certificate_untrusted"
    return raised.value


def _wait_for(predicate: Any, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def test_the_fingerprint_is_the_keys_sha256(tls_site: dict[str, Any]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    pem = (tls_site["tls"].folder / "cert.pem").read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    expected = base64.b64encode(hashlib.sha256(spki).digest()).decode()
    assert certificates.spki_fingerprint(der) == expected == tls_site["fingerprint"]()


def test_an_untrusted_certificate_stops_the_agent_and_the_person_trusts_it(
    conversation: Any, browser: Any, tls_site: dict[str, Any]
) -> None:
    app, sid, client = conversation
    refused = _untrusted(tls_site["c"] + "/status")
    detail = refused.detail
    assert detail["host"] == "c.fixture.localhost"
    assert detail["error"] == "ERR_CERT_AUTHORITY_INVALID"
    assert detail["fingerprint"] == tls_site["fingerprint"]()
    assert "c.fixture.localhost" in detail["subject"] and detail["issuer"]
    page = detail["page"]
    shown = tools.browser_pages()["pages"][0]  # the page stays, on Chromium's warning
    assert shown["id"] == page and shown["url"].startswith(tls_site["c"])
    assert shown["certificate_untrusted"]["fingerprint"] == detail["fingerprint"]
    assert shown["certificate_untrusted"]["grantable"] is True

    answer = client.post(
        f"/v1/sessions/{sid}/browser/pages/{page}/trust-certificate", json={"scope": "session"}
    )
    assert answer.status_code == 200, answer.text
    rows = [r for r in app.state.permission_policies if r.get("kind") == KIND_BROWSER_CERTIFICATE]
    assert rows == [
        {
            **rows[0],
            "scope": "session",
            "scope_id": sid,
            "host_pattern": "c.fixture.localhost",
            "fingerprint": detail["fingerprint"],
            "action": "allow",
        }
    ]
    record = browser.pages[page]
    _wait_for(lambda: not record.relaunching and record.page.url.endswith("/status"))
    read = tools.browser_read(page, _NOZZLE, "text")
    assert "215" in str(read) and read["page"]["id"] == page  # same page, same id
    assert read.get("browser_restarted") is True
    assert "browser_restarted" not in tools.browser_read(page, _NOZZLE, "text")


def test_a_trust_for_one_conversation_does_not_reach_another(
    conversation: Any, browser: Any, tls_site: dict[str, Any]
) -> None:
    from clio_agent.gact import context

    app, sid, client = conversation
    page = _untrusted(tls_site["c"] + "/status").detail["page"]
    client.post(f"/v1/sessions/{sid}/browser/pages/{page}/trust-certificate", json={})
    _wait_for(lambda: browser.pages[page].page.url.endswith("/status"))
    other = client.post("/v1/sessions", json={"title": "Other"}).json()["id"]
    token = context.set_session_id(other)
    try:
        # The browser trusts the key now, so the page loads; the agent here may not use it.
        opened = tools.browser_open(url=tls_site["c"] + "/status")
        with pytest.raises(BrowserError) as refused:
            tools.browser_read(opened["page"]["id"], _NOZZLE, "text")
    finally:
        token.var.reset(token)
    assert refused.value.reason == "browser_certificate_untrusted"


def test_a_device_with_a_new_certificate_is_asked_about_again(
    conversation: Any, browser: Any, tls_site: dict[str, Any]
) -> None:
    _app, sid, client = conversation
    page = _untrusted(tls_site["c"] + "/status").detail["page"]
    client.post(f"/v1/sessions/{sid}/browser/pages/{page}/trust-certificate", json={})
    _wait_for(lambda: browser.pages[page].page.url.endswith("/status"))
    before = tls_site["fingerprint"]()
    tls_site["tls"].regenerate()
    assert tls_site["fingerprint"]() != before
    with pytest.raises(BrowserError) as refused:
        tools.browser_navigate(page, url=tls_site["c"] + "/status?again=1")
    assert refused.value.reason == "browser_certificate_untrusted"
    assert refused.value.detail["fingerprint"] == tls_site["fingerprint"]()


def test_proceeding_through_chromiums_warning_is_not_a_grant(
    conversation: Any, browser: Any, tls_site: dict[str, Any]
) -> None:
    page = _untrusted(tls_site["c"] + "/status").detail["page"]
    record = browser.pages[page]

    async def proceed() -> None:  # what the person does in the live view
        await record.page.click("#details-button")
        await record.page.click("#proceed-link")
        await record.page.wait_for_load_state("load")

    browser.wait(proceed())
    _wait_for(lambda: (record.certificate or {}).get("state") == "insecure-broken")
    with pytest.raises(BrowserError) as refused:
        tools.browser_read(page, _NOZZLE, "text")
    assert refused.value.reason == "browser_certificate_untrusted"
    tools.browser_navigate(page, url="about:blank")  # leaving the page is allowed


def test_a_host_that_does_not_answer_says_where_the_agent_runs(
    conversation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "127.0.0.1,octopi.local")
    with pytest.raises(BrowserError) as refused:
        tools.browser_open(url="http://127.0.0.1:59999/")
    detail = refused.value.detail
    assert refused.value.reason == "browser_host_unreachable"
    assert detail["error"] == "ERR_CONNECTION_REFUSED" and detail["private"] is True
    assert detail["agent_host"] and detail["agent_host"] in refused.value.message
    assert "agent's network, not the person's" in refused.value.message
    with pytest.raises(BrowserError) as unresolved:
        tools.browser_open(url="http://octopi.local/")
    assert unresolved.value.reason == "browser_host_unreachable"
    assert unresolved.value.detail["error"] == "ERR_NAME_NOT_RESOLVED"
    assert unresolved.value.detail["private"] is True


def test_a_deployment_target_is_named(conversation: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "127.0.0.1")
    monkeypatch.setenv("CLIO_DEPLOYMENT_TARGET", "Polaris")
    with pytest.raises(BrowserError) as refused:
        tools.browser_open(url="http://127.0.0.1:59999/")
    assert refused.value.detail["target"] == "Polaris"
    assert "infrastructure target Polaris" in refused.value.message


def test_a_certificate_grant_names_the_exact_host_and_key() -> None:
    row = {
        "kind": KIND_BROWSER_CERTIFICATE,
        "scope": "workspace",
        "scope_id": "ws_1",
        "host_pattern": "octopi.lan",
        "fingerprint": "abc=",
        "action": "allow",
    }

    def action(host: str, key: str) -> str:
        return resolve(
            KIND_BROWSER_CERTIFICATE, host, path=key, policies=[row], workspace_id="ws_1"
        )

    assert action("octopi.lan", "abc=") == "allow"
    assert action("octopi.lan", "other=") == ""
    assert action("other.lan", "abc=") == ""
    assert resolve("browser_host", "octopi.lan", policies=[row], workspace_id="ws_1") == ""


def test_browser_grants_keep_their_kind_when_policies_reload() -> None:
    rows = [
        {
            "kind": "browser_host",
            "scope": "session",
            "host_pattern": "a.example",
            "action": "allow",
        },
        {
            "kind": KIND_BROWSER_CERTIFICATE,
            "scope": "session",
            "host_pattern": "b.example",
            "fingerprint": "abc=",
            "action": "allow",
        },
        {"scope": "workspace", "host_pattern": "c.example", "action": "allow"},
    ]
    clean, errors = _validate_permission_policies(rows)
    assert errors == []
    assert [row["kind"] for row in clean] == ["browser_host", KIND_BROWSER_CERTIFICATE, "domain"]


@pytest.mark.marketplace
def test_nothing_in_clio_turns_certificate_checks_off() -> None:
    """W24: no tool argument, setting or plugin reaches a context-wide bypass."""
    root = REPO_ROOT
    banned = ("ignore_https_errors", "setIgnoreCertificateErrors", "ignoreHTTPSErrors")
    found = [
        f"{path.relative_to(root)}: {word}"
        for folder in (root / "src", MARKETPLACE_ROOT / "plugins")
        if folder.is_dir()
        for path in folder.rglob("*")
        if path.suffix in {".py", ".md", ".json", ".ts"}
        for word in banned
        if word in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert found == []
