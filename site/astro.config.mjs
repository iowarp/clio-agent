// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import react from '@astrojs/react';
import tailwindcss from '@tailwindcss/vite';
import starlightLinksValidator from 'starlight-links-validator';

// Public site for CLIO, served by GitHub Pages at clio.iowarp.ai.
// The overview (/) is a Starlight splash page so the header, search, and
// theme switch are the same on every page. Guides and examples live under
// /docs/. Older tutorial URLs redirect there; drafts appear only in `pnpm dev`.
export default defineConfig({
	site: 'https://clio.iowarp.ai',
	trailingSlash: 'ignore',
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
	},
	integrations: [
		starlight({
			title: 'CLIO',
			description:
				'An open-source AI workspace for scientific data: data discovery, analysis, expert agents, and provenance in one place.',
			logo: { src: './src/assets/brand/clio-mark.svg', alt: 'CLIO' },
			favicon: '/favicon.svg',
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/iowarp/clio-agent' }],
			editLink: { baseUrl: 'https://github.com/iowarp/clio-agent/edit/develop/site/' },
			lastUpdated: true,
			customCss: ['./src/styles/global.css'],
			// Wrap long prompts and commands instead of scrolling sideways; copy still copies the exact text.
			expressiveCode: { defaultProps: { wrap: true } },
			components: {
				Header: './src/components/starlight/Header.astro',
				Hero: './src/components/overview/Hero.astro',
				Footer: './src/components/starlight/Footer.astro',
			},
			plugins: [starlightLinksValidator({
				errorOnLocalLinks: false,
				// Static native HTML endpoints are checked by the archive browser tests.
				exclude: ['/conversations/ndp/alta/', '/conversations/ndp/tahoe/', '/conversations/ndp/konza/', '/conversations/ndp/endpoint/'],
			})],
			sidebar: [
				{
					label: 'Get started',
					collapsed: false,
					items: [
						{ label: 'Start here', slug: 'docs' },
						{ label: 'Install CLIO', slug: 'docs/install' },
						{ label: 'Install with your AI assistant', slug: 'docs/install-with-ai' },
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
						{ label: 'Follow work in progress', slug: 'docs/following-work' },
						{ label: 'Flows and workflows', slug: 'docs/flows' },
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
		react(),
	],
	vite: {
		plugins: [tailwindcss()],
		// Keep Satteri's native loader beside its platform package during
		// prerendering; bundling it changes createRequire(import.meta.url).
		ssr: { external: ['satteri'] },
	},
});
