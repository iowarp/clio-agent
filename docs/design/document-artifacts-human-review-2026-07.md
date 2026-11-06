# Document artifacts and human review

This campaign makes documents first-class views over CLIO's immutable artifact
registry. It is for one user working with their own files. The native file is
canonical and remains compatible with the application that created it; CLIO does
not replace Office, require an Office add-in, or convert an OOXML file into a
private format.

## Product workflow

1. An agent or tool produces and registers a Markdown, PDF, LaTeX, static HTML,
   OOXML, or OpenDocument artifact.
2. The artifact rail opens the exact immutable version in a format-aware viewer.
3. The user highlights text or a PDF region. The UI opens a floating comment box
   beside that selection.
4. Sending the comment creates a durable `artifact_review` part bound to the
   artifact ID, version, SHA-256, and structured anchor. It immediately continues
   the same agent session.
5. The agent edits the canonical source and mints a new immutable version.
   Historical selections never silently move to the new content.

The Comments view records CLIO and native-file comments. The History view selects
immutable versions. The Policy view explains canonical bytes, derived renditions,
and HTML execution policy.

## Supported profiles

| Files | Profile | Direct view | Editing path |
|---|---|---|---|
| `.md` | Markdown | rendered document | agent or native editor |
| `.pdf` | PDF | PDF.js canvas plus selectable text layer | source artifact, when available |
| `.tex` | LaTeX | source and derived PDF | agent or native editor |
| `.html`, `.htm` | static HTML | sanitized, scripts disabled | agent or native editor |
| `.docx`, `.xlsx`, `.pptx` | OOXML | optional derived PDF | native editor |
| `.odt`, `.ods`, `.odp` | OpenDocument | optional derived PDF | native editor |

Executable HTML is not a document profile. It moves to Live Web, retaining Live
Web's untrusted-code consent, network, and provenance boundary. A2UI and MCP Apps
also remain distinct protocol paths.

## Wire and storage

`GET /v1/capabilities` advertises `x_clio_document_artifacts`. The capability
lists profiles, anchors, immutable history, floating comments,
the `@clio` native-comment trigger, and the static-HTML policy.

Important routes:

- `GET /v1/artifacts/{id}/document` returns a format-aware manifest.
- `GET /v1/artifacts/{id}/document/content` serves verified bytes and applies a
  sandboxing CSP to static HTML.
- `POST /v1/sessions/{id}/artifact-reviews` validates the exact version and hash,
  persists the review idempotently, emits semantic events, and starts or steers a
  user turn with a typed `artifact_review` part.
- `POST /v1/artifacts/{id}/renditions` invokes a real local converter and
  registers the derived PDF as an artifact. Pandoc uses a local Typst engine
  when available, with `CLIO_DOCUMENT_TYPST_FONT` as an optional font override.
- `POST /v1/artifacts/{id}/working-copies` creates one confined mutable copy. A
  stable save hashes the bytes and either mints a new immutable version or reports
  an explicit head conflict.

Reviews are append-only JSONL projections under the workspace `.clio` area.
Working-copy metadata is durable for the Host process and the files remain under
the confined document working-copy root. A writable lease prevents two editors
from silently racing on one logical artifact.

## Native comments and package preservation

OOXML and OpenDocument are ZIP packages containing XML parts and related
resources. CLIO reads bounded, path-confined packages and imports native comments.
Only a comment beginning with `@clio` is an agent instruction; other comments are
preserved as human notes. The fingerprint and working-copy identity make an
agent-bound native comment exactly once.

Document-production skills edit supported semantic parts with `python-docx`,
`openpyxl`, `python-pptx`, or `odfpy`. A package inventory guard compares the
before and after archives. If an editor cannot preserve an unknown or unsupported
part, it must block rather than silently discard it.

## Opening in the desktop application

Native open is the only editing path in CLIO. CLIO creates a working copy and asks
the desktop shell to open its exact path in the registered system application.
Stable saves checkpoint automatically. The embedded ONLYOFFICE and Collabora
editors this campaign first shipped were removed by the comments and visual
feedback plan (`docs/design/sdk/comments-and-visual-feedback-plan.md`, ledger
decision FB4); Office files are shown through their PDF rendition. Missing
converter prerequisites are an explicit unavailable state; there is no fake
renderer.

## Security and limits

- Artifact content is hash-verified before serving or materializing.
- Reviews are rejected when the selected version/hash is stale.
- Working-copy paths are generated beneath a confined root.
- Archive entry count, entry size, total size, and traversal are bounded.
- Static HTML strips active elements client-side and is served with a
  `sandbox; default-src 'none'` CSP.
- PDF and Office previews are derived artifacts; they never replace native bytes.

Browser confinement and office-format libraries do not guarantee perfect
round-tripping of every third-party extension. Unsupported package parts are why
inventory comparison and fail-closed editing are required.

## Validation

The automated gate covers profile negotiation, static HTML policy, exact-version
reviews and idempotency, native comments, malformed archives, immutable saves,
stale conflicts, and coalesced autosave turns. The web
gate covers selection, the floating composer, typed transcript presentation,
comments, history, policy, production build, and Chromium recording.

The checked-in acceptance bundle at
`acceptance/document-artifacts/acceptance-report.json` records a real Codex
review continuation, its immutable input and output hashes, and the exact
environment limitations of the run.
