# Linked folders and explicit downloads, October 4

The integration checkout separates **Link folder** from **Download file** and
**Download all**. Linking indexes metadata, exposes a virtual folder in CLIO Files,
and fetches only a requested file when opened or attached. This does not install
an operating-system filesystem mount. Agent access uses `connected_data_open`
and the existing immutable resource read tools.

| Provider | Linked file access | Explicit copies |
| --- | --- | --- |
| Globus | `globusfs` with source-bound credentials | Native Globus transfer jobs when receiving storage is configured |
| Google Drive | `gdrive-fsspec` with CLIO's private approved token | Existing Drive transfer/export adapter |
| SSH/SFTP | fsspec `SFTPFileSystem` over the verified SSH connection | Existing SFTP transfer adapter; Desktop retains its native SSH path |
| GitHub | fsspec `GithubFileSystem`, public repositories | Not offered |
| Local folder | fsspec `LocalFileSystem` | Existing local copy adapter |

Selected downloads retain earlier copies and fetch only selected remote files.
Links use a separate revision from downloaded snapshots. Refresh is explicit;
changed upstream metadata rejects a stale linked-file read. Per-file locks prevent
concurrent previews from overwriting a read-only cached file. Disconnecting prevents
linked reads while preserving copies and attached evidence.

Google-native documents retain the existing PDF/XLSX/PPTX/PNG export formats. A
small compatibility override uses the Google client constructed by gdrive-fsspec,
because the upstream export helper supplies an unsupported `supportsAllDrives`
argument. Ordinary files use the library's buffered read implementation.

## Live evidence

- Saved Globus sign-in was reused for the public tutorial folder
  `/home/share/godata/`. Linking read metadata; attaching `file1.txt` fetched four
  bytes through globusfs. The linked cache contained only `file1.txt`.
- GitHub linked `https://github.com/fsspec/gdrive-fsspec`. The picker offered linking
  without download controls. Opening README.md from **Linked folders** rendered
  the actual document. Its linked cache contained only README.md.
  [Picker](github-fsspec-linked.png), [file preview](github-fsspec-file-open.png).
- Homelab `/data/clio-beta3-sftp-check-20261004` passed a real fsspec SFTP metadata
  link, a 71-byte read, and an independent selected-file download. Evidence was
  retained at `C:/Users/jaime/AppData/Local/Temp/clio-fsspec-sftp-check-1bw4r2o4`.

## Automated checks

108 distinct focused backend tests passed across connected storage, authentication,
browser SFTP, SSH targets, Globus consent/destination/library access, lazy links,
setup tools and tool instrumentation. Twenty focused UI tests passed for the
picker and shared SSH target editor. The schema change passed ten focused tests.
UI/core lint, workspace type checking, component reuse and file-size guards,
Python Ruff and focused mypy checks passed.

Tests cover metadata-only linking, single-file fetching, source ownership,
credential isolation, upstream changes, retained selected downloads, exact native
Globus request contents, Google library reads/exports, and capability-driven UI.

Google account sign-in and native Globus receiving transfers remain live acceptance
gates. They are not reported as passed. This is integration-checkout evidence, not
packaged-build, develop-merge or publication qualification. Schemas is pinned to
`67e9c3760de287c45d481e5d8b31ebee28883eb3`.

## Source management and message handoff follow-up

Files > Sources now starts at the source list on every opening, independently of
the composer's selected source. It offers management of existing sources, with
new connections, uploads and downloads confined to Attach. Completed transfer
history no longer claims a removed workspace copy is available.

Folders use the existing immutable resource-reference message pipeline. Attaching
a linked or downloaded folder retains a bounded metadata index, source identity
and revision. The agent's resource grounding identifies the folder and its
`connected_data_open` read path. Linked reads use the existing fsspec backend;
downloaded reads use the approved baseline. Stale revisions and removed sources
are rejected. No new message-part kind or provider implementation was introduced.

Live browser checks on 2026-10-04:

- Removed a generated 27-byte workspace copy. Its detail changed to “No downloaded
  copy in this workspace.” Reopening Sources returned to the list.
