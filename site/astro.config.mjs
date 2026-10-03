// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import react from '@astrojs/react';
import tailwindcss from '@tailwindcss/vite';
import starlightLinksValidator from 'starlight-links-validator';

// Public site for CLIO, served by GitHub Pages at clio.iowarp.ai.
// The overview (/) is a Starlight splash page so the header, search, and
// theme switch are the same on every page. Docs live under /docs/ and
// tutorials under /tutorials/; a tutorial marked `draft: true` is built by
// `pnpm dev` and never published.
export default defineConfig({
	site: 'https://clio.iowarp.ai',
	trailingSlash: 'ignore',
	// The pre-Astro site published these three pages; keep their URLs working.
	redirects: {
		'/docs.html': '/docs/',
		'/uninstall.html': '/docs/uninstall/',
		'/contributing.html': '/docs/contributing/',
	},
	integrations: [
		starlight({
			title: 'CLIO',
			description:
				'An open-source AI workspace for scientific data: data discovery, analysis, expert agents, and provenance in one place.',
			logo: { src: './src/assets/brand/clio-mark.png', alt: 'CLIO' },
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
			plugins: [starlightLinksValidator({ errorOnLocalLinks: false })],
			sidebar: [
				{
					label: 'Start here',
					items: [
						{ label: 'What CLIO is', slug: 'docs' },
						{ label: 'Install', slug: 'docs/install' },
						{ label: 'Connect a model', slug: 'docs/connect-a-model' },
					],
				},
				{
					label: 'Use CLIO',
					items: [
						{ label: 'Sessions and modes', slug: 'docs/sessions' },
						{
							label: 'Widget gallery',
							collapsed: false,
							items: [
								{ label: 'Introduction', slug: 'docs/widgets' },
								{ label: 'Linked data', slug: 'docs/widgets/linked-data' },
								{ label: 'Human-agent interaction', slug: 'docs/widgets/interaction' },
								{ label: 'Component catalog', slug: 'docs/widgets/components' },
							],
						},
						{ label: 'Agent blueprints', slug: 'docs/blueprints' },
						{ label: 'Tools and MCP servers', slug: 'docs/mcp-servers' },
						{ label: 'Permissions and sandbox', slug: 'docs/permissions' },
						{ label: 'Hooks', slug: 'docs/hooks' },
						{ label: 'Memory and evidence', slug: 'docs/memory-and-evidence' },
					],
				},
				{
					label: 'Reference',
					items: [
						{ label: 'Command line', slug: 'docs/cli' },
						{ label: 'Configuration', slug: 'docs/configuration' },
						{ label: 'Data and network', slug: 'docs/privacy' },
					],
				},
				{
					label: 'Help',
					items: [
						{ label: 'Troubleshooting', slug: 'docs/troubleshooting' },
						{ label: 'Uninstall', slug: 'docs/uninstall' },
						{ label: 'Contributing', slug: 'docs/contributing' },
					],
				},
				{
					label: 'Tutorials',
					items: [{ label: 'All tutorials', slug: 'tutorials' }, { autogenerate: { directory: 'tutorials/guides' } }],
				},
			],
		}),
		react(),
	],
	vite: {
		plugins: [tailwindcss()],
	},
});
