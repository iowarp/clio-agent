# Storage sign-in: distributor setup and Delta qualification

The owner registered CLIO's Globus native application on October 4. Its public
client ID, `ca844cdc-ddb1-4332-bb66-69f9479d99b5`, is bundled with Agent so local
and remote installations need no per-host Globus application configuration.
The owner also supplied CLIO's Google **Desktop app** registration. Its application
ID and accompanying installed-app client value are bundled in
`src/clio_agent/gact/storage/oauth_clients.py`, alongside the Globus ID. Neither
provider requires users to register an application or configure environment variables.
Google user sign-in remains unqualified. Live Globus sign-in, collection consent,
browsing and a 14-byte HTTPS import passed; native collection-to-collection transfer
remains unqualified.
These are one-time distributor steps, not OAuth configuration for ordinary users.
Do not put credentials, consent return URLs, or tokens in issues or transcripts.

Registration check: the running browser preview's **Log in** control reached
Globus authorization using the registered CLIO client and code-return redirect.
The user completed sign-in with the real public Globus Tutorial Collection 1.
The first listing returned HTTP 403 `ConsentRequired` for that collection's
`data_access` dependency. CLIO now retains the token, validates required scopes
against the approved collection pair, and offers **Authorize collection**.
The public collection's additional consent completed through the existing browser
login. The shared external-link bridge no longer treats
a null `window.open` result with `noopener` as proof of a blocked
popup. Storage sign-in reserves a tab during the click and provides a direct
**Open sign-in page** fallback. Collection authorization and real transfers are
separate acceptance checks. The public sample folder is
`/home/share/godata/` on collection `6c54cade-bde5-45c1-bdea-f4bd71dba2cc`;
normal folder previews no longer recursively enter unopened directories.
The removed initial source was verified disconnected with no remaining saved
credentials. The replacement **Globus public sample files** source now lists
`file1.txt` (4 bytes), `file2.txt` (4 bytes), and `file3.txt` (6 bytes). Reopening it
in the browser required no sign-in. A fresh Python process loaded the persisted
credential store and listed the same files through the production adapter,
confirming reuse independently of browser/query state. Tokens and returned codes
were not included in the qualification output. See
[the browser evidence](globus-public-sample-browse.png), which also shows the
enlarged source logo.

The live preview has no receiving collection. After consenting to this source
collection's HTTPS scope through CLIO, **Transfer to CLIO** imported all three
files through `globusfs==0.2.1`. The UI reported **Available in workspace Files:
14 B / 14 B** and the Files tree showed all three files under **Connected data /
Globus public sample files**. The baseline sizes were verified as 4, 4 and 6 bytes.
A fresh Python process reused the saved collection credential to read `file1.txt`
through the production adapter. See [the completed import](globus-public-sample-import.png).
This qualifies the direct HTTPS path only. Native collection transfers,
expiry-triggered refresh, Google Drive and packaged Desktop acceptance remain open.

## Upstream library audit (October 4)

The initial implementation used custom OAuth configuration and native Globus
transfers. It did not use either of these upstream browser-login implementations:

- `gdrive-fsspec` delegates browser/cache authentication to
  `pydata_google_auth.get_user_credentials`. PyData supplies built-in Desktop and
  web application IDs; `use_local_webserver=False` uses its hosted code-return
  flow. An application ID identifies the requesting software, separately from
  the person signing in. PyData's application-author policy requires a product
  integration to use its own ID. No live Drive login has been qualified here.
- `saforem2/globusfs` supplies Globus's public tutorial native client ID in
  `login.py`, persists refreshable tokens, and requests both Transfer metadata
  and collection HTTPS scopes. Its HTTPS read path does not require a receiving
  collection. CLIO now uses its filesystem for direct file reads when no receiving
  collection is configured, supplying the selected source's private, refreshable
  credential. Saved browser accounts are reusable for the same user and CLIO
  host across workspaces; source roots and collection consent remain separate. Native
  SDK transfer jobs remain in use when receiving storage is configured. The host
  receiving settings below apply to native transfers only.

The existing CLIO **Log in** button must initiate user authorization. Application
registration is a one-time maintainer step, never an ordinary user's setup step.
Do not call sign-in functional based only on local callback tests.

