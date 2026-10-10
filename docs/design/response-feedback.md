# Response ratings

Completed assistant messages offer **Good response**, **Bad response**, and
**Remove rating** in their footer. The selected state changes only after the
server acknowledges the write. Offline transcript archives do not offer ratings.
Ratings neither start an agent turn nor send feedback into its live prompt.

`ARCMemory.response_feedback` owns a `ResponseFeedbackLedger` on ARC's existing
clio-core store. Its versioned `response_feedback` records contain the session,
workspace, message and originating turn ids, UTC recording time, answer text and
SHA-256, originating prompt text when available, and the recorded model selection
with its source. Missing historical model or prompt information stays unknown;
the current session model is never substituted for historical evidence.

Every change has a client UUID and the previous feedback UUID. Removing a rating
records `rating: null`; it does not erase the history. Repeating a request UUID
with the same rating is idempotent. A stale expected UUID or reuse of a UUID for
another rating returns `409 feedback_conflict`. Writes serialize within the
process ARC ledger. Store failures, unavailable ARC and unreadable evidence return
`503 feedback_unavailable`, so the UI cannot claim a browser-only success.

The record family is separate from searchable working-set segments and remains
available after ARC releases the session's hot context. This is evaluation data
collection; no training or optimizer automatically consumes the ratings yet.

## HTTP contract

- `GET /v1/sessions/{sid}/messages/{message_id}/feedback` returns
  `{"feedback": null}` before the first rating, otherwise the latest stored record.
- `PUT` to the same URL accepts
  `{"feedback_id": "<new UUID>", "expected_feedback_id": null, "rating": "good"}`.
  Use the last returned `feedback_id` as `expected_feedback_id` for subsequent
  changes. Allowed ratings are `"good"`, `"bad"`, and `null`.
- `GET /v1/sessions/{sid}/response-feedback` returns `{"items": [...]}` with the
  immutable history and snapshots for later evaluation/export. Consumers deriving
  a current rating use the last record per message and honor removal tombstones.

Unknown sessions/messages return `404`. Non-assistant messages and malformed
requests return `422`; unfinished responses return `409 response_not_settled`.
These endpoints use the same host/auth boundary as the message ledger.

## Verification

`tests/test_arc/test_response_feedback.py` includes the real clio-core backend,
fresh-ledger reads after release, idempotency and concurrent-change checks.
`tests/test_gact/test_response_feedback.py` covers API provenance, changes,
removals, scoping, validation and storage-failure acknowledgement.

The browser review uses the production transcript renderer, repository and GACT
routes with a synthetic conversation and isolated native clio-core storage:

1. Run `uv run --no-sync python -m tests.test_gact.response_rating_review_server`.
2. In `external/gact-tui/web`, run
   `pnpm exec vite --config tests/review/transcript-vite.config.ts`.
3. Set `CLIO_REVIEW_EVIDENCE` to a bounded output directory, then run
   `pnpm exec playwright test --config tests/review/rating-playwright.config.ts`.
4. POST to `http://127.0.0.1:18817/__test/shutdown` and stop Vite. The review
   service releases its private native daemon and removes its temporary state.

The review service never invokes an LM or attaches to the installed Desktop.
