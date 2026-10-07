# Default-agent document workflows

The code-shipped `main` agent declares PDF, Word, PowerPoint and spreadsheet skills.
These are independently authored Clio procedures. Upstream Anthropic and GPT skills
were references; their prompts, scripts and proprietary artifact libraries are not
vendored here.

The default `clio.chat` prompt explains managed Python/uv and Node/pnpm execution.
It directs document creation/editing tasks to load the relevant document skill.
`prepare_execution_runtime` resolves the tools; `prepare_document_runtime` remains
a compatible alias. Preparation binds verified tools to subsequent workspace
shell calls: `python` routes through uv with the prepared interpreter,
and pnpm uses managed Node. POSIX login shells restore these tool paths after
reading host profiles. Returned script argument lists select uv and pnpm
explicitly. Repository dependency environments stay separate.

Preparation provisions an isolated Python environment from Clio's installed
interpreter, including desktop's bundled Python 3.13, using
the shipped `runtime/document_stack/pyproject.toml` and `uv.lock`. Node.js is provided
by the locked `nodejs-wheel-binaries` package. Clio installs a pinned pnpm privately
and prepares JavaScript dependencies from `package.json` and `pnpm-lock.yaml` in a
workspace-owned directory. This works with a desktop bundle or an ordinary Python
installation; uv is a base dependency. Shared Clio dependencies are unchanged.

The tool probes Python imports and versions against the lock, tests CommonJS and
ES module imports, and returns interpreter/executable argument lists, output and
script workspaces, skill paths, installed fonts and native-tool readiness. Existing
Python caches are probed before reuse and repaired with a locked reinstall on a
failed probe. JavaScript import failures get one locked repair attempt. A failed
JavaScript setup is reported independently from a working Python runtime.
The cache identity includes the Python interpreter and host platform, so runtime
upgrades create a compatible environment instead of reusing an older virtualenv.
The private pnpm command is checked against its pinned metadata and executed
version; damaged or mismatched commands receive one bounded repair attempt.

`clio_agent.runtime.document_install` installs the locked packages and renderer.
The Windows native installer's existing runtime preparation step and both shell
installers invoke it during installation. DMG/AppImage installations finish this
managed setup before starting the agent, because those formats have no post-install
hook. Python packages, Node, pnpm and JavaScript packages download into managed
storage; they do not enlarge the installer payload. New workspaces reuse the
package cache. Later preparation repairs missing or damaged packages.

`prepare_document` provides three operations on active-workspace files:

| Action | Result | Verification boundary |
| --- | --- | --- |
| `inspect` | Bounded format-native JSON for PDF/DOCX/PPTX/XLSX/XLSM/CSV/TSV/text | Content extraction, not layout or recalculation |
| `render` | Separate PDF rendition and selected page PNGs | Rendering, not model visual inspection |
| `recalculate` | Separate XLSX copy with formula-cache/error checks | Evaluated formulas, not semantic correctness |

Sources are preserved. Each run gets a new directory and durable manifest with
source/output SHA-256 hashes and explicit failure information. Long PDFs and decks
require a range of at most 40 pages. Workbooks are bounded to 10,000 inspected
cells and may be selected by sheet/range. Render preflight limits each page to
20 million pixels and the selection to 80 million pixels; PNG output is limited
to 128 MiB. Existing file policy and the shell confinement boundary apply.
Agent-facing source documents, generated files, PDFs and page images remain inside
the active workspace, under the requested destination or
`.tmp/clio-documents/output/` for preparation and
`artifacts/document-previews/<source-artifact-id>/` for published PDF previews.
These directories are visible to the existing `@` picker, which excludes CLIO
service storage. Managed JavaScript script workspaces also live under `.tmp`.
Native/embedded editor document copies live under
`artifacts/document-working-copies/<working-copy-id>/`; their lease/review
manifests stay in canonical state. Existing manifests remain readable.
Backend retention under canonical state is
internal, not an agent file reference. Converter staging uses an already allowed
workspace-local `.tmp/clio-documents/` directory. Each conversion owns and cleans
up its private staging directory. Prepared Python and Node shells receive
`TEMP`, `TMP` and `TMPDIR` pointing at this workspace's scratch directory, also
returned as `scratch_directory`; no additional sandbox grants are introduced. Immutable PDF
custody may be retained in CAS while an addressable workspace copy is restored
without reconversion when necessary.

The native `view_pdf` and `view_image` capability gates remain unchanged. Their
snapshots use workspace-bound references into canonical managed state, so viewing
works when state is outside the authored workspace. Legacy workspace-relative
snapshots remain readable; references cannot escape their bound storage.
A model must actually
receive the relevant PDF or page pixels before claiming visual review. Text-only
models retain extraction and authoring; their review limitation is explicit.
The existing Docling PDF helper remains available for richer optional structured
conversion and existing blueprints; the prepared local fallback needs no Docling
model download.