Sources: [gdrive-fsspec](https://github.com/fsspec/gdrive-fsspec),
[PyData application policy](https://pydata-google-auth.readthedocs.io/en/latest/privacy.html#policies-for-application-authors),
[globusfs login](https://github.com/saforem2/globusfs/blob/main/src/globusfs/login.py).

## Google Drive

Public folder access through `gdrive-fsspec` also requires the CLIO distributor's
Google Drive API key (`CLIO_STORAGE_GOOGLE_API_KEY`). The OAuth Desktop client ID
is not an API key. Enable Google Drive API in the same project, create an API key,
and restrict its API access to Google Drive API. This is application configuration;
ordinary users should not create a Google Cloud project or supply their own key.
The existing filesystem receives this key through Google's client library and
retains its listing, pagination, and file reading implementation.

Google documents this in [List files in a public folder](https://developers.google.com/workspace/drive/api/guides/search-files#list_files_in_a_public_folder).
Missing/invalid application configuration and rate limits must not mark a folder
as private. CLIO checks parent-folder access before listing its children, because
an inaccessible parent can otherwise produce an empty listing. Sign-in remains
available when public access is not configured. Public access remains unqualified
until the application key is supplied; private access requires a real user grant.

1. In an IOWarp-owned Google Cloud project, enable **Google Drive API**. Set up the
   OAuth consent screen with CLIO branding, owner contact, homepage and privacy policy.
2. Use **External / Testing** for the Delta rehearsal and add the actual participating
   Google accounts as test users. The current folder browser requests `drive.readonly`
   for read-only sources and `drive` for working copies. These are restricted scopes;
   broader distribution needs the applicable verification. `drive.file` is narrower
   but does not grant this browser access to arbitrary existing folders.
3. Create an OAuth client of type **Desktop app** for the loopback/PKCE flow. This is
   complete for CLIO: the supplied native registration ships with the application.
   Google's installed-app documentation explicitly describes embedding the client ID
   and its accompanying client value. The latter is non-confidential for this client
   type; it is not a service-account key, web-client secret, or user access token.
   Per-user access and refresh tokens remain in the private account store.
4. In Desktop, **Log in** starts a loopback receiver on the user's computer and
   opens the system browser. The callback is captured automatically and delivered
   only to the initiating connected CLIO. Google uses an ephemeral loopback port.
5. For standalone browser qualification only, on the **computer running the browser** run:

   ```powershell
   uv run --no-sync python scripts/storage_oauth_return.py
   ```

   This ten-minute helper binds only `127.0.0.1:48173`, has no outbound requests,
   and never logs or stores return URLs. It is the manual callback fallback for
   qualification. The packaged Desktop uses its built-in receiver instead.
6. The browser qualification fallback defaults to
   `http://127.0.0.1:48173/clio-storage-return`. Start sign-in from **Connect data**,
   authorize in the system browser, then paste the return URL from the helper page
   into that same CLIO dialog. With a remote CLIO, the helper still runs on the laptop;
   the initiating CLIO privately exchanges the PKCE code over its authenticated route.

References: [Google installed-app registration](https://developers.google.com/identity/protocols/oauth2#installed),
[Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app),
[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
[testing and verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

## Globus

1. An IOWarp owner opens [Globus developer settings](https://app.globus.org/settings/developers),
   creates/selects the CLIO project, and registers CLIO as a **Thick Client / native app**.
2. Register both `http://localhost:48173/clio-storage-return` (Desktop callback)
   and `https://auth.globus.org/v2/web/auth-code` (standalone browser code fallback).
   Record the public client ID. The native PKCE flow needs no confidential client secret.
3. The registered client ID is bundled with CLIO. Custom distributions can override
   it with `CLIO_STORAGE_GLOBUS_CLIENT_ID` (an explicit empty value disables it).
   The integration requests Transfer
   access plus `offline_access`. Direct reads request the selected collection's
   HTTPS scope through incremental consent. Only Transfer and that collection's
   resource-server tokens are kept in the private account credential store.
4. Start sign-in from CLIO and authorize in the system browser. Desktop captures the
   callback automatically; the standalone browser fallback asks for the displayed
   code in the initiating source's setup dialog. Source/user/CLIO/host binding and
   PKCE still apply. Do not paste the code into an agent conversation.

Reference: [Globus native registration and PKCE](https://docs.globus.org/api/auth/developer-guide/).

## Host configuration

Both native application registrations are included with CLIO. These environment
variables are optional overrides for custom distributions, not user setup steps.
Changing the Google client ID also clears the bundled client value: provide the
matching value for the replacement registration. An explicitly empty client ID
disables that provider's sign-in. Restart after changing distributor configuration.

| Variable | Value |
| --- | --- |
| `CLIO_STORAGE_GOOGLE_CLIENT_ID` | Optional override of bundled CLIO Google Desktop app ID |
| `CLIO_STORAGE_GOOGLE_CLIENT_SECRET` | Optional matching installed-app client value |
| `CLIO_STORAGE_GOOGLE_REDIRECT_URI` | Optional override of `http://127.0.0.1:48173/clio-storage-return` |
| `CLIO_STORAGE_GLOBUS_CLIENT_ID` | Optional override of bundled IOWarp native app ID |
| `CLIO_STORAGE_GLOBUS_REDIRECT_URI` | `https://auth.globus.org/v2/web/auth-code` (also the default) |

The trusted UI reports both bundled providers as configured. User credentials are
stored separately under the connected CLIO's namespaced private storage and are
never included in source references or agent tool results. Saved accounts can be
reused across workspaces; additional permissions still require user consent.

The Google registration tests exercise the real storage routes, both default and
Desktop loopback redirects, PKCE exchange and token refresh, with only Google's
HTTP responses replaced. They check custom registration pairing and that application
client values do not appear in UI responses or the user-token vault. This does not
qualify a real Google consent exchange. All 37 focused registration, authentication,
account, public Drive and storage HTTP tests pass, as do Ruff and focused mypy.
A built wheel was inspected and contains both native registrations and their
configuration reader. The existing browser preview still needs
to load this code and its qualification callback helper; Desktop already supplies
the callback receiver. Restarting the existing preview was blocked by automatic
approval review, so no claim of live Google sign-in is made.

## Globus receiving storage

Receiving collection configuration is host-owned, not part of each attachment.
The Agent uses the Globus SDK to detect an existing Globus Connect Personal
installation on its own machine, including Windows drive-path translation. It
does not assume the browser's computer is the receiving host or change GCP access
permissions. In **Infrastructure / Models & storage**, the connected host's
**Globus receiving storage** panel can save an institutional collection mapping
once. The mapped local folder must contain CLIO Agent's namespaced source store.
Before submitting a fresh native transfer, a random local marker must be visible
through the receiving collection. A resumed native request retains its original
destination even if host settings change. Files enter the workspace only after
native completion and local validation through the normal materialization flow.

This detects existing receiving software; it does not claim to install or
provision a new Globus Connect endpoint. A host without GCP or a configured
institutional collection can still sign in and browse a source, but cannot run a
native collection transfer until receiving storage is available. Collections that
offer HTTPS reads can instead use **Link folder** through globusfs without setting
up a receiving endpoint. **Download file / Download all** use native Globus jobs
and appear only when receiving storage is configured. The older HTTPS import
evidence above predates this explicit split; existing copies remain available.
See [linked-folder qualification](linked-folders.md).

## Live acceptance after registration

### GitHub App registration

Register one public CLIO GitHub App. Individual users sign in with their own accounts
and install the app for their chosen repositories; they do not register separate apps.

- Repository **Contents: Read and write**; **Metadata: Read-only**. Other permissions
  remain unset, including Workflows. This supports the agreed reviewed-write workflow.
- Enable **Device Flow** and **Expire user authorization tokens**.
- Leave **Request user authorization (OAuth) during installation** unchecked. CLIO's
  sign-in button obtains a user access token through Device Flow instead.
- Leave Redirect URI and Setup URL blank, wildcard matching and Redirect on update off.
- Turn **Webhook Active** off and select **Any account** for installation.
- The shipped public Client ID is `Iv23lieBkZ0yxGbLAIyE`, App ID `5190371`, with
  public page `https://github.com/apps/clio-agent`. Device Flow and its refresh
  requests do not require a client secret or an app private key.
- Distributors can override `CLIO_STORAGE_GITHUB_CLIENT_ID` and
  `CLIO_STORAGE_GITHUB_APP_URL` together for a different registration.
  **Repository access** opens the registered app's installation picker.

The registration UI observed on October 4 still required a Webhook URL with Active
unchecked. If that validation occurs, use `https://clio.iowarp.ai/` as an inactive
placeholder and retain Active off. The site is not a webhook receiver.

The user opens GitHub from CLIO's sign-in button and enters the displayed code.
The CLIO host polls GitHub; this supports both local and remote hosts without an
inbound callback. Authentication is reusable across that user's workspaces.
Public read-only links still work without signing in. Working copies require sign-in;
edits use the existing review/apply workflow, with conditional file revisions and
GitHub's default authenticated-user attribution. Ordinary links remain read-only.

Live registration check on October 4: GitHub issued a device authorization using
the bundled Client ID, confirming Device Flow is enabled. No account authorization
or repository write was performed. The app page still reported private at that
check; its owner can change **Advanced → Danger zone → Make public** to allow other
accounts to install it. Real user sign-in, private reads, and reviewed remote writes
remain live acceptance steps. Unit/integration coverage uses real fsspec and the
shared storage engine with only provider HTTP boundaries replaced in tests.

References: [GitHub registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app),
[user device authorization](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app),
[file updates](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents).

### Provider acceptance

Use a small disposable folder owned by the tester. Check sign-in, browsing and download;
read-only rejection; working-copy review and upstream conflict detection; reconnect/token
refresh; source disconnect; and authorization cancellation. Repeat through the Delta SSH
route with the materialized data on that node. Test a connection/account switch during
sign-in: it must not attach credentials to the replacement host or account. For Globus,
retain the native task ID and confirm its terminal transfer status. Some collections
need additional consent/authentication; a refusal must remain visible, never called success.

The helper and adapter tests prove local mechanics only. Globus registration,
sign-in, public tutorial collection consent, browsing and persisted-token reuse
have live evidence above. Google registration/consent, restricted-scope readiness,
remote transfers, and expiry-driven token refresh remain external qualification gates.
