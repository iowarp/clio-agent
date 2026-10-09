# clio.iowarp.ai

The public site for CLIO: the overview, the user docs, and the tutorials. It is an [Astro](https://astro.build) site built with [Starlight](https://starlight.astro.build), deployed to GitHub Pages by `.github/workflows/pages.yml` whenever `main` changes.

Read [WRITING.md](WRITING.md) before you write or edit a page.

Use [FEATURE_COVERAGE.md](FEATURE_COVERAGE.md) when updating product features. It
maps their reader entry points, retained examples, and release availability.

## Run it locally

```sh
cd site
pnpm install
pnpm dev          # http://localhost:4321, drafts included
pnpm test         # unit tests (download resolver, release version)
pnpm check        # type check
pnpm build        # production build; fails on any broken internal link
pnpm preview      # serve the production build
```

The build reads the CLIO version from `../pyproject.toml`, so run it from inside the repository.

## Where things live

| Path | What it holds |
|---|---|
| `src/content/docs/index.mdx` | The overview page. Its sections are components in `src/components/overview/`, and its copy is in `src/data/overview.ts`. |
| `src/content/docs/docs/widgets.mdx` | The interactive Widgets page, backed by the production gact-tui gallery. |
| `src/content/docs/docs/` | User docs. Add a page here, then list it in the `sidebar` in `astro.config.mjs`. |
| `src/content/docs/tutorials/guides/` | Tutorials. They appear in the sidebar automatically. |
| `src/assets/captures/` | Product captures used on the overview. |
| `src/assets/tutorials/<slug>/` | Images for one tutorial. |
| `src/styles/global.css` | Brand tokens for both themes, mapped onto Starlight and onto the shadcn/reui components. |
| `src/lib/downloads.ts` | Chooses the right desktop installer from the latest GitHub release. It is covered by `downloads.test.ts`. |
| `src/components/starlight/` | Header and footer overrides, shared by every page. |
| `src/components/ui/`, `src/components/reui/` | shadcn and reui components, added with `pnpm dlx shadcn@latest add`. Do not hand-roll a replacement for one of these. |

Icons come from [Lucide](https://lucide.dev) (`lucide-react`). Do not draw your own SVG icons. The platform marks in `src/assets/platform-icons/` are vendored from Devicon; see the README in that folder.

## Widget gallery

The Widgets page embeds the standalone gallery from `gact-tui`. The Pages workflow checks out the pinned gact-tui commit, builds only its gallery entrypoint with `CLIO_GALLERY_STANDALONE=1`, and stages the result under `public/widgets/`. This generated directory is ignored by Git.

For local review, build the gact-tui gallery and then run `node scripts/stage-widgets.mjs <path-to-gact-tui>/web/dist` from `site/` before `pnpm dev` or `pnpm build`. Update the workflow's pinned commit when a reviewed gallery change should appear on the public site.

## Add a tutorial

1. Create `src/content/docs/tutorials/guides/<slug>.mdx` with `title`, `description`, `sidebar.label`, and `draft: true`.
2. Write the steps from a real run, and list the screenshots it needs in a `:::note[Captures needed]` block.
3. Record those captures from a real CLIO session and put them in `src/assets/tutorials/<slug>/`.
4. Remove `draft: true`, add a `LinkCard` for it to `src/content/docs/tutorials/index.mdx`, and open a pull request.

Drafts show up in `pnpm dev` and are left out of production builds, so a half-finished tutorial never goes live.

## Refresh product captures

Captures must come from a real session of the current release. Do not use mockups or edited images. Replace a file in `src/assets/captures/` and keep its name, and the overview picks it up. Astro converts each capture to responsive WebP at build time. The capture plan lives in [`docs/CLIO_INSTALL_PAGE_IMAGE_MANIFEST.md`](../docs/CLIO_INSTALL_PAGE_IMAGE_MANIFEST.md).
