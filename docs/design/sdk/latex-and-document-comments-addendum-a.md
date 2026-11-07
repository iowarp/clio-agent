# LaTeX builds and document comments: addendum A

This addendum amends [latex-and-document-comments-plan.md](latex-and-document-comments-plan.md). The comment system and the removal of the embedded Office editors have moved to their own plan, [comments-and-visual-feedback-plan.md](comments-and-visual-feedback-plan.md), so they can serve images, HTML artifacts and in-app browser pages as well as documents, and so neither plan waits for the other. Where this addendum and the plan disagree, this addendum wins. Record each change as an `L` decision in `docs/design/latex-and-comments-ledger.md`, citing this addendum.

## What this plan keeps

Workstreams 2, 3 and 4 (bundled builds, paper templates, source and PDF mapping) and the build, outline, click-through, diagnostics and outline navigator parts of workstream 6 (the LaTeX workspace). Its tools (`latex_build`, `latex_outline`, `latex_locate`), its rules 1, 3 and 4, and its out-of-scope list stay as written.

## What moves to the comments plan

- Workstream 1, removing the ONLYOFFICE and Collabora editors, with Phase 1 and scenarios L16 and L17.
- Workstream 5, the comment system, with Phase 4, items 1 to 4 of Phase 5, rule 2 (one anchor model), and scenarios L8 to L14 and L18.
- The comments bullet of workstream 6.

## How the two meet

- **The resolver.** This plan registers a `source-map` resolver in the comments plan's resolver registry (`gact/documents/anchors.py`). It resolves a text or region anchor on a compiled PDF to the source file, lines and outline node through the SyncTeX index, re-anchors through the new index after a rebuild, and names the `\includegraphics` file for a region on a figure. If the comments plan has not landed when this plan reaches Phase 3, build the resolver against the registry's interface as that plan defines it and register it when the registry lands. If the registry exists, register it directly.
- **The workspace.** The LaTeX workspace uses the comments plan's comment layer on both panes once that plan has landed. A comment on the PDF shows at its lines in the source and the other way round.

## Scenarios after the move

L1 to L7 and L15 stay here. Two scenarios are added, run once both plans have landed:

| ID | Scenario | Passes when |
| --- | --- | --- |
| L19 | A text comment on a PDF sentence of a LaTeX paper, sent to the agent | The entry names the source file, lines and outline node, and the agent edits the source |
| L20 | A region comment around a figure in the PDF | The entry names the figure environment and its `\includegraphics` file with that file's producing script, and the agent edits the script or the environment as the comment asks |

## Base

The base moves forward to `develop` after the tool-calling branch merges, since `claude/sdk-plugins-flows` has long since merged. The rendition and document code this plan changes is unaffected by the move.
