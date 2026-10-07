/**
 * Copy and structure for the overview page.
 *
 * Writing rules for this file:
 * - Describe what a user can do, with concrete verbs. No superlatives.
 * - Every capability shown here must be visible in the capture next to it.
 * - Facts (providers, commands, ports) must match the code; cite the source
 *   module in a comment when the fact is not obvious.
 */
import type { ImageMetadata } from 'astro';
import earthscopeMap from '@/assets/captures/earthscope-map.png';
import factorioReport from '@/assets/captures/factorio-report.png';
import agentGantt from '@/assets/captures/agent-gantt.png';
import provenance from '@/assets/captures/provenance-evidence.png';
import deepSearch from '@/assets/captures/deep-search.png';

/** One numbered capability section with its capture. */
export interface Feature {
	id: string;
	label: string;
	title: string;
	/** Words of the title set in the accent italic. */
	emphasis: string;
	body: string;
	example?: string;
	link: { label: string; href: string };
	image: ImageMetadata;
	alt: string;
	caption: string;
}

export const features: readonly Feature[] = [
	{
		id: 'ask',
		label: 'Ask',
		title: 'Start from a question about',
		emphasis: 'real data.',
		body: 'Describe what you want to know. CLIO finds the data source, pulls real records, and stops to ask you when a choice is yours to make, such as which station to analyze.',
		example: 'Find the five EarthScope GNSS stations nearest Palm Springs and let me pick one.',
		link: { label: 'Your first analysis', href: '/tutorials/guides/first-analysis/' },
		image: earthscopeMap,
		alt: 'An interactive map in a CLIO session listing the five nearest EarthScope GNSS stations for the user to choose from',
		caption: 'CLIO waits for you to choose a station on the map before the analysis continues.',
	},
	{
		id: 'delegate',
		label: 'Delegate',
		title: 'Experts work',
		emphasis: 'in parallel.',
		body: 'An agent blueprint declares which domain experts a session can call on. They run as child sessions in parallel, and their findings come back as versioned artifacts with lineage.',
		link: { label: 'Agent blueprints', href: '/docs/blueprints/' },
		image: factorioReport,
		alt: 'A CLIO session where several expert agents completed in parallel and produced a validation report shown in the side panel',
		caption: 'Six expert consultations completed, with the resulting report open beside the session.',
	},
	{
		id: 'watch',
		label: 'Watch',
		title: 'See every step',
		emphasis: 'as it runs.',
		body: 'Agents, child agents, and tool calls appear on one timeline with their arguments and outcomes, so you can follow the work while it happens and check it afterwards.',
		link: { label: 'Memory and evidence', href: '/docs/memory-and-evidence/' },
		image: agentGantt,
		alt: 'A Gantt timeline in CLIO showing the main agent, child agents, and individual tool calls over time',
		caption: 'The execution timeline for a run with child agents and tool calls.',
	},
	{
		id: 'verify',
		label: 'Verify',
		title: 'Keep the evidence',
		emphasis: 'with the answer.',
		body: 'Files read, changes proposed, and artifacts created stay attached to the run that produced them. Edits wait for your approval, and each artifact records a content hash.',
		link: { label: 'Permissions and sandbox', href: '/docs/permissions/' },
		image: provenance,
		alt: 'The CLIO evidence panel listing files read by the agent, changed files, and generated artifacts beside the session',
		caption: 'The evidence panel: what the agent read, what it changed, and what it produced.',
	},
	{
		id: 'continue',
		label: 'Continue',
		title: 'Pick up',
		emphasis: 'earlier work.',
		body: 'CLIO can search your past sessions and bring their results into a new question, so analyses build on each other instead of starting over.',
		link: { label: 'Memory and evidence', href: '/docs/memory-and-evidence/' },
		image: deepSearch,
		alt: 'A CLIO session searching session memory and listing matching excerpts from earlier sessions',
		caption: 'Session memory search returning matches from earlier sessions in the workspace.',
	},
];

/** Model providers, grouped as a user thinks about them. Source: providers/catalog.py. */
export const modelGroups = [
	{
		title: 'On your machine',
		detail: 'Your model prompts stay local.',
		items: ['LM Studio', 'Ollama', 'llama.cpp', 'vLLM'],
	},
	{
		title: 'Cloud APIs',
		detail: 'Bring your own API key.',
		items: ['OpenAI', 'Anthropic', 'Google Gemini', 'Vertex AI', 'Azure OpenAI', 'AWS Bedrock', 'NVIDIA NIM', 'OpenRouter'],
	},
	{
		title: 'Subscriptions',
		detail: 'Use a plan you already have.',
		items: ['Claude Code', 'Codex'],
	},
	{
		title: 'Research computing',
		detail: 'Sign in with Globus.',
		items: ['ALCF Sophia', 'ALCF Metis'],
	},
] as const;

/** Frequently asked questions. Answers link into the docs for detail. */
export const faq: readonly { q: string; a: string }[] = [
	{
		q: 'Does CLIO need an account?',
		a: 'CLIO itself needs no account. Connect a model provider, or use a local model without signing in. Accounts for private data sources, such as GitHub and Google Drive, are optional and managed separately in Settings.',
	},
	{
		q: 'Can I use it with local models only?',
		a: 'Yes. Connect LM Studio, Ollama, llama.cpp, or vLLM and every prompt stays on your machine. Long, multi-step analyses are more reliable on larger models.',
	},
	{
		q: 'What leaves my machine?',
		a: 'Your prompts and data go only to the model provider you pick and to any tools you add. Separately, the launcher and desktop app check for updates and CLIO downloads public model metadata. There is no product telemetry. See Data and network in the docs.',
	},
	{
		q: 'Can CLIO change my files?',
		a: 'Reading is always allowed. Anything that writes, runs, or reaches the network asks you first unless you have allowed it for the session or workspace. File access is limited to your workspace and temporary files, and an optional OS-level sandbox can enforce that.',
	},
	{
		q: 'What is an agent blueprint?',
		a: 'A blueprint is a folder of Markdown that defines an agent: its instructions, the experts it can call, and its tools. You can install blueprints from the marketplace, write your own, and switch blueprints per session.',
	},
	{
		q: 'How mature is it?',
		a: 'CLIO is in beta and releases ship often. Expect rough edges, and please report them on GitHub so they get fixed.',
	},
	{
		q: 'Is it free?',
		a: 'Yes. CLIO is open source under the BSD 3-Clause license. You pay only for the models you use, if they are paid.',
	},
];