Final Office deliverables use `create_artifact(path=..., kind="report")`.
Word, PowerPoint, Excel and OpenDocument artifacts receive a PDF preview by default.
`pdf_preview=false` omits it; `pdf_preview=true` also requests one for another
supported document format.
For a batch, an item's boolean `pdf_preview` overrides the call-wide option.
The editable source and its PDF are both immutable artifacts. Preview failures
appear in `pdf_previews` while retaining the accepted source. The UI manifest
advertises `pdf_rendition_artifact_id`, and the document viewer loads that saved
preview on opening the source. Existing UI requests through `/renditions` reuse
the same retained PDF. Every preview binds to a source artifact ID and checksum;
a changed source revision needs a new rendition. Converters receive a verified
snapshot from CAS rather than the current mutable workspace file. Native editing
and downloads continue to use the editable source.

The editable file is the deliverable. A PDF made only for display or review stays
an internal derivative: its producer carries `designation=document-rendition`
and `source_artifact_id`, so the UI omits it from output cards and deliverable
counts while retaining it for preview, downloads and lineage. Publish a separate
PDF only when the user requested that output. An independently registered PDF
without this binding remains a separate artifact; filenames do not establish a
preview relationship.

Each new conversion records source version -> Render PDF preview -> PDF version.
An enclosing `create_artifact` registration cannot claim that conversion's output
as its own. Registration output paths are not consumed inputs: only explicit
`used` references describe its sources. Registry reconstruction also drops older
generic output-path guesses with a `registration_output_refs_not_inputs` note,
retaining explicit source references and the original event log. Preview copying
and original-byte downloads support extended Windows paths under deep workspaces.

Office rendering and recalculation use **LibreOffice on the execution host**.
Clio discovers its standard Windows/macOS/PATH location or the absolute executable
specified by `CLIO_DOCUMENT_SOFFICE`. When no renderer is installed, setup downloads
the pinned official LibreOffice distribution, verifies its published SHA-256, and
extracts a private copy without installing it system-wide. Windows creates an
administrative image, macOS copies the app from a read-only disk image, and Linux
extracts only the Debian packages' application tree. License/notices remain with
the renderer. Linux hosts need the renderer's OS shared libraries. Conversion can
also provision the renderer for installations predating this setup step.
Each conversion has a fresh profile, disables document macros,
checks for a nonempty output, and bounds/cleans up its subprocess tree. XLSX
external links and macro-enabled recalculation are refused to avoid silently
destroying unsupported workbook features. Legacy and OpenDocument formats can be
rendered through LibreOffice; native extraction should follow explicit conversion
to a supported OOXML copy.
Private profiles prefer workspace-local scratch storage. On Windows, long
workspace paths use a private disposable profile in the already allowed short
OS temporary directory to avoid LibreOffice's native profile-path limits;
documents and rendered files still stay inside the workspace. Cleanup handles
long Windows filenames and briefly retries transient file handles.

Developer qualification uses the packaged `document_stack/smoke.py`: it creates
synthetic files, edits content, extracts all four formats, renders their pages and
recalculates a formula with an independent expected-value check. It includes an
image-only PDF page and embedded Word/PowerPoint images. The dedicated GitHub
workflow runs the actual installation routine and smoke on Linux, Windows and macOS;
it uploads source files, manifests and previews. `--pdf-text-only` is an explicit
subset for hosts without LibreOffice and is not full Office qualification.

## Local qualification (2026-10-04 and 2026-10-05)

- The focused document/default-agent/PDF suite passed 129 tests. Artifact identity
  and publication regression suites also passed; no tests were skipped.
- The actual managed Windows runtime verified its locked Python package versions,
  Node, CommonJS/ES imports and JavaScript package versions, including warm reuse.
  Private LibreOffice provisioning now succeeds on this Windows host, including
  official download checksum verification and administrative-image extraction.
- Windows passed the full synthetic create/edit/extract/render/recalculate smoke
  for all four formats. Its production shell invocation selected uv's prepared
  Python, private pnpm and managed Node; importing JavaScript packages through
  pnpm also passed.
- A Linux container without a system LibreOffice installation automatically
  downloaded and extracted the private renderer and passed the full synthetic
  create/edit/extract/render/recalculate smoke. Generated PDF pages, including
  the image-only PDF page and Word/PowerPoint images, were visually inspected.
- The native desktop library compiled and its installation-command test passed.
  The shared installation step ran against the built wheel in a Linux container.
  Its production login shell selected the prepared uv Python, pnpm and Node after
  loading host profiles; managed JavaScript imports through pnpm passed.
- Follow-up regression groups passed 40 conversion/runtime tests, 52 shell/artifact
  tests and 39 image/PDF snapshot tests; these groups overlap the earlier suite.
- Workspace-bound scratch and publication checks passed 51 focused tests, followed
  by 27 artifact/editing regression tests and picker-visible placement checks.
  Editor copies remained addressable while their internal manifests reloaded the
  saved lease. Real Windows API conversions for DOCX,
  PPTX and XLSX retained source bytes, restored deleted previews from immutable
  custody and cleaned their private staging directories. Python and Node shells
  resolved temporary storage inside the active workspace on Windows and Linux.
  The rebuilt wheel's Linux installation and full four-format rendering smoke
  also passed with JavaScript projects and intermediate files under `.tmp`.
