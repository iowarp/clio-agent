# Endpoint operations and stream acceptance

The Ares deployment is ours to administer. Its Unix/Docker operator identity is
separate from an NDP identity-service role. The saved test identity validates
successfully, but the deployment still referred to an earlier registration group.
An explicit local administrator ACL now recognizes the exact validated subject,
scoped to the configured test identity authority. Authentication and downstream
catalog authorization remain enabled. No development token or fabricated role was
used, and central identity-service group administration was not required.

Private receipts, unedited message ledgers, exact prompts and native HTML exports
are retained under `D:/CLIO-Develop/ndp-owned-qualification-20261019/`.

## Repairs and their boundaries

| Observed problem | Repair and verification |
| --- | --- |
| An empty workspace could not expose the tool needed to connect its first Endpoint. | Root sessions receive the setup-only connection tool. Operational schemas activate after connection; this does not grant unrelated specialist tools. Fresh visible Sol conversations connect using the ordinary SSH request. |
| CLIO rejected the deployed API's actual multipart binary schema before upload. | Recognize JSON Schema `contentMediaType: application/octet-stream` as well as the older binary format. Real file uploads and SHA-256/byte-count readback succeed. |
| Service registration forced a destination organization the publishing credential could not write to. | Accept explicit catalog organizations for creation and updates; retain downstream permission checks. Configure the Endpoint's legitimately granted PRE catalog organization and report that destination in authenticated status. |
| The API and Kafka containers had separate networks, and no usable HTTP consumer interface existed. | Join the intended broker network and deploy an authenticated, bounded Kafka consumer-view API. Broker credentials remain on the Endpoint. This is an owned deployment extension, not a claim that every upstream NDP Endpoint already exposes these routes. |
| Waiting for records held a SQLite write transaction, delaying other readers. | Poll outside transactions; freeze one canonical response per checkpoint in a short commit. Stop/resume generations prevent an in-flight read from advancing a stopped reader. Concurrent-reader and racing-retry regressions pass. |
| Short request budgets and blank socket exceptions produced opaque failures. | Use a finite 30-second default budget; explain header timeouts and reserve time for SSH/identity validation. Native tools record the exception type when its message is empty. |
| A transient identity-service HTTP 500 interrupted a valid user's read. | Retry a read-only identity lookup once for transient 5xx responses. Rejection and persistent failure still fail closed; no prior identity grant is reused. |
| Finalization matched tool results by name/arguments and could overwrite an earlier failed attempt with a successful retry's body. | Match invocation IDs; allow only unambiguous legacy matches and preserve failures. The original diagnostic history remains unedited. |
| Portable maps lacked geographic context after remote tiles were removed. | The contained renderer includes coarse Natural Earth land geometry separately from scientific layers. Recorded points, charts, table rows and selection remain ordinary native views. This background is not scientific evidence or an analysis boundary. |

## Live Endpoint operations

The visible Sol acceptance conversation is `sess_a90526e00ebb`, workspace
`ws_77eb8a1da616`. It:

1. Connected over the saved SSH route and compared anonymous/account discovery.
2. Acquired Utah station metadata from the registered EarthScope service,
   selected 2,143 station epochs / 1,770 network-station identifiers / 33 networks,
   and rendered its map. A misspelled optional upstream parameter produced one
   HTTP 400, was corrected, and remains visible in the ledger.
3. Created `clio-field-research` and private dataset
   `c5d0fccb-0eff-4231-8563-e404a05d442c`, uploaded four actual files, and verified
   each by exact readback.
4. Created a separate public service-linked description and submitted it to the
   configured NDP test catalog. Returned ID:
   `7e72fa30-e63c-4666-89f1-52f0d0bc06a4`, status `submitted`.
5. Verified anonymous discovery of that public description while the private
   collection remained hidden, then updated private metadata without changing
   existing resource IDs or uploading the files again.

The intentional anonymous protected-read checks returned 401. They demonstrate
access enforcement, not another deployment failure. Test-catalog submission does
not claim approval or production/global publication. The station counts are
metadata coverage, not confirmation of live instruments or waveform availability.

A separate public-facing authoring conversation, `sess_4c0e22f7564d`, uses the
already acquired station collection to demonstrate reuse, new authoring and
publication. Its empty local workspace contains no prepared answer files. This
is not presented as an independent acquisition benchmark. It is now complete:
seven ordinary user prompts, 14 messages and 63 tool results, with no tool errors.

It created organization `clio-field-notes` and private dataset
`bbe6fbdb-5987-48f0-959f-25cf27646990`. Its 312-row subset, map and methods note
were each uploaded and verified. Metadata maintenance preserved all resource
identities. A separate public description was submitted to the configured test
catalog as `fb87f695-7638-4604-aaee-4ba584dffa98`; anonymous reads find its single
original-service link while the private collection remains hidden. A subsequent
authenticated read recovered the identical 47,794-byte CSV for a linked map/table.
Browser review selected 151 of its 312 rows and filtered the table to those rows.

Scientific inspection found 52 explicitly synthetic-labeled SY entries in the
source metadata. The remaining 260 entries are not thereby confirmed as operating
physical stations. Both the actual conversation and its public introduction retain
this distinction; the original files were not silently filtered or rewritten.

## Live scientific stream

The stream source is the official USGS magnitude-1.0+ rolling-day GeoJSON feed,
registered as a real Kafka resource in the Endpoint's local NDP catalog:

