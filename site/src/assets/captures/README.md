# Product capture sources

The OPAL captures are untouched 1440 × 1000 PNG screenshots from an isolated
CLIO browser review on 2026-10-06, in light mode. They show the working UI at
`gact-tui` commit `f9f2108942d7430877b0e1d7bf850f341e4e472f` with the directory
browsing backend at `clio-agent` commit `a25d9703e1fcc3c988603af51d1b26e5d993d9c5`.
They are development review captures, not evidence that a release was published.

| Capture | What it demonstrates |
| --- | --- |
| `opal-figure-viewer.png` | The complete original six-panel plant-area figure in CLIO’s image viewer. Recorded category labels and IQR bands remain visible; this does not establish nickel doses or a treatment effect. |
| `opal-report-review.png` | Review and publication of an existing three-page Word report, alongside an existing eleven-slide PowerPoint deck. This session did not author the initial documents. |
| `opal-review-evidence.png` | The same review session’s recorded tool calls, sources and artifacts. |

`public/media/source-accounts.png` was captured from the same isolated CLIO. All
accounts are signed out. It demonstrates global account settings, separate
from workspace datasets, without exposing private credentials.

The untouched originals, website before/after captures and review notes are
retained in the local dated `2026-10-06-site-ergonomics` capture archive with
SHA-256 checksums. Astro generates responsive WebP derivatives at build time;
the originals above remain the sources. Click a homepage capture to inspect its
full-resolution derivative.

The other captures are earlier EarthScope and expert-agent examples, retained
in the expandable workflow section. They are separate sessions from the OPAL
review; their captions describe their own recorded behavior.
