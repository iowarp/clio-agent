// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import react from '@astrojs/react';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';
import { copyFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import starlightLinksValidator from 'starlight-links-validator';
import { sidebar } from './src/sidebar.mjs';

// Public site for CLIO, served by GitHub Pages at clio.iowarp.ai.
// The home page (/) is a standalone Astro page (src/pages/index.astro) with
// its own light layout; guides and examples are Starlight pages under /docs/. Older tutorial URLs redirect there; drafts appear only in `pnpm dev`.

// Client-side redirect pages under src/pages that must not be listed as content.
const SITEMAP_EXCLUDED_PATHS = ['/docs/memory-and-evidence/'];

export default defineConfig({
	site: 'https://clio.iowarp.ai',
	trailingSlash: 'ignore',
	// Fetch a page as soon as a link to it is hovered or tapped, so moving
	// between the home page and the docs feels immediate.
	prefetch: { prefetchAll: true, defaultStrategy: 'hover' },
	// The pre-Astro site published these three pages; keep their URLs working.
	redirects: {
		'/docs.html': '/docs/',
		'/uninstall.html': '/docs/uninstall/',
		'/contributing.html': '/docs/contributing/',
		'/tutorials': '/docs/examples/',
		'/tutorials/guides/first-analysis': '/docs/examples/ground-motion/',
		'/tutorials/guides/install-a-blueprint': '/docs/agents-and-plugins/#pick-an-agent-for-a-conversation',
		// Agent blueprints were replaced by agents and plugins.
		'/docs/blueprints': '/docs/agents-and-plugins/',
		'/versions': '/docs/versions/',
	},
	integrations: [
		starlight({
			title: 'CLIO',
			description:
				'An open-source AI workspace for scientific data: data discovery, analysis, expert agents, and provenance in one place.',
			logo: { src: './src/assets/brand/clio-mark-small.svg', alt: 'CLIO' },
			favicon: '/clio-mark-small.svg',
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/iowarp/clio-agent' }],
			editLink: { baseUrl: 'https://github.com/iowarp/clio-agent/edit/develop/site/' },
			lastUpdated: true,
			customCss: ['./src/styles/global.css'],
			// Wrap long prompts and commands instead of scrolling sideways; copy still copies the exact text.
			expressiveCode: { defaultProps: { wrap: true } },
			components: {
				Header: './src/components/starlight/Header.astro',
				Footer: './src/components/starlight/Footer.astro',
				ThemeProvider: './src/components/starlight/ThemeProvider.astro',
				ThemeSelect: './src/components/starlight/ThemeSelect.astro',
			},
			plugins: [starlightLinksValidator({
				errorOnLocalLinks: false,
				// Static native HTML endpoints are checked by the archive browser tests;
				// the home page is a custom page the validator cannot read.
				exclude: ['/conversations/**', '/', '/#download'],
			})],
			sidebar,
		}),
		// Registering the sitemap here replaces Starlight's default one, so
		// redirect-only alias pages can be left out of it.
		sitemap({ filter: (page) => !SITEMAP_EXCLUDED_PATHS.some((path) => page.endsWith(path)) }),
		// GitHub Pages cannot redirect /sitemap.xml, so publish the index there too.
		{
			name: 'clio-sitemap-alias',
			hooks: {
				'astro:build:done': async ({ dir }) => {
					await copyFile(new URL('sitemap-index.xml', dir), new URL('sitemap.xml', dir));
				},
			},
		},
		// Every docs page as Markdown at /docs/<path>.md, and llms.txt listing them: the
		// text the guide plugin bundles, with site links, for outside agents to read.
		{
			name: 'clio-docs-markdown',
			hooks: {
				'astro:build:done': async ({ dir }) => {
					execFileSync(process.execPath, ['scripts/export-docs-markdown.mjs', '--target', 'site', '--out', fileURLToPath(dir)], { stdio: 'inherit' });
				},
			},
		},
		react(),
	],
	vite: {
		plugins: [tailwindcss()],
		// Keep Satteri's native loader beside its platform package during
		// prerendering; bundling it changes createRequire(import.meta.url).
		ssr: { external: ['satteri'] },
	},
});