- Attached the existing public Globus folder, producing a named composer reference.
- Selected a generated directory through the browser folder chooser and uploaded
  it. The completed upload automatically added its folder reference to the draft.
- Removed the two generated test sources and their uploaded test resources after
  verification. The existing Globus source and its sign-in were preserved.

Evidence: [management list](sources-management-list.png),
[removed copy](source-copy-removed.png), [folder references](folder-reference-composer.png).
The browser check stopped at the composed message. Agent grounding and individual
file reads were exercised by integration tests, without a live model invocation.

This follow-up passed 74 distinct focused Python tests and 28 UI tests. Workspace
type checking, UI/core lint, component reuse/file-size guards, Ruff, focused mypy,
and both repositories' diff checks passed.

## Folder attachment cards

Linked and downloaded folder references now appear as removable folder cards in
the composer attachment area. The existing resource-reference message pipeline
still carries their identity and approved revision to the agent. Source files
selected through Attach use the same tray with a file icon.

Download all hands its operation to the composer. Its progress card survives
closing Attach, then becomes a downloaded-folder attachment when the operation
and resource receipt finish. Sending waits until pending attachments are ready
or removed. Removing a progress card now withdraws its provisional workspace
data and cancels its pending transfer. A late receipt cannot restore the removed
attachment, and editing message text preserves existing cards. The cleanup
verification below supersedes the earlier attachment-only removal behavior.

Live browser review attached the downloaded public Globus folder and the linked
GitHub documentation folder together. Both appeared as compact folder cards.
Evidence: [downloaded and linked folder attachments](folder-attachment-cards.png).

Thirty-five distinct focused UI tests passed across the source picker, composer,
reference selection and new attachment lifecycle tests. Two backend integration
tests passed for folder grounding and approved file reads. Workspace type
checking, UI lint, component reuse and file-size checks passed. The browser
verification stopped at the draft; agent grounding was tested without invoking
a live model. The close-during-download lifecycle was exercised by the UI tests.

## Reusing sources and browsing attached folders

The collapsed source list now offers **Add to message** for reusable sources.
The source detail offers the same action for the current folder. Subfolder
references retain only that folder's index and carry its path to agent grounding
and `connected_data_open`; paths outside that selection are rejected when that
folder is supplied. Whole-source approval remains the existing access boundary.

Clicking a folder attachment opens its source browser at the selected folder and
file location. Reusing an existing link does not refresh or download it. Link and
Download have adjacent plain-language tips; source-file copy actions say
**Download file**, while existing copies use **Add to message**. Selected-file
downloads use the existing transfer capability and attach only that file after
completion. Folder downloads retain the selected subfolder in the attachment.

Live browser verification reused the public Globus source from the list, reopened
it from its attachment card, and completed the real 71-byte Homelab SFTP folder
download. The download card stayed in the composer after closing Attach and
became a ready folder attachment. Source configuration was preserved and the
verification attachments were removed from the draft afterward.

Evidence: [source reuse list](source-reuse-list.png),
[Link and Download help](source-link-download-actions.png),
[completed download attachment](source-download-message.png).

Twenty-nine focused UI tests and thirteen backend tests passed. They cover root
and subfolder reuse, reopening attached folders, selected-file transfer handoff,
plain-language help, folder grounding and file reads. The subfolder checks also
exposed and fixed Windows long-path handling in the linked-file cache. Workspace
type checking, UI/core lint, focused mypy, Ruff, component reuse and file-size
checks passed. No live model was invoked.

## GitHub request reuse and rate-limit handling

The GitHub adapter now retains the real fsspec filesystem and its directory
cache between operations, bounded to sixteen repositories and five minutes.
Ordinary source browsing reads only the selected directory. Searching and the
first link reuse those listings; explicitly refreshing a link invalidates them.
Opening a linked GitHub file no longer scans the entire repository. Its bytes
are checked against the approved Git blob hash, and previously cached approved
bytes can be reopened without contacting GitHub.

