# Plan: comments and visual feedback on `claude/busy-turing-r40z0q`

## Context

The plan `docs/design/sdk/comments-and-visual-feedback-plan.md`, as amended by its addendum A, gives every CLIO surface one way to point at part of something and say what should change. That covers documents, figures, HTML, in-app browser pages, A2UI surfaces and agent answers.

The person enters a select mode and makes numbered marks (`S1`, `S2`, ...), each with its own comment, and sends them together. The agent receives readable entries, a labelled overview, a crop per mark and what lies under each mark, never raw JSON. On artifacts, each mark becomes a thread that holds the agent's reply and links the version it made.

Phase 0 is done and committed locally (`1f5607ec`, ledger `docs/design/comments-ledger.md`). This plan covers Phases 1–6 in clio-agent, gact-tui and clio-agent-marketplace, on the PTC base. It runs straight through, as the person chose.

**Merge path.** The work happens locally on `claude/busy-turing-r40z0q`, which starts from the PTC branch `claude/tool-calling-long-horizon` and tracks it. When the work is done, it merges into the PTC branch, and the PTC branch merges into `develop` after verification. So the draft PRs target `claude/tool-calling-long-horizon`, and nothing is force-pushed. For the stack, re-create the local link from `external/gact-tui` to `/home/user/gact-tui`, and never commit it.

## Decisions taken in planning (add to the ledger as FB22–FB27)