- Dataset `faa6e233-b5be-4581-9a0f-472408a70500`.
- Kafka resource `6304369f-c74c-48d4-a8bc-8cd7e5753bc5`.
- One-minute producer polls preserve original GeoJSON features and acquisition,
  source/snapshot and feature hashes. Initial-snapshot messages are distinct from
  later changed/new event versions. These are derived catalog estimates, not raw
  waveforms, GNSS samples, synthetic CSVs or an independently measured sensor feed.
- The producer was configured as deployment preparation. CLIO itself discovers,
  reads and analyzes the records through authenticated Endpoint operations.

Fresh visible Sol conversation `sess_b32b417f7f60`, workspace
`ws_fa10c022abcb`, begins with the ordinary SSH connection request. It has verified:

- First five records, then two five-record continuations across stop/resume:
  offsets 0-4, 5-9 and 10-14, without missing positions or repeated event versions.
- A larger sample of 165 consecutive deliveries (0-164), representing 163 event
  IDs: 161 earthquakes, one explosion and one quarry blast. The explorer uses
  latest captured earthquake versions; the message export preserves revisions
  and the excluded event types.
- Three fresh bounded ten-second watches at the current end, with honest empty
  results and preserved offset 165. An unchanged change-only feed cannot by
  itself establish producer health.
- A separate earliest-position reader, full retained-range read and empty
  continuation. About a day of event origins is represented, but only about
  half an hour of delivered retrieval history existed at that check. A seven-day
  retention limit does not create a seven-day archive.

The native stream UI separately opened an actual reader, captured five records
twice using automatic continuation, then stopped the reader with its checkpoint
preserved. Screenshot: `native-stream-reader.png`. Operator tests additionally
verified exact provider-feature hashes, retry identity, API restart recovery and
authenticated broker delivery; these are kept separate from model acceptance.

After the desktop restart, the same visible stream conversation completed its
previously blocked deposit. Dataset `0e013acf-9175-4968-b06f-31234d20b0c3` contains
the observations (66,566 bytes), original deliveries (68,225 bytes), provenance
archive (66,741 bytes) and current deposit methods (2,938 bytes). Exact object
readback matches all four hashes. The original archive is retained byte-for-byte;
its historical blocked-deposit note is explicitly superseded by the new deposit
methods and actual upload receipts. The original identity-service failure remains
in this diagnostic conversation; it is not a failure-free promotional transcript.

The next ordinary follow-up explicitly checked anonymous access and resumed the
original reader at position 165. It obtained 127 more messages at offsets 165-291
without gaps or exact event-version redelivery: 61 newly encountered catalog IDs,
four preferred-ID changes linked by provider aliases, and 62 changed versions.
One event version was catalog-updated at 12:09:16 UTC, retrieved at 12:09:32 and
captured at 12:09:35. This is direct evidence of newly delivered provider updates,
not merely replay of the initial snapshot. The reader stopped at position 292.
The four anonymous object reads correctly returned HTTP 401 and remain visible as
expected access-denial tool results; no authentication bypass was used.

Independent operator receipt `independent-acceptance-readback.json` verifies the
seven stored objects across both conversations, anonymous HTTP 401 for every
object, private resource HTTP 404, and public resource HTTP 200. It also recomputes
the 312-row subset from the 2,143 source epochs and confirms the original stream
sample's 165 consecutive offsets and 161 latest earthquake rows.

Current identity probes (`probe_identity_consistency.json`) use the same saved
account through direct Windows, direct Ares-container and Endpoint paths: all 20
sequential/concurrent requests returned HTTP 200. The Endpoint reports this exact
validated account as its local administrator. Earlier HTTP 502 responses represent
observed upstream `/api/information` HTTP 500 responses, not a demonstrated need
for additional roles or client claims. The underlying cause of those intermittent
identity-service failures is not established; recovery is not a claim of a
permanent upstream repair. Today's producer heartbeat also confirms successful
polling of the official provider, independently of change-only stream delivery.

## Rendered review and current limits

Native exports use `SessionHistoryRepository.prepareSessionArchive`, actual API
snapshot capture and the built offline renderer. No conversation messages or
failed calls are rewritten for showcase selection. The diagnostic export contains
no snapshot omissions; its saved points and chart render in a local HTML preview.
The completed Endpoint native HTML is `Endpoint-workflow-session.html`, 33,013,584
bytes, SHA-256 `682061c54d38863eba3deae981b12b142f204ca12f24880e0de0cacba50cb144`.
It has no snapshot failures or omitted captures. Its original PNG, saved 312-row
map and metadata table render through the offline viewer.

Luna's renderer qualification uses the 161 already-acquired observation rows,
with a short ordinary map/timing prompt. It is not a separate acquisition test.
One stale-view capture was correctly refused and retried; retain that diagnostic.
Rendered offline review confirms all 161 event rows, saved point geometry and the
coarse Natural Earth land outline. Box selection selected 54 rows and highlighted
those same rows in the time chart (`exported-stream-map-selection.png`).

Public guide screenshots come from actual light-theme native UI and the saved
scientific viewer. Their untouched captures and rejected framing are archived in
`D:/Libraries/Videos/clio_recordings/2026-10-20-ndp-endpoint/`. Native UI capture
again read five records twice after restart, preserving automatic continuation.

These runs do not qualify every advertised method, external Endpoint, central
AAI administrative operation, source-producer lifecycle or the separate Utah GNSS
deployment. There is no evidence that changing the user's token claims is needed
for the Endpoint/storage/consumer workflows demonstrated here.
