---
title: "Ask CLIO how to use CLIO"
description: "Ask how to do something in CLIO, or whether it can, and get the answer from the documentation of the version you have, with its screenshots."
source: https://clio.iowarp.ai/docs/ask-clio/
---

# Ask CLIO how to use CLIO

Ask how to do something in CLIO, or whether it can, and get the answer from the documentation of the version you have, with its screenshots.

Ask CLIO about itself the way you would ask a colleague who knows it well: "How do I make
CLIO refuse to touch my raw data?", "Can CLIO watch my instrument while I work?", "What
does this permission question mean?". It answers from the documentation of the CLIO
version you are running, names the page it used, and links it so you can read more.

The documentation ships with CLIO, so the answer matches your version even when this
website describes a newer one, and it works offline.

## What you get back

- **A short answer** for a narrow question, with the page it comes from.
- **Steps with the screens they happen on** for a how-to with several actions in the app.
  The screenshots are the ones on these pages, shown in the conversation.
- **A diagram** when the question is how parts fit together, such as what a plugin
  contains.
- **A choice** when you have to pick, such as which kind of plugin part or which
  marketplace, with what each option means.

A broad question ("How should I set up CLIO for my lab's spectroscopy analysis?") is
handed to a helper that reads as many pages as it needs, so the conversation keeps its
room for your work. You can run that helper on a cheaper model: in **Settings > Models**,
under **Helper models**, choose a model for **Guide**.

## Questions about your own CLIO

"Which plugins do I have?" or "Can my agent read HDF5 files?" are about your
installation, not the documentation. CLIO answers them from the conversation's own
agent and tools, and uses the documentation only to explain what each one does.

## Turn it off

The guide is the `guide` plugin, carried by every agent. Remove it from a conversation in
its agent menu, or from your own agent, like any other plugin. See
[Agents and plugins](docs/agents-and-plugins.md).
