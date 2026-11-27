---
title: "Write a paper"
description: "Ask for a LaTeX paper, read its source beside the built PDF, comment on either and follow its versions."
source: https://clio.iowarp.ai/docs/papers/
---

# Write a paper

Ask for a LaTeX paper, read its source beside the built PDF, comment on either and follow its versions.

CLIO writes LaTeX papers as projects: a `main.tex` that brings in one file per section,
a bibliography and figures. It builds the project to PDF and gives you the result as one
**paper**, shown by its title, that opens with its source and its PDF side by side.

> **Preview:** Papers, the LaTeX workspace and paper templates (since v0.9.6.1). Availability depends on your build.

## Ask for a paper

1. Say what the paper is about, where it is going and how you want it laid out.

   ```text
   Write a short paper on how burst buffers change
   checkpoint performance on shared HPC storage, for an
   IEEE conference. Put it in a new folder paper-ee: a
   main.tex that brings together five sections, each
   section in its own .tex file. Then build it so I can
   read it.
   ```

2. CLIO starts from a template for the venue, writes the files, builds the paper and
   fixes the problems the build reports.

3. The answer ends with the paper's card. Choose it to open the paper.

![The end of CLIO's answer, with a card titled Burst Buffers and Checkpoint Performance on Shared HPC Storage](plugin-asset://guide/captures/paper-card-in-reply.webp)
*The paper's card shows its title, its type and venue (Paper, IEEE conference), its pages and its version. The section files are inside the paper, not listed beside it.*

The templates cover IEEE conference and journal papers, ACM conference papers (with
double-blind review) and journal articles, Springer LNCS and Elsevier articles. When the
paper is for one of these venues, CLIO also checks it against the venue's usual rules:
the page limit, double-blind review, required packages and the paper size. Give CLIO the
call for papers to use its exact limits.

## Read the source beside the PDF

The paper opens with its outline, its source and its PDF. The source pane shows one
project file at a time; choose another from the file menu above it.

![The paper open full width: the outline on the left, main.tex in the middle and the built PDF on the right](plugin-asset://guide/captures/paper-opened.webp)
*The outline, the source of main.tex and the built PDF.*

- **Choose a section in the outline** to open its file and outline its place on the PDF.
- **Click a source line** to outline where it lands on the PDF.
- **Double-click the PDF** to open the source file and line behind that point.

![Background chosen in the outline: its file open in the source pane and the section outlined on the PDF](plugin-asset://guide/captures/paper-outline-to-pdf.webp)
*Background chosen in the outline: sections/background.tex opens, and the section is outlined on the page.*

![A source line selected and the sentence it produces outlined on the PDF](plugin-asset://guide/captures/latex-workspace-source-to-pdf.webp)
*A clicked source line, outlined on the PDF.*

To give one side the whole width, choose **Source** or **PDF** above the panes, or use
the arrows on the strip between them. **Source and PDF** brings both back. On a narrow
screen, the paper shows **Source**, **PDF** and **Outline** as tabs.

![The paper with only its PDF showing, and the outline still open on the left](plugin-asset://guide/captures/latex-paper-pdf-only.webp)
*PDF only. The arrow at the left edge brings the source back.*

## Check the build

The summary above the panes says how the build went, in words: its pages, problems and
warnings. Choose it to list every problem; choose a problem to open its file at its line.
Problems are also marked beside their lines in the source.

![The build summary open, listing font substitution warnings](plugin-asset://guide/captures/latex-workspace-problems.webp)
*The build's problems, listed by kind with their messages.*

When the paper is written for one of the venues above, the list also has that venue's
checks: the page limit, the paper size and, for double-blind review, author names outside
the title block and acknowledgments. A check that names a place opens it.

![The build summary of an ACM paper: page limit and paper size passed, double-blind author names failed at sections/body.tex line 2](plugin-asset://guide/captures/paper-venue-checks.webp)
*An ACM double-blind paper whose body still names its author, at sections/body.tex, line 2.*

> **Caution**
>
> The first build that needs a TeX package downloads it, so it needs network access.
> Packages that run other programs, such as `minted`, are not supported; use `listings`
> for code. Bibliographies build with BibTeX, not biber.

## Comment on the paper

Choose **Comment**, then select text in the source or on the PDF, or drag a region over
a figure. CLIO receives each comment with the source file, the lines and the section it
falls in, so the change is made in the source.

![A phrase selected on the PDF with its comment box open, titled with the paper's name](plugin-asset://guide/captures/paper-comment-on-pdf.webp)
*A comment on the PDF. CLIO receives it as sections/background.tex, line 4, in the Background section.*

Each mark stays drawn where you made it, on the source or on the PDF, with its label, and
follows the text as you scroll. Choose the label to change the comment.

![S1 drawn on a phrase in sections/background.tex and S2 on a phrase of the PDF, with Send 2 comments at the lower right](plugin-asset://guide/captures/paper-marks-on-both-panes.webp)
*Two marks before sending: S1 on the source, S2 on the PDF.*

See [Comment on results](docs/comments.md) for the comment controls.

## Follow its versions

Each build after a change is the paper's next version, and the answer shows the new
card. Your comments follow the paper: open **Comments** from the more menu (the three
dots) to see each one, CLIO's answer and whether the part you marked **Moved to
version 2** or was changed.

![Two comment threads on the paper: one moved to version 2, one outdated because version 2 changed it](plugin-asset://guide/captures/paper-threads-on-v2.webp)
*After CLIO edited the section, one comment's text moved to version 2 and the other was rewritten. CLIO's reply is kept on each comment.*

**Lineage**, beside the paper's name, shows which version of each source file the
paper was built from.

![The paper's lineage: each section file written, then used by the build that made the paper](plugin-asset://guide/captures/paper-lineage.webp)
*Each source file, the build that used them and the paper it made.*

![The paper at phone width, with Source, PDF and Outline tabs](plugin-asset://guide/captures/latex-workspace-phone.webp)
*At phone width the paper shows one pane at a time.*

## Write with a method

The `latex` plugin knows how to build a paper. How to write one is a separate plugin:
WTF-P, a method for writing a scientific paper from evidence, by Anthony Kougkas. Add
**wtfp** to the conversation's agent (**Customize**, then add the plugin), and the agent
keeps the paper's plan and progress in validated records under `.planning/` while the
manuscript stays the paper's LaTeX under `paper/`, one file per section.

Ask in your own words. Asked for "a 6-page IEEE Cluster workshop paper … set it up,
outline it, then plan, draft and review the introduction, and build it with the venue
checks", the agent started the paper from the IEEE conference template, wrote the
outline and section records, had the plan checked by an independent checker, drafted
the introduction, counted its words, built it with the venue checks and had a reviewer
read it.

![The agent's answer: what exists, the defaults it chose, and the review findings](plugin-asset://guide/captures/wtfp-new-paper.webp)
*What the agent made, the defaults it chose and what the reviewer found. The build passes the venue checks.*

Its actions are commands: type `/wtfp:` to see them, from `/wtfp:new-paper` and
`/wtfp:plan-section` to `/wtfp:check-refs`. During `/wtfp:write-section`,
`/wtfp:polish-prose`, `/wtfp:execute-outline` and `/wtfp:quick`, files can be written only
under `paper/` and `.planning/`.

![The / menu listing WTF-P's commands](plugin-asset://guide/captures/wtfp-commands.webp)

Its roles are subagents you can name with `@`: `@wtfp-section-reviewer` to review a
section, `@wtfp-citation-expert` to check the bibliography against the claims. The
checker, reviewer and verifier roles only read.

![The @ menu listing WTF-P's eleven roles](plugin-asset://guide/captures/wtfp-roles.webp)

Two commands run a role after another as a workflow: `/wtfp:plan-and-check <section>`
(a planner, then an independent plan checker) and `/wtfp:draft-and-review <section>` (the
section writer, then a reviewer). The plan is approved only when you say so.

![A workflow run with a plan step and a check step, both done](plugin-asset://guide/captures/wtfp-plan-and-check.webp)
*/wtfp:plan-and-check background: the planner wrote the plan and the checker reviewed it.*

A comment on the built PDF works as for any paper: the agent edits the section's file,
counts the section again and rebuilds.

![A comment on the paper and the agent's edit to introduction.tex](plugin-asset://guide/captures/wtfp-comment-applied.webp)
*The comment, and the one-sentence edit to the introduction it asked for.*
