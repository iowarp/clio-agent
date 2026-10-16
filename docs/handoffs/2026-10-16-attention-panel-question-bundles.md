# Attention panel and native question bundles

UI: iowarp/gact-tui#643. Backend: iowarp/clio-agent#1791.

The shared attention panel displays one permission/question at a time with
previous/next controls and a shrinking remaining count. Permission decisions use
one Allow button with an explicit server-offered scope selector, exact operation
details and a technical disclosure. Linked A2UI questions retain their Go to action.
Conversation-owned memory drafts preserve answers, permission scope, current
question and errors across pager navigation, collapse and responsive composer
remounts. No question draft is persisted to disk.

Native `ask_user(questions=[...])` publishes one durable question containing the
bundle. Each item defines its own kind, options and freeform setting; top-level
single-question defaults do not override it. All answers are validated at the
atomic question transition before persistence/delivery. Partial/invalid answers
return 422 without settling or resuming. Blocking answers resume the owner once;
async answers enter one ordinary queued message. Composer prose cannot bypass the
bundle form. Recovery, cancellation, ownership and MCP form semantics remain on
the existing question ledger and lifecycle.

Qualification evidence is retained in
`D:/CLIO-Develop/attention-panel-20261016/`:

- `backend-qualified.log`: 131 backend cases passed, including bundle validation,
  recovery, one queued delivery, duplicate refusal, ordinary questions and elicitation.
- `ui-qualification-tests.log`: 88 focused interaction, navigation, scope, draft and
  responsive remount tests. Root lint, type checks and production/offline builds
  are recorded separately.
- `browser-final-all.log` plus `browser-final-repaired.log`: all 30 production
  browser cases passed. The latter updates two expectations to verify drafts
  survive reopening/resizing and corrects a misplaced test setup line. Checks cover shared attention, linked
  visual questions, transcript navigation, keyboard/scrolling, accessibility,
  desktop/mobile layout and retained state across width changes.
- `linux-browser.log`: real Linux Chromium reviewed/refreshed the two affected
  visual baselines. Windows baselines were reviewed independently. Pixel budgets
  were not increased.
- `live-v5/receipt.json` and `live-v5/browser-submitted.json`: a real GPT-6 Luna
  agent using the canonical Base Agent asked the three-question bundle. The real
  backend refused a partial answer (422). The production browser answered all
  three, changed from desktop to phone width without losing drafts, and sent one
  complete submission. The agent resumed and said: "You chose the station table
  for New Zealand, with emphasis on uncertainty."

Live acceptance exposed and fixed two gaps beyond the initial focused tests:
model-supplied single-question defaults unnecessarily rejected bundles, and
responsive composer remounts discarded local drafts. The final live run includes
both fixes. Earlier failed harness attempts are retained as evidence, including
the fresh server without an activated Base Agent and an ambiguous unqualified
blueprint identity; the passing run explicitly installs/selects the canonical
workspace blueprint. MAG was not used for this live acceptance.

These changes are source/PR work. This handoff does not claim a release or an
installed Desktop update.

The backend pins UI commit `8b26a5698f55aa6bf26e995f686e61790761e328`.

The final bridge refactor keeps canonical answer validation in the bundle module;
`backend-cap-repair.log` records 72 passing bundle/elicitation cases after it, and
the unchanged file-size guard passes. `ui-id-regression.log` adds coverage for
question ids matching Object property names.
