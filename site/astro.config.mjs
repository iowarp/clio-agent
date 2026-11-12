// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import react from '@astrojs/react';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';
import { copyFile } from 'node:fs/promises';
import starlightLinksValidator from 'starlight-links-validator';

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
			},
			plugins: [starlightLinksValidator({
				errorOnLocalLinks: false,
				// Static native HTML endpoints are checked by the archive browser tests;
				// the home page is a custom page the validator cannot read.
				exclude: ['/conversations/**', '/', '/#download'],
			})],
			sidebar: [
				{
					label: 'Get started',
					collapsed: false,
					items: [
						{ label: 'Start here', slug: 'docs' },
						{ label: 'Install CLIO', slug: 'docs/install' },
						{ label: 'Install with your AI assistant', slug: 'docs/install-with-ai' },
						{ label: 'Remote deployment with CLIO', slug: 'tutorials/guides/remote-deployment' },
						{ label: 'Choose a model', slug: 'docs/connect-a-model' },
						{ label: 'Your first analysis', slug: 'docs/examples/check-a-dataset' },
						{ label: 'Your workspace', slug: 'docs/workspace-panels' },
						{ label: 'Features and versions', slug: 'docs/versions' },
					],
				},
				{
					label: 'Bring your data',
					collapsed: true,
					items: [
						{ label: 'Choose how to add data', slug: 'docs/files' },
						{ label: 'Attach files and folders', slug: 'docs/files/attach' },
						{ label: 'Refer to a file with @', slug: 'docs/files/references' },
						{ label: 'Connect a data source', slug: 'docs/files/connect' },
						{ label: 'National Data Platform', slug: 'docs/files/ndp' },
						{ label: 'NDP streams', slug: 'docs/files/ndp-streams' },
						{ label: 'Copies and original files', slug: 'docs/files/download-or-link' },
						{ label: 'Explore research notes', slug: 'docs/files/knowledge' },
						{ label: 'Manage your sources', slug: 'docs/files/manage' },
					],
				},
				{
					label: 'Work with CLIO',
					collapsed: true,
					items: [
						{ label: 'Start and steer a conversation', slug: 'docs/sessions' },
						{
							label: 'Tasks and coordination', collapsed: true,
							items: [
								{ label: 'Background work', slug: 'docs/tasks' },
								{ label: 'Observe and wait', slug: 'docs/tasks/observe-and-wait' },
								{ label: 'Subagent controls', slug: 'docs/tasks/subagents' },
								{ label: 'Messages and feedback', slug: 'docs/tasks/messages' },
								{ label: 'Tools and sources', slug: 'docs/tasks/tools-and-sources' },
								{ label: 'Session coordination', slug: 'docs/tasks/session-coordination' },
								{ label: 'Real conversations', slug: 'docs/tasks/recorded-conversations' },
							],
						},
						{ label: 'Answer a question', slug: 'docs/questions' },
						{ label: 'Use the browser', slug: 'docs/browser' },
						{ label: 'Scripts and background work', slug: 'docs/scripts' },
						{ label: 'Watch something live', slug: 'docs/live-sources' },
						{ label: 'Follow work in progress', slug: 'docs/following-work' },
						{ label: 'Flows and workflows', slug: 'docs/flows' },
						{ label: 'Subagents', slug: 'docs/subagents' },
						{ label: 'Commands and @ mentions', slug: 'docs/commands' },
						{ label: 'Science log', slug: 'docs/science-log' },
						{ label: 'Models and usage', slug: 'docs/models-and-usage' },
						{ label: 'Updates during a conversation', slug: 'docs/injections' },
						{ label: 'Continue work over time', slug: 'docs/long-running-work' },
						{ label: 'Choose what CLIO may do', slug: 'docs/permissions' },
					],
				},
				{
					label: 'Review and share results',
					collapsed: true,
					items: [
						{ label: 'Check answers and sources', slug: 'docs/evidence' },
						{ label: 'Open and edit documents', slug: 'docs/working-with-files' },
						{ label: 'Comment on results', slug: 'docs/comments' },
						{ label: 'Write a paper', slug: 'docs/papers' },
						{ label: 'Explore charts and dashboards', slug: 'docs/dashboards' },
						{ label: 'Compare alternative answers', slug: 'docs/best-of-n' },
						{ label: 'Save and share your work', slug: 'docs/exports' },
					],
				},
				{
					label: 'Find and revisit work',
					collapsed: true,
					items: [
						{ label: 'Search files and conversations', slug: 'docs/search' },
						{ label: 'Long-term memory', slug: 'docs/long-term-memory' },
						{ label: 'What CLIO remembers', slug: 'docs/memory' },
						{ label: 'Try another approach', slug: 'docs/branch-and-rewind' },
					],
				},
				{
					label: 'Worked examples',
					collapsed: true,
					items: [
						{ label: 'Choose an example', slug: 'docs/examples' },
						{ label: 'Explore ground motion', slug: 'docs/examples/ground-motion' },
						{ label: 'EarthScope data from NDP', slug: 'docs/examples/ndp-earthscope' },
						{ label: 'Scientific questions for NDP', slug: 'docs/examples/ndp-science' },
						{ label: 'A branched study', slug: 'docs/examples/branched-study' },
						{ label: 'Find a decision in research notes', slug: 'docs/examples/research-notes' },
						{ label: 'Create a report and slides', slug: 'docs/examples/reports-and-slides' },
					],
				},
				{
					label: 'Customize CLIO',
					collapsed: true,
					items: [
						{ label: 'Agents and plugins', slug: 'docs/agents-and-plugins' },
						{ label: 'Extensions preview', slug: 'docs/extensions' },
						{ label: 'Plugin marketplaces', slug: 'docs/marketplaces' },
						{ label: 'Add and manage marketplaces', slug: 'docs/marketplaces/manage' },
						{ label: 'Connect tools and services', slug: 'docs/mcp-servers' },
						{ label: 'Interactive views and controls', slug: 'docs/widgets' },
					],
				},
				{
					label: 'Build with the SDK',
					collapsed: true,
					items: [
						{ label: 'How agents and plugins are built', slug: 'docs/sdk' },
						{ label: 'Build your first plugin', slug: 'docs/sdk/build-a-plugin' },
						{ label: 'Write a flow', slug: 'docs/sdk/flows' },
						{ label: 'Workflows as data', slug: 'docs/sdk/workflows' },
						{ label: "What's available", slug: 'docs/sdk/available' },
						{ label: 'Plugin reference', slug: 'docs/sdk/plugins' },
						{ label: 'Agent reference', slug: 'docs/sdk/agents' },
						{ label: 'SDK reference', slug: 'docs/sdk/reference' },
					],
				},
				{
					label: 'Help and privacy',
					collapsed: true,
					items: [
						{ label: 'When something goes wrong', slug: 'docs/troubleshooting' },
						{ label: 'Where your data goes', slug: 'docs/privacy' },
						{ label: 'Remove CLIO', slug: 'docs/uninstall' },
						{ label: 'Privacy Policy', slug: 'privacy' },
						{ label: 'Terms of Service', slug: 'terms' },
					],
				},
				{
					label: 'Technical reference',
					collapsed: true,
					items: [
						{ label: 'Server and terminal setup', slug: 'docs/server-setup' },
						{ label: 'Managed infrastructure', slug: 'docs/managed-infrastructure' },
						{ label: 'Command line', slug: 'docs/cli' },
						{ label: 'Configuration', slug: 'docs/configuration' },
						{ label: 'Hooks and automation', slug: 'docs/hooks' },
						{ label: 'Background work and limits', slug: 'docs/background-work-reference' },
						{ label: 'Interactive component catalog', slug: 'docs/widgets/components' },
						{ label: 'Contribute to CLIO', slug: 'docs/contributing' },
					],
				},
			],
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
		react(),
	],
	vite: {
		plugins: [tailwindcss()],
		// Keep Satteri's native loader beside its platform package during
		// prerendering; bundling it changes createRequire(import.meta.url).
		ssr: { external: ['satteri'] },
	},
});
