---
name: clio
description: Answer questions about CLIO itself from the documentation of the installed version - what CLIO can do, how to do or set up something in it, which of its features fit a goal, what a feature, setting or message means, and why it behaved some way. Use when the person asks "how do I...", "can CLIO...", "how should I set up CLIO for...", "what's the best way to ... in CLIO", "where is...", "what does ... mean" or "why did CLIO..." about the app, not about their data. For advice on setting CLIO up, the docs describe the options to choose from.
---

# Answering questions about CLIO

The pages bundled with this skill are the docs of the CLIO version that is running.
The website may describe a newer version, so answer from these pages, not from
memory or from the web.

## How to answer

1. Read `docs-map.md` (`load_skill("clio", file="docs-map.md")`). It lists every page
   with its path, title and description, grouped as the docs sidebar groups them.
2. Load only the pages the question needs, in one call:
   `load_skill("clio", files=["docs/hooks.md", "docs/permissions.md"])`.
3. Answer from those pages. Name each page you used by its title and give its site
   address (`https://clio.iowarp.ai/docs/<path>/`, the `source` line at the top of each
   page) so the person can open it.
4. When the pages do not cover the question, say so plainly. Do not fill the gap
   with guesses about menus, settings or behaviour.

A question that needs more than two or three pages ("how should I set up CLIO for
my lab's analysis?", "what is the best way to automate this?") goes to the
`clio-guide` subagent with the person's question in their words. It reads what it
needs in its own context and returns the answer, the pages it used, and the steps
and screenshots that belong in the reply. Present its answer the way "Show instead
of tell" below describes: its steps with the screenshots it named, as Markdown
images under their steps, and its diagram as a diagram. Do not read the same pages again.

## The person's words and the page that answers

People describe what they want, not the feature's name. Look for the feature
behind the words:

| They say | Look at |
| --- | --- |
| "make it remember a rule", "always do X", "stop it from touching Y" | prompt fragments and hooks (`sdk/plugins.md`, `hooks.md`, `permissions.md`) |
| "do these steps every time", "a recipe", "a checklist" | skills and flows (`sdk/plugins.md`, `flows.md`, `sdk/flows.md`) |
| "connect to my instrument's / lab's service", "use my account on..." | connections and connectors (`connectors.md`, `mcp-servers.md`, `extensions.md`) |
| "a helper", "a second opinion", "split the work" | subagents and background work (`subagents.md`, `tasks/index.md`) |
| "watch it live", "keep an eye on" | streams and live sources (`live-sources.md`, `scripts.md`) |
| "keep a record of what we found", "lab notebook" | the science log (`science-log.md`) |
| "it remembers / forgot something about me" | long-term memory (`long-term-memory.md`, `memory.md`) |
| "turn this into something reusable", "make a plugin" | `/capture` (the builder plugin), `agents-and-plugins.md`, `sdk/build-a-plugin.md` |
| "it asked me something", "it's waiting for me" | `questions.md`, `following-work.md` |
| "it refused", "it is not allowed to" | `permissions.md`, `troubleshooting.md` |

The map's descriptions are the final word: use them when a row above does not fit.

## Show instead of tell

Show when a picture helps more than prose. The pages carry their screenshots as
Markdown images (`![alt](plugin-asset://guide/captures/<name>.webp)`); put the ones
that help into your answer exactly as the page writes them, next to the step they
show. They render in the conversation from the installed docs, offline.

- **A how-to with more than three actions in the app:** numbered steps, one action
  each, with the screenshot of each screen under its step when the page has one. In
  a client that renders interactive views, a `clio.steps.v1` view followed by the
  captures as `Image` components (`url` is the same `plugin-asset://` reference) can
  replace the list; load the `a2ui-catalog-clio-workspace` skill for its fields.
- **Where something is in the app:** the page's screenshot of that screen.
- **How parts fit together** (a plugin's parts, a flow's shape, what calls what): a
  Mermaid diagram (a `mermaid` code block, or `clio.mermaid.v1`).
- **A choice the person has to make** (which part type, which marketplace, which
  model): a `ChoicePicker` with one option per choice and what each means, or a short
  table.

Use a screenshot only when the page you answer from shows it, and only the ones that
help: one screen is often enough. A short answer stays prose. Never link an image
from the web; every screenshot the docs have is bundled.

## Questions about this installation

"Which plugins do I have?", "what does my agent carry?", "is the hdf5 tool
available?" are about this CLIO, not about the docs. Answer them from what the
running conversation knows: the agent's plugin list in the session facts,
`search_tools` for a tool, and the plugin inventory. Use the docs only to explain
what a plugin or setting does, never to say what is installed.
