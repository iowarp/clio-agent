---
title: "Comment on results"
description: "Mark the part of a document, figure, web page or answer you mean, and say what to change."
source: https://clio.iowarp.ai/docs/comments/
---

# Comment on results

Mark the part of a document, figure, web page or answer you mean, and say what to change.

A comment points at the exact part of a result you mean: a sentence in a report, a
chart in a PDF, a button on a web page or an area of a map. CLIO sends each comment
with what lies under it, so you do not have to describe where it is.

> **Preview:** Comments on documents, figures and web pages (since v0.9.6.1). Availability depends on your build.

## Comment on a file

1. Open the file from **Files** or from its card in the conversation, and choose
   **Comment** in the viewer's toolbar.

2. Choose how to mark it with **Select**, at the lower right of the viewer:
   - **Text**: select words in a document, a PDF or an answer.
   - **Region**: drag a box over a figure, a PDF page or a web page.
   - **Element**: on an SVG figure or a web page, point at a part and click it.

3. Write your comment in the box that opens beside the mark, and choose **Done**.
   Each mark gets a label, S1, S2 and so on, and stays drawn where you made it. The list
   beside **Select** shows them all.

4. Choose **Send** (for example **Send 2 comments**) to send every comment at once.

![A Markdown report with a selected sentence and the comment box open beside it, labelled S2](plugin-asset://guide/captures/comment-markdown-text-editor.webp)
*A text comment on a report: the quote, the comment and Send to agent. The controls at the lower right show two marks ready to send.*

You can mix modes on one file. Here two sentences and the chart on a PDF page are
marked in one session:

![A PDF page with a region drawn over its chart, labelled S2, and three comments listed at the lower right](plugin-asset://guide/captures/comment-pdf-three-marks.webp)
*Three comments on one PDF: two text marks and a region over the chart, sent together.*

## What CLIO receives

Each comment reaches CLIO as a short entry with its label: the words you selected and
the text around them, or, for a region, an image of the page with every mark drawn on
it and a close-up of the area. For a Word, PowerPoint or Excel file, the entry names the
paragraph, slide shape or cells. For a figure CLIO registered with the script that drew
it, it names that script, so the change goes into the script, not the image.

Comments on a saved result become threads on that version. CLIO's answer is kept on
each comment, and so is the version it made. When the file changes, each thread says
whether the part you marked is still there, **Moved to version 2** or **Outdated**.
Open the threads from **Comments** in the viewer's more menu (the three dots).

![The conversation after sending: the comment as the next message, with the report open beside it](plugin-asset://guide/captures/comment-text-on-markdown-sent.webp)
*The sent comment appears in the conversation, and CLIO starts on it.*

To keep a comment for yourself, turn off **Send to agent** in its box. It is saved as a
note and starts nothing.

![A comment box with Send to agent turned off](plugin-asset://guide/captures/comment-human-note.webp)
*With Send to agent off, the comment is kept as a note.*

## Comment on a web page, an answer or a chart

The same controls work in the browser, on CLIO's answers and on the charts and maps in
the conversation. These comments are added to your next message, with **Add 2 marks to
message**, instead of becoming threads on a file.

![A web page in the browser with one element picked and its comment box open](plugin-asset://guide/captures/comment-browser-element-editor.webp)
*An element picked on a web page. CLIO receives it by its role and name, so it can act on that exact element.*

![A map in the conversation with a region drawn over three locations](plugin-asset://guide/captures/comment-a2ui-map-region.webp)
*A region on a map: CLIO receives the box, the area it covers and the points inside it.*

## On a phone

The controls fit a phone screen, and you can drag a region with your finger.

![A figure on a phone-sized screen with a region drawn by touch and its comment box](plugin-asset://guide/captures/comment-phone-touch-region-editor.webp)
*A region drawn by touch at phone width.*

> **Note**
>
> HTML files open in the browser, where their scripts and libraries run. Comment on them
> there.

To comment on a LaTeX paper's source or its PDF, see [Write a paper](docs/papers.md).
