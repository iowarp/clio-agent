# Storage sign-in: distributor setup and Delta qualification

Owner confirmed on October 4 that CLIO has no Google or Globus applications yet.
The integrations are implemented, but live sign-in and transfers remain unqualified.
These are one-time distributor steps, not OAuth configuration for ordinary users.
Do not put credentials, consent return URLs, or tokens in issues or transcripts.

## Google Drive

1. In an IOWarp-owned Google Cloud project, enable **Google Drive API**. Set up the
   OAuth consent screen with CLIO branding, owner contact, homepage and privacy policy.
2. Use **External / Testing** for the Delta rehearsal and add the actual participating
   Google accounts as test users. The current folder browser requests `drive.readonly`
   for read-only sources and `drive` for working copies. These are restricted scopes;
   broader distribution needs the applicable verification. `drive.file` is narrower
   but does not grant this browser access to arbitrary existing folders.
3. Create an OAuth client of type **Desktop app** for the loopback/PKCE flow. Keep the
   downloaded configuration private. Provision its client ID and, if supplied, client
   secret on the connected CLIO through its process environment (below).
4. On the **computer running the browser**, from this checkout run:

   ```powershell
   uv run --no-sync python scripts/storage_oauth_return.py
   ```

   This ten-minute helper binds only `127.0.0.1:48173`, has no outbound requests,
   and never logs or stores return URLs. It is the manual callback fallback for
   qualification; it does not claim automatic Desktop callback integration.
5. Set the connected CLIO's redirect to exactly
   `http://127.0.0.1:48173/clio-storage-return`. Start sign-in from **Connect data**,
   authorize in the system browser, then paste the return URL from the helper page
   into that same CLIO dialog. With a remote CLIO, the helper still runs on the laptop;
   the initiating CLIO privately exchanges the PKCE code over its authenticated route.

References: [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app),
[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
[testing and verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

## Globus

1. An IOWarp owner opens [Globus developer settings](https://app.globus.org/settings/developers),
   creates/selects the CLIO project, and registers CLIO as a **Thick Client / native app**.
2. Register `https://auth.globus.org/v2/web/auth-code` as its redirect. Record the public
   client ID. The native PKCE flow needs no confidential client secret.
3. Provision the client ID on the connected CLIO. The integration requests Transfer
   access plus `offline_access`; it keeps only the Transfer resource-server tokens.
4. Start sign-in from CLIO, authorize in the browser, and paste the displayed code
   into the initiating source's setup dialog. Source/user/CLIO/host binding and PKCE
   still apply. Do not paste the code into an agent conversation.

Reference: [Globus native registration and PKCE](https://docs.globus.org/api/auth/developer-guide/).

## Host configuration

Provision these variables in the environment of the **CLIO Agent process**, including
the allocated node when using Delta. Restart that process after provisioning. Use the
host's private service environment/credential mechanism; no repository `.env` files.

| Variable | Value |
| --- | --- |
| `CLIO_STORAGE_GOOGLE_CLIENT_ID` | IOWarp Google Desktop app ID |
| `CLIO_STORAGE_GOOGLE_CLIENT_SECRET` | Value supplied with that client, if required; keep private |
| `CLIO_STORAGE_GOOGLE_REDIRECT_URI` | `http://127.0.0.1:48173/clio-storage-return` |
| `CLIO_STORAGE_GLOBUS_CLIENT_ID` | IOWarp native app ID |
| `CLIO_STORAGE_GLOBUS_REDIRECT_URI` | `https://auth.globus.org/v2/web/auth-code` (also the default) |

The trusted UI reports an unconfigured distributor application until the relevant
ID and redirect exist. Credentials are stored separately under the connected CLIO's
namespaced private storage and are never included in source references or agent tool results.
Changing the selected source root or access mode requires fresh authorization.

## Live acceptance after registration

Use a small disposable folder owned by the tester. Check sign-in, browsing and download;
read-only rejection; working-copy review and upstream conflict detection; reconnect/token
refresh; source disconnect; and authorization cancellation. Repeat through the Delta SSH
route with the materialized data on that node. Test a connection/account switch during
sign-in: it must not attach credentials to the replacement host or account. For Globus,
retain the native task ID and confirm its terminal transfer status. Some collections
need additional consent/authentication; a refusal must remain visible, never called success.

The helper and adapter tests prove local mechanics only. Registration, provider consent,
restricted-scope readiness, collection permissions and real remote transfer are external
qualification gates; no such check has passed yet.
