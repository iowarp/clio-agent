# Writing for clio.iowarp.ai

Rules for every page on the public site: the overview, the docs, and the tutorials.

## Voice

- Write for someone using CLIO, not for the people building it. Lead with what the reader can do.
- Use concrete verbs and familiar nouns. One short paragraph per idea. Put details in commands, examples, and links.
- Do not use superlatives, sales language, or invented outcomes. Avoid "revolutionary", "seamless(ly)", "cutting-edge", "world-class", "unlock", and "powerful".
- Do not narrate development ("we recently", "now supports", "coming soon"). Describe how things work today.
- Do not use internal vocabulary. Say "the CLIO backend", not "gact server", "ARC keystone", "DSPy", "turn.py", or issue numbers. Say "workspace interface", not "GACT".
- Use American English and sentence-case headings.
- Do not use middots or similar characters as field separators. Use layout: lists, tables, or separate lines.

## Accuracy

- Every command, path, port, flag, environment variable, and provider name must match the repository. Check the source before you write it: `install/` for the launcher and scripts, `src/clio_agent/providers/catalog.py` for models, `src/clio_agent/paths.py` and `conf.py` for configuration, and `docs/` for the design references.
- If you cannot verify a fact, leave it out. Do not guess.
- Platform paths differ. User configuration lives in `~/.config/clio-agent` on Linux, `~/Library/Application Support/clio-agent` on macOS, and under `%LOCALAPPDATA%` on Windows. `CLIO_USER_DIR` overrides all three.

## Starlight components

Import them from `@astrojs/starlight/components` in `.mdx` files:

- `<Steps>` for ordered procedures, wrapping a Markdown ordered list.
- `<Tabs syncKey="os">` and `<TabItem label="macOS and Linux">` / `<TabItem label="Windows">` for platform variants. Use `syncKey="os"` everywhere so a reader's choice carries across pages.
- `<Aside type="note|tip|caution|danger">` for one important point. Use at most two per page.
- `<CardGrid>` and `<LinkCard>` for "next steps" at the end of a page.
- `<FileTree>` for directory layouts.

## Tutorials

- A tutorial is a real workflow a reader can repeat end to end. It needs real captures, recorded from a real CLIO session.
- An unfinished tutorial carries `draft: true` in its frontmatter. `pnpm dev` builds drafts, and production builds leave them out. Each draft also lists the captures it still needs, in a `:::note[Captures needed]` block, so whoever records them knows what to take.
- Put tutorial images in `src/assets/tutorials/<slug>/` and reference them with relative Markdown image syntax, so Astro can optimize them.