Rate-limit responses stop further GitHub requests until the provider's
Retry-After or reset time, with increasing delays when neither is supplied.
The UI receives a plain-language wait message. Public anonymous access still
has GitHub's shared sixty-request hourly limit, so a large initial repository
index can still exhaust it. This change does not introduce GitHub authentication.

Seven request-count regression cases exercise the real fsspec implementation
with only HTTP responses replaced in tests. Repeated root browsing uses two
requests total, repeated nested browsing adds one, and subsequent searches and
linking reuse that metadata. A linked file adds one content request; reopening
the cached file adds none. Other cases cover restart behavior, changed-byte
rejection, and primary, secondary and Retry-After cooldowns. Nine existing
linked-storage and folder-reference tests also passed, as did Ruff, focused
mypy and the diff check.

Live browser verification on 2026-10-04 opened the existing public GitHub source
and browsed src/gdrive_fsspec, showing its two Python files without the previous
403. Its unlinked state and the user's draft were preserved. No live model was
invoked. Evidence: [GitHub folder browsing](github-cached-browse.png).

## Unsent attachment cleanup

New links, source downloads, ordinary uploaded files and uploaded folders remain
provisional until a message accepts their resource references. Removing their
composer card rolls back the data introduced by that draft, including the
source-named entry in Files and empty CLIO-owned storage wrapper. Removing a
pending download requests cancellation and waits for the transfer owner to
settle before deleting its bytes. Late upload requests and resource receipts
cannot recreate a removed attachment.

Concurrent drafts share ownership until the last unsent attachment is removed.
Successful message acceptance preserves the data, including after API restart;
failed sends leave the attachment retryable. Reusing data already retained by a
sent message does not make that data disposable. Edited working copies are
protected from automatic deletion. Remote originals and saved sign-ins are
unchanged. Merely closing Attach keeps the draft.

Live browser checks on 2026-10-04 removed a new GitHub link, an ordinary generated
file and an uploaded generated folder using the composer remove buttons. API
and disk checks confirmed removal of the resource/copy and its parent wrapper,
while the original files and preexisting Globus/SFTP data remained. The source
list and Files refreshed without the removed entries. Evidence:
[removed link](removed-draft-link.png), [removed folder](removed-draft-folder.png).

Focused checks passed for source draft cleanup, resource HTTP routes, ordinary
resources, the picker, source attachment cards, composer file handling, upload
preparation and session mutations. Type checking, UI lint, Ruff, focused mypy,
component reuse and file-size guards passed. Browser checks stopped at the draft;
message retention and agent handoff were tested without a live model invocation.

## Reusable provider accounts

Google Drive and Globus sign-ins belong to the same user and connected CLIO host,
and can be reused across workspace sources. Existing source-bound OAuth records
are migrated inside the private credential store. Sources and folder permissions
remain workspace-owned; saved accounts do not grant the agent access to arbitrary
folders. Existing sources retain their chosen account if another login is added.
Read-only Drive credentials do not authorize write access.

Provider rows show a signed-in checkmark and separate Sign in / Sign out from
Connect. Independent sign-in uses the existing browser and Desktop PKCE flow and
does not create a source. Removing or disconnecting a source preserves saved
accounts; provider sign-out clears that user's provider grants across this CLIO's
workspaces. Pending callbacks cannot restore an account after sign-out or discard.

Public Drive folders use the existing gdrive-fsspec anonymous mode for browsing
and linking; a provider refusal prompts for sign-in. GitHub public folders remain
anonymous. Globus HTTPS supports known public file reads, but collection listing
uses the Transfer API, so CLIO's Globus folder picker still needs sign-in.

Focused backend tests cover migration, restart reuse, ownership isolation,
source removal/reconnection, provider sign-out, scope separation and anonymous
Drive access/refusal. UI tests cover independent login, refreshed status, global
sign-out confirmation, collection consent and public browsing; Desktop callback
regression checks also pass. The running preview's backend restart was rejected
by automatic approval review ("blocked by policy"), so live account migration and
the existing Globus checkmark remain unverified until that preview is restarted.