- UI checks passed 7 document-workspace tests and 2 repository tests, type checks,
  lint and a production build. Three Chromium tests opened DOCX/PPTX/XLSX artifacts
  with real generated PDF renditions through the production pdf.js viewer. The
  transport was a fixture; screenshots were reviewed and no reconversion occurred.
- Ruff, mypy, source-size/instrumentation checks and wheel packaging passed.
  The wheel contains the helper, both lockfiles/manifests and packaged skills.
  No Node or LibreOffice distribution is embedded in the wheel.
- A real default-agent conversation using Codex/Luna loaded the Word skill, used
  the prepared uv interpreter, edited an existing DOCX while preserving its image,
  rendered and actually viewed its page, and published the editable artifact with
  a PDF bound to its source ID and checksum. The UI manifest selected that PDF.
  This conversation was rerun against the final workspace-local `.tmp` layout.
  Every recorded tool call succeeded; independent checks confirmed the requested
  uv/pnpm edits, unchanged embedded image, preserved heading, one-page render,
  viewed workspace page pixels and `@`-picker-visible source/PDF paths.

The Linux and Windows runtime paths and a live default-agent turn passed locally.
Renderer provisioning and installation-time packages are implemented. The new
GitHub document workflow also passed on Linux, Windows and macOS ARM64 on 2026-10-05,
including actual installer provisioning and full document conversion. These changes
have not been released.

## Publication qualification on develop (2026-10-05)

The publication branches start directly from `develop`, without the earlier
beta integration commits. The agent keeps develop's existing schema 0.5.3 and
adds only the uv/truststore roots to the shared dependency lock.

The final focused suite passed 139 tests with no skips. The Windows installation
routine and full four-format smoke passed on this base, including actual uv,
pnpm and Node execution with workspace-local temporary storage. The desktop
installer-command test passed after rebuilding incomplete local Rust cache entries.
Batch preview options now override the call-wide default per item, with regression
checks for a rejected item between accepted sources.
A fresh default-agent Codex/Luna conversation on this base passed after strengthening
publication guidance. All tool calls succeeded; independent checks verified the
requested edits, preserved heading/image, actual viewed workspace page, and the PDF
bound to the source artifact and visible through the file picker.

The broader local Windows Python run is not qualified: it crashed with stack
overflow and thread-exhaustion errors after 1,963 passing tests, with failures,
errors and skips. This remains a failed local gate; focused passing checks do not
replace it. An initial frontend run exhausted a worker's memory; rerunning with
two workers passed all 2,469 web tests. All 251 core tests and 59 desktop tests
also passed, with no skips. Lint, type checks, production builds and the three
real-PDF Chromium artifact-opening cases passed on this develop base.
The PRs were opened as drafts during broader qualification. Current run conclusions
are recorded in the PR checks and descriptions. The document installation matrix
passed independently; it does not replace the broader suite. CI's new-helper
exception-handler ratchet failure was corrected with typed operation errors, and
all 15 helper tests passed again.
The full source mypy check passed for all 911 files on Windows and with Linux
platform selection. CI's Windows-only subprocess-constant typing failures were
corrected without ignoring the type errors; both CI interpreter lint jobs passed.

## Live UI and documentation qualification (2026-10-05)

An ordinary two-turn Codex/Luna UI conversation revised an existing Word document,
then a two-slide PowerPoint deck and a formula-bearing Excel workbook. It loaded
the built-in document skills, executed the managed runtime, viewed the rendered
pages and published real artifact cards. Independent checks downloaded every
editable artifact and saved PDF, verified their hashes and source-ID/hash bindings,
checked the Word image and date, both slides, and the retained workbook formulas
and cached totals (8 hours and 200). All recorded tool calls succeeded. A fresh
spreadsheet conversation also verified that the recalculated deliverable is copied
outside `.tmp` before publication, without clearing its formula caches.

Live review exposed and fixed document media-type loss, misleading recovery text,
blocked local-path Markdown links, and unreadably small spreadsheet previews.
Word and slides open with full-page fitting; spreadsheet previews fit the available
width. The final preview checks passed 14 unit tests and three Chromium cases;
the artifact projection checks passed 43 Python tests and the skill/size checks
passed 33, with no skips. The real long Windows state-path reload and all nine
default-agent transcript tests also passed. The projection URI helper now lives
with the wire helpers, keeping the route's size guard below its lowered baseline.

The public site's Working with files page contains real continuous recordings
for PDF reading, a Word edit, and a slides/workbook follow-up, plus screenshots
and a slide-navigation GIF. The examples use synthetic planning data. Visible
labels disclose accelerated agent waits; normal typing, artifact opening and
preview navigation remain recorded interactions. Rejected takes are retained in
the dated local capture archive rather than delivered. These examples describe
the document workflows included in CLIO 0.9.5-beta.3.
