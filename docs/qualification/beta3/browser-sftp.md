# Browser SFTP qualification (October 4)

The browser attachment flow reuses the shared SSH host editor, infrastructure
host records, and folder picker. Its Test connection, folder browsing, and source
reads use the connected CLIO's existing fsspec/Paramiko SFTP adapter. They do not
call Desktop's native SSH commands. Desktop retains the existing interactive
OpenSSH connection, route editor, authentication prompts, and transport bridge.

Browser authentication offers SSH key or Password. Choose key file opens the
browser's file picker, reads the selected key in memory, and optionally accepts
its passphrase. Without a selection, it uses keys or an agent configured on the
CLIO host. A selected key or password travels only to the connected CLIO's trusted
API, never into source metadata, transcript content, or model input. Source-bound
credentials remain in memory until disconnect or backend restart; the same source
can then sign in again without recreating its imported data.

The authentication info icon explains the connection origin and that interactive
Duo/2FA, security-key prompts, and Kerberos need Desktop. Browser hop connections
need noninteractive credentials configured on the CLIO host. Server host keys must
already be trusted by that host; the adapter never auto-accepts an unknown key.

## Live evidence

In the running browser preview, the tester selected the existing homelab key with
Choose key file. Test connection reported Connection succeeded. The folder picker
browsed homelab, selected the owned `/data/clio-beta3-sftp-check-20261004` folder,
and connected it as Homelab SFTP check. Transfer to CLIO imported its 71-byte file.
Workspace Files displayed Connected data / Homelab SFTP check /
connection-check.txt, and opening it rendered the expected qualification text.
After restarting the backend, the same source requested sign-in while retaining
the imported file. Selecting the key again reconnected successfully, and the file
remained readable without importing it again.

- [Selected key and successful connection](browser-sftp-key-tested.png)
- [Completed import](browser-sftp-homelab-import.png)
- [File opened in the workspace](browser-sftp-workspace-file.png)
- [Reconnected after backend restart](browser-sftp-reconnected.png)

Globus public sample files were also opened from Workspace Files after their
14-byte import; file1.txt rendered `one`. See [the preview](globus-workspace-file-open.png)
and [Globus qualification](oauth-registration.md).

## Automated checks and limits

The focused backend checks passed 43 distinct tests across the storage and HTTP
error suites. Tests use an actual local SFTP server to exercise password and key
authentication, browsing, byte-for-byte materialization, sanitized login failures,
source credential binding, and disconnect/restart behavior. The HTTP reconnect
case additionally switches the same source from password to key after disconnect.
Malformed requests are checked to ensure validation errors never echo credentials.

The focused UI checks passed 67 distinct tests. Coverage includes the real shared
editor, key picker, password
input, browser authentication tooltip, private credential request separation,
Desktop host picker and deployment regressions, and connected-source lifecycle.
The pending-file-read case prevents an old key selection from overriding a switch
to password. Workspace type checking, UI lint, and focused Python Ruff/mypy checks
also passed during this qualification.

Live homelab testing used a key. Password authentication was exercised against the
real local test server, not a homelab password. Interactive MFA, packaged Desktop,
browser hop authentication, Google Drive consent, and native Globus transfers are
not claimed as passed by this record.