- **FB22, workspace files take comments (the person's call; replaces Q1).** There is no per-call write detection. The comment panel also runs in the workspace file view (`WorkspaceFileView`). When the person comments on a workspace file that is not an artifact, the server registers it as an artifact version on demand, with the designation `comment-target` and no producing call. The entry then says "Producer unknown" (FB20). When the agent did register the figure (the usual case, per the person), "Produced by" comes from `transforms_generating` as planned. K3, K4 and K8 run as written: if the agent leaves a figure unregistered, the person comments through the file view and the entry states the producer is unknown.
- **FB23, attention set excluded (Q2).** The attention set keeps clio-schemas `ContentSelection`. Rule 1 and K20 apply to comment, review, annotation and capture types only.
- **FB24, models without image input (Q3).** The renderer applies the same test as `message_submission.py:308-312`: the selected model is image-capable (`modality_evidence.image_input_capability`), or it is the active model and the active LM takes vision. When that test fails, the entries go without image parts. The header says so and names the session file holding the overview and crops. This is never silent.
- **FB24a, expansion point.** One helper, `comment_attachments.expand_request(app, sid, req)`, rewrites a request's `comment_session` parts into image parts plus model text before `_validate_provider_and_payload`.
  - It is called by `accept_message_async` and by `routes/mailbox.py` promote (lines 143-170).
  - The rendered entries travel as `internal_model_text`, so the person's own message stays readable in the transcript.
  - Stored comment sessions go through the same helper.
- **FB25, drafts (Q6).** A draft is a review row with status `draft` in the existing `documents/reviews.jsonl`, so there is no new store.
- **FB26, replies (Q5).** One `publication_step` in `turn_finalize.publish_finalized_turn`, and a one-liner in `publish_failed_finalize`, calls `gact/documents/comment_replies.on_turn_finalized(app, sid, turn_id, assistant_msg)`. That keeps the file at about 794 of its 800 lines, adds no broad `except`, and uses `turn_id == user_msg.id`.
  - It finds the comment sessions the turn carried: the user message's `metadata.comment_session_id`, plus steers where `task_history.committed_notification_input(sid, message_id).turn_id == turn_id`.
  - It writes the assistant's final text onto every thread in those sessions.
  - It links the new version from the assistant message's `resource_link` parts. By then the turn buffer has already been drained (`artifacts/wire.py:216`).
  - It falls back to FB28 reconcile.
  - A failed turn stores the failure on the thread, not a reply.
- **FB27, re-anchoring is lazy.** It runs when threads are listed and the artifact head is newer than a thread's version. The resulting moved or outdated row is persisted then. No event subscriber is needed. The write is idempotent: same input, same row. Region threads stay `current` on their own version.
- **FB28, new versions without write detection.** An agent that reruns a script or edits a file through the shell leaves the artifact head unchanged. So at finalize, `comment_replies` calls `artifacts/versions.reconcile_designated_path` (`versions.py:577`) on each thread's path, and `comment-targets` and GET threads do the same when a file opens. That makes the version 2 that K3 and K13 need.
- **FB29, naming on demand.** `artifact_name_for_path` keys on the basename (`minting.py:141`). On-demand registration calls `registry.find_version_by_path` (`registry.py:338`) first. If another path already holds that basename, the new artifact is named by its workspace-relative path.
- **FB30, "Produced by" when the agent registered the file.** `transforms_generating` then returns the `create_artifact` call. The renderer follows that call's declared `used` edges and the transform's `instrument.script_artifact_id` (`transforms.py:333`) to name the script.

## Architecture

### Server (clio-agent)

New code goes in owner modules. `routes/documents.py` has 27 lines of headroom and `turn_finalize.py` has 8; `tool_observer.py` is baselined and is not touched.

- `gact/documents/models.py`
  - `AnchorProfile` gains `region`.
  - `DocumentAnchor` gains `rect` (normalized x, y, w, h), `natural_size`, `crop_asset`, `tag`, `text_excerpt`, `html_excerpt`, `viewport_width` and a typed `source`.
  - `ArtifactReview` becomes the thread. It gains `label`, `session_key`, `mode` and `send_to_agent`, status `draft`, `reply_text`, `reply_message_id`, `reply_version_artifact_id`, `anchor_state` (`current|moved|outdated`), and `anchored_artifact_id` and `anchor` for the current version.
  - The editor models go.
- `gact/documents/profiles.py`
  - New `image` profile: `.png .jpg .jpeg .webp .gif` take `region`; `.svg` takes `region` and `dom`.
  - HTML, PDF and every Office rendition gain `region`.
  - `embedded_editors` goes.
- `gact/documents/anchors.py`: the resolver registry by profile. `resolve(anchor, ctx) -> Resolved(under_lines, source_location, crop_png, page)` and `reanchor(anchor, old, new) -> (state, anchor)`. The built-ins:
  - **text** (Markdown and HTML): match by exact, prefix and suffix.
  - **pdf**: text under quads or a rectangle via `pypdf`. Pages are rasterized with `pdftoppm`, found by `runtime/document_stack/process.find_native`.
  - **image**: Pillow crops. SVG is rendered through the HTML renderer, and its element lines come from the SVG's DOM.
  - **html**: render plus element lookup.
  - **office rendition**: map back to a paragraph, cell range or slide shape with python-docx, openpyxl and python-pptx, plus embedded-picture naming (FB16; port `scripts/cloud/comments_probe/probe_embedded.py`).
  - LaTeX registers `source-map` later.
- `gact/documents/html_render.py`: a short-lived Chromium (FB15). It uses `browser/install.ensure_chromium()`, `new_context(offline=True)`, aborts every route, and renders at `viewport_width` and 2x. Port `probe_html_render.py`.
- `gact/documents/mark_images.py`: the labelled overview in `labelledPng`'s style (box plus label badge), one per page that holds a mark, and the 2x crops, bounded to 2048 px a side.
- Resolver details:
  - Markdown and HTML text anchors match the rendered text (`innerText`), not the source. The text resolver maps rendered offsets back to source lines for the Source line.
  - HTML regions list tag and accessible name from CDP `Accessibility.getPartialAXTree` in the server render.
  - The server render allows the preview CSP's library CDN origins (`html-interactive-policy.ts` `LIBRARY_ORIGINS`) and aborts everything else, so a scripted Plotly page does not crop blank.
  - Excel renditions map a region to a cell range through openpyxl's column and row extents over the rendition's page layout. OpenDocument renditions name the page only, and the Source line says the file has no position map.
- `gact/documents/review_render.py`: replaces `_review_prompt`. Per session it writes:
  - one header (name, artifact id, version, short SHA, "Produced by" or "Producer unknown", "Edit X, not the PDF/image")
  - "Image 1 is ..." when images go
  - one entry per mark, `S1 (region, attached as image 2)`, with Box, Element, Selected, Before, After, Under, Source and Comment lines.

  It cuts each mark's under text at `comments.under_mark_chars` (1,200) and writes the rest to a file under `runtime/session_scratch.session_scratch_dir(sid)/comments/`, which the entry names. "Produced by" comes from `registry.transforms_generating(artifact_id)`; a `run_code` producer is named by its cell (the code).

  Settings `comments.under_mark_chars` and `comments.html_excerpt_chars` follow `browser/settings.py`'s `conf.resolve` pattern. Run `scripts/gen_env_reference.py` and add a `comments` section to `scripts/config_key_notes.py`.
- `gact/documents/comment_sessions.py`: label allocation (the next `S<n>` from the highest label on that artifact version, across sessions and drafts), drafts, send, human notes, and on-demand workspace-file registration (`artifacts/storage.ingest_artifact_identity` plus `minting.mint_artifact`, producer `{designation: "comment-target"}`). Dispatch builds one message:
  - a `text` part with the rendered entries, metadata `comment_render: true`
  - image parts (inline `data`) in order: overview, then crops
  - one `artifact_review` part per thread, for the UI

  It sends that with `metadata.comment_session_id` through `message_submission.accept_message`. Native `@clio` comments are dispatched through the same path, as one mark each.

  The plan names a separate rendered text part, but the entries go as `internal_model_text` instead (FB24a). The transcript shows the person's comments and the `artifact_review` parts, and "What the agent received" opens the rendered text.

  Sending a draft whose artifact head has moved returns `stale_artifact_anchor` (as `routes/documents.py:349-375` does today). The UI then offers to re-mark on the new version or to send the comments against the old version as history.
- `gact/routes/comments.py`:
  - `GET /v1/workspaces/{wid}/comment-targets?path=` (look up a file's artifact, drafts and threads without registering it)
  - `GET /v1/artifacts/{id}/threads` (replaces `/reviews`; runs the lazy re-anchor)
  - `PUT /v1/artifacts/{id}/comment-drafts`
  - `POST /v1/sessions/{sid}/comment-sessions` (replaces `/artifact-reviews`)
  - `POST /v1/workspaces/{wid}/comment-targets {path}` (register or return the artifact for a file)
  - `GET /v1/artifacts/{id}/threads/{tid}/compare` (the before and after crops)
- `gact/documents/comment_attachments.py`: the `comment_session` part for surfaces without a version (browser page, agent answer, A2UI). At admission it expands the part into the rendered `text` part and image parts, from the same renderer.
  - A2UI: the client-made overview comes as an upload (`resource_ref`), and the under-mark data arrives in `anchor.source`.
  - Browser page: crops come from a fresh screenshot through `browser/service.service().run(record, "screenshot", ...)`.
  - Wiring: add `comment_session` to the allowed parts in `messaging._user_message_parts` and to the non-empty check in `message_submission._validate_provider_and_payload`.
- `gact/documents/comment_replies.py`: FB26.
- `gact/browser/picker.py` plus the picker JS. A page's own script can call the exposed binding, so reports are accepted only while a pick is active on that page and its input viewer. The element is re-resolved through CDP at the reported point before anything trusts it.
  - installed per profile with `context.add_init_script` and reported through `context.expose_binding` (managed backend only; FB17)
  - live-socket messages `pick.start`, `pick.stop`, `pick.hover` (rectangles broadcast to the input viewer) and `pick.selected`
  - text selection with prefix and suffix
  - target spec from CDP `Accessibility.getPartialAXTree`, falling back to the picker's selector, in `browser/targets.py`'s `{"steps":[...]}` format
  - hooked from `live.py` and `service._bind` with a line each

### UI (gact-tui)

`a2ui-region-capture.tsx` (789 lines) and `composer.tsx` (778) are near the 800-line lint limit, so this splits into new modules.

- `packages/core/src/v3`:
  - the extended `DocumentAnchor` and thread types in `document-domain.ts`
  - a new `comment-repository.ts` (threads, drafts, comment sessions, comment targets, compare)
  - the `comment_session` part in `composer-domain.ts` and `composer-schemas.ts`
  - editor types removed
- `web/src/components/clio/comments/` (generalized from `a2ui-region-capture.tsx`, which shrinks to the A2UI adapter):
  - `comment-session.tsx`: provider with marks, labels, mode, and draft load and save (artifacts) or discard (elsewhere)
  - `comment-panel.tsx`: the movable panel; mode dropdown applying to the next mark; list; per-mark comment; "Send to agent"; "Send N comments" on artifacts, "Add N marks to message" elsewhere; "Add to attention set" kept beside it
  - `comment-region-layer.tsx`: drag overlay, touch-capable, reporting in the viewer's own coordinates
  - `comment-adapter.ts`: the per-surface interface, giving its modes, anchor builders, optional client overview (A2UI only) and the send target (artifact or attachment)
- Adapters and viewers. The adapters attach to the shared viewers themselves (`ClioPdfPreview`, `FileTextView`/`FileReadView`, `ImageResourceView`, `HtmlPreview`), not to `document-workspace.tsx`. The session provider is mounted where those viewers are hosted: `ClioDocumentWorkspace`, `ArtifactView`'s image and HTML branches (`resource-viewers.tsx:414-430`) and `WorkspaceFileView`. Default mode per surface follows FB6.
  - `document-workspace.tsx`: Markdown text; PDF and rendition text and region.
  - HTML text, region and element: the picker joins `html-preview-bridge.ts`'s `bridgeScript`, using `clio.html-preview.pick.start|hover|selected|cancel` and `clio.html-preview.selection`, with handlers in `desktop-html-frame.tsx`. This works on web `srcdoc` and on the Tauri `/v1/html-renderer` path alike.
    - Everything the frame reports is length-bounded.
    - The overlay takes pointer events in region mode and passes them through to the frame for element and text modes.
  - `image-resource-view.tsx`: region on images. SVG element mode renders the SVG in the HTML frame so the picker can reach it.
  - `resource-viewers.tsx`: `WorkspaceFileView` (images, PDF, HTML, Markdown, SVG) takes the same panel, registering the file through `comment-targets` on the first draft or send (FB22).
  - `browser-surface.tsx`: comment control after the address bar; region on the live view (`browser-live-input.pagePoint`), element and text through the server picker; only the viewer that sends input.
  - A2UI: `surface-toolbar.tsx`'s Select starts the shared session. `a2ui-region-evidence.ts` builds typed `anchor.source` fields, not the JSON text.
  - Transcript: "Add to chat" (`use-add-to-chat-selection-action.ts`) adds a text mark to the shared session.
- Threads in the document: `comment-thread-layer.tsx` draws highlights, outlines and margin markers from the positions the server resolves. `comment-thread-list.tsx` is a side panel with status as icon plus text (`status.tsx` `ClioStatus`). `comment-compare.tsx` shows before and after for region threads: the crop beside the same rectangle on the newest version, and a toggle between the two whole images.
- Migrated onto the shared session:
  - the eleven `buildZoneReference` producers (a zone becomes an element mark on its surface, with the zone summary as its under-mark lines)
  - `composer-annotations.tsx`, now showing mark chips
  - `hooks/use-composer-draft.ts`, now holding marks
  - `use-reference-this-selection-action.ts` and `data-reference-this-button.tsx`
  - `lib/sent-data-references.ts`, which parses the old blockquotes and is replaced by reading `comment_session` parts
- Deleted: the Review selection dialog, the Reviews tab, `ReviewTimeline`, the reviews entry in `document-view-controls.tsx`, `lib/composer-annotations.ts`, `use-region-capture-annotation.ts`, `messageTextWithAnnotations`, the second `clio:add-region-capture` listener, and `onlyoffice-editor.tsx` with the editor branches.

### Marketplace

- `plugins/document-production/skills/review-document-artifact/SKILL.md` and `references/anchor-semantics.md` are rewritten for the readable entries. They map S labels, the Selected/Before/After/Under lines, the overview and crops, and "Produced by ... change the script".
- `plugins/visuals/skills/review-visual-presentation/SKILL.md` gains "Acting on a figure comment" after the repeat paragraph: look at the crop, change the producing script, rerun it, `view_image` the new output, then reply.
- Also update the pptx, xlsx and OpenDocument skills and `document-production`'s `mission.md` where they teach anchor profiles.
- Patch bumps in `plugin.json` plus a `CHANGELOG.md` `[Unreleased]` entry. Bump the clio-agent gitlink.

## Phases (each step is a small Conventional Commit)

1. **Remove the editors.** This follows ledger measurements 1–1b: delete the files and routes, remove `provider`, set the route guard to 427, drop the env vars and docs, and remove the gact-tui component, branches, types, fixtures and SPEC rows. Then check that `.docx`, `.pptx` and `.odt` show their rendition and offer only the desktop app (K1).
2. **Anchors, profiles and resolvers.** The models (the client SDK's `client/types.py` `DocumentAnchor` too), profiles, `anchors.py`, `html_render.py`, `mark_images.py`, and the PDF, image, SVG, HTML and rendition resolvers with re-anchoring. Unit tests per resolver use the probe fixtures.
3. **Threads and rendering.** `comment_sessions.py`, `review_render.py`, `routes/comments.py` (SPEC rows plus a capability flag in `x_clio_document_artifacts`; route guard 427 + 6 = 433), `comment_replies.py` and its finalize hook, `comment_attachments.py`, and native `@clio` moved onto sessions. `_review_prompt` goes now. The old `/reviews` and `/artifact-reviews` routes stay, backed by the new session code, because gact-tui, `client.py:555` and the e2e fixtures still call them.
4. **UI.** The core types and repository, the comments package, the adapters on every surface plus the workspace file view, the HTML picker, the thread layer, list and compare, the migrations and deletions, and the A2UI path moved onto the shared panel. The commit that switches the UI also deletes the old review routes, the client SDK methods, their SPEC rows and fixtures (route guard 431).
5. **Figures and pages.** Embedded-picture naming wired into the rendition resolver, the browser picker and page crops and target specs, the marketplace skills, and W13/W14 in `browser-ledger.md` pointing to K17/K18.
6. **Qualification.** K1–K24 on a freshly restarted stack, recorded in the ledger. Then push all three branches without force, pin the submodules and open draft PRs against `claude/tool-calling-long-horizon`.

## Reuse

- `browser/files.py:50-96`: the template for minting a file the server made.
- `artifacts/storage.ingest_artifact_identity` and `minting.mint_artifact`.
- `registry.transforms_generating`, `get_by_artifact_id` and `get(ws, name).versions`.
- `renditions.find_pdf_rendition`.
- `message_submission.accept_message`, with `server_metadata`.
- `runtime/session_scratch.session_scratch_dir`.
- `browser/service.service().owned/run`, `targets.locator_for` and `describe`.
- `view_image_tool`, which the agent uses on the rerun.
- In the UI: `labelledPng` (A2UI overview only), `html-preview-bridge` channel scoping, `browser-live-input.pagePoint`, `ClioStatus`, `InfoTip`, `icon-vocabulary`.
- Test helpers `_workspace_session` and `_pin` in `tests/test_gact/test_document_artifacts.py`.

## Verification

- **Per step:**
  - focused `pytest`, plus `uv run ruff check` and `ruff format --check` on the touched paths
  - the CI guards `scripts/check_file_size.py`, `scripts/check_noqa_swallows.py`, `scripts/check_no_class_in_function.py` and `tests/test_gact/test_decomposition_guardrails.py`
  - for gact-tui: `pnpm lint` (including its 800-line check), `pnpm typecheck`, `pnpm test`, `pnpm build`
- **Server tests:**
  - label continuation (K24), drafts (K23), the cut and session file (K22), and that the agent can read that file
  - no JSON in the rendered text, the image order, one overview per marked page
  - "Producer unknown", and the script named through `create_artifact` (FB30)
  - the image test matching `message_submission`'s, and the expand helper on both entry points
  - the reply stored on every thread after a fake turn finalizes, and on a steer absorbed by another turn
  - the re-anchor states and idempotence under concurrent GETs
  - the basename collision (FB29), and reconcile minting version 2 (FB28)
  - a crop of a CDN-scripted HTML page
  - the forged-pick refusal, tested inside `BrowserService` with `host_gate` attached
- **UI checks:** restart the stack, run `node scripts/cloud/ui_check.mjs --steps ...` at 1440×900 and 390×844, read every screenshot, and require `consoleErrors` and `failedRequests` to be empty. Add a `--touch` option (a context with `hasTouch`) so K16's touch drag is real.
- **Live scenarios K1–K24:** plain requests on the `claude_code` stack, each recorded with commands, session handles and the screenshots read. The tool-directed K4 is labelled as mechanism-only. Whether `claude_code` delivers image parts is confirmed in K3; if it does not, FB24's stated path is what K3 checks. The Tauri desktop and the Windows and macOS installs stay local phase.
- **Done:** K20 search clean; ledger rows filled; branches pushed; pins bumped; draft PRs carry the summary and the evidence.
