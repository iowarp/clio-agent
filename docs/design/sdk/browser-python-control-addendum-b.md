# In-app browser with Python control: addendum B

This addendum amends [browser-python-control-plan.md](browser-python-control-plan.md) and [addendum A](browser-python-control-addendum-a.md) before the browser branch merges. It comes from clio-agent issue #1908, where a user asked CLIO to watch her 3D printer through OctoPrint, served over `https://` at a LAN address. It was checked against `claude/gracious-goldberg-gagoo4`, where `gact/browser/` has no handling for certificate errors and reports every failed navigation the same way. Where this addendum and the earlier documents disagree, this addendum wins. Record each change as a `BR` decision in `docs/design/sdk/browser-ledger.md`, citing this addendum.

The issue's other request, a live dashboard that CLIO keeps current without a model turn per update, is not part of this addendum or of item (g). It belongs to the clio-relay and task rework, together with live views of running simulations and the NDP event subscriptions.

## 1. Untrusted certificates are the person's decision

Devices on a home or lab network (OctoPrint, Home Assistant, a NAS, a BMC, a Jupyter server) usually serve a self-signed certificate. Today the managed Chromium stops at the interstitial and the agent receives a generic navigation failure.

**The rule.** Trusting a certificate Chromium does not trust is a decision only the person makes, in the same spirit as rule 5 (the person signs in). The agent never bypasses a certificate error, and no tool argument, setting or fragment lets it.

**What the agent receives.** A navigation that fails with a `net::ERR_CERT_*` error raises `browser_certificate_untrusted` in the script, carrying the host, Chromium's error name, the issuer and subject, and the certificate's SHA-256 SPKI fingerprint. The message tells the agent to ask the person to look at the page in the shared tab. The page stays open on Chromium's interstitial so the person sees it there.

**What the person does.** In the live view the person sees Chromium's own interstitial. The panel adds a control beside it, "Trust this certificate", with the host, issuer and fingerprint on hover, offering this conversation or this workspace. Accepting writes a policy row of `kind="browser_certificate"` with the host and the SPKI fingerprint, over the existing row shape in `grant_resolver.py`, and reloads the page. The grant is pinned to the fingerprint, not the host, so a device that presents a different certificate later is asked about again. The person clicking through Chromium's own "Proceed" link is not a grant; it lasts only until that browser closes, and the agent is told nothing different.

**The mechanism.** Phase 0 picks it, in this order of preference, and records the choice:

1. Chromium's `--ignore-certificate-errors-spki-list` with the granted fingerprints, which trusts exactly those keys and nothing else. It is a launch flag, so adding a grant restarts the profile's browser. If that is the choice, the restart reopens the session's pages at their URLs, as `BrowserService.switch_browser` already closes and reopens, and the agent's next result on each page carries `browser_restarted`.
2. A per-request decision through CDP (the `Security` domain or `Fetch` interception) that accepts a certificate only when its fingerprint matches a grant, if Phase 0 finds one that works on Playwright 1.64.1 without restarting.

Playwright's `ignore_https_errors` and CDP's `Security.setIgnoreCertificateErrors` apply to the whole context, so neither is used.

**The `cdp` backend.** In the person's own Chrome the interstitial is Chrome's business. The agent still receives `browser_certificate_untrusted`, the person proceeds in their browser, and CLIO writes no grant.

## 2. Unreachable hosts say where the agent is

A page loads from the machine the agent runs on. A local agent reaches the person's LAN; an agent on a cluster or a remote server does not, and today it reports the same failure as a typo in the address.

A navigation that fails with `net::ERR_ADDRESS_UNREACHABLE`, `ERR_CONNECTION_TIMED_OUT`, `ERR_CONNECTION_REFUSED` or `ERR_NAME_NOT_RESOLVED` raises `browser_host_unreachable`, carrying the host, Chromium's error name, and where the agent runs: the GACT host name and, when the agent was started through an infrastructure target, that target's name. When the host is a private, link-local or loopback address, or ends in `.local` or `.lan`, the message adds that such addresses are resolved on the agent's network, not the person's. Nothing is guessed about where the person is, and no navigation is retried.

## 3. Idle pages: no change

Checked on the branch: the idle reaper (BR32, BR56) never counts a page with a live-view window open as idle, so a print the person watches stays open. A page nobody watches closes after `browser.idle_minutes`, and reopening it costs nothing for a device dashboard. A certificate grant from section 1 survives this, since it lives in the policy rows, not in the browser.

## Qualification additions

| ID | Scenario | Passes when |
| --- | --- | --- |
| W21 | The fixture site gains `c.fixture.localhost`, served over `https://` with a self-signed certificate. Plain request: "open the status page on c.fixture.localhost and tell me the temperature" | The script raises `browser_certificate_untrusted` with the fingerprint; the agent asks the person; the person accepts "Trust this certificate" for the conversation in the live view; the agent reads the value; the policy row has `kind=browser_certificate` and the fingerprint |
| W22 | Regenerate `c.fixture.localhost`'s certificate and ask again in the same conversation | The grant does not apply; the agent receives `browser_certificate_untrusted` again |
| W23 | A navigation to `10.255.255.1` (unroutable) from the cloud stack | `browser_host_unreachable` names the agent's host and says private addresses resolve on the agent's network; the agent's answer says so instead of blaming the address |
| W24 | `plugin check` and a repository search for `ignore_https_errors` and `setIgnoreCertificateErrors` under `src/` | None |

W21 and W22 run on the cloud stack. The OctoPrint case itself, a real self-signed device on a LAN with a local agent, is a local-phase row.

## Phase 0 additions

1. On Playwright 1.64.1, run both mechanisms in section 1 against the W21 fixture and record which one trusts only the granted fingerprint. If it is the launch flag, measure the restart and confirm pages reopen at their URLs.
2. Record how the service obtains the certificate's issuer, subject and SPKI fingerprint for a failed navigation (CDP `Security` or `Network` events, or a direct TLS handshake from the service).
3. List the Chromium error names a navigation to an unreachable, refused, unresolvable and timed-out host produces, and map them to `browser_host_unreachable`.
