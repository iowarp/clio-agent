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
		q: 'What is an agent?',
		a: 'An agent is a name, a role, and a set of plugins. Plugins bring its tools, skills, subagents, and prompt guidance. You install agents and plugins from marketplaces, choose an agent for each session, add or remove plugins as you work, and save the result as your own agent.',
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
