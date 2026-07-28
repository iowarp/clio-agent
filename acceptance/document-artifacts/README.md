# Document artifact acceptance

This bundle records the real Codex continuation for a version-bound document
review. The source was pinned as immutable artifact version 1, the user comment
was sent as a typed `artifact_review`, and Codex edited the canonical Markdown.
The artifact observer then registered version 2 with a new hash and model
provenance.

Files:

- `acceptance-report.json` contains the exact workspace, session, review,
  artifact, version, and hash identities.
- `evidence/live-codex-transcript.json` is the persisted real session transcript,
  including the structured review, tool evidence, answer, and resource link.
- `evidence/live-codex-result.md` is the final canonical document bytes.
- `evidence/live-markdown-rendition.pdf` is the derived PDF created by the live
  Pandoc/Typst route, and `live-markdown-rendition-page-1.png` is its reviewed
  Poppler rendering.
- `workspace/evidence-brief.md` is the deterministic starting fixture used by
  automated and repeatable local runs.

The report keeps environment limitations explicit. In particular, it does not
claim live embedded-editor or in-app-browser acceptance when those runtimes were
unavailable on the recording host.
