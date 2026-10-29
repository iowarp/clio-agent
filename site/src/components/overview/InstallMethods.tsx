import { useEffect, useState } from 'react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import Command from './Command';

/** Install commands. Source: install/install.sh, install/install.ps1 and site/public/install.md. */
const SCRIPT_SH = 'https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh';
const SCRIPT_PS1 = 'https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1';

const METHODS = [
	{
		id: 'desktop',
		label: 'Desktop app',
		note: 'The same app as the download above, checked against the release checksums.',
		unix: `curl -fsSL ${SCRIPT_SH} | bash -s -- --desktop`,
		windows: `& ([scriptblock]::Create((irm ${SCRIPT_PS1}))) -Desktop`,
	},
	{
		id: 'terminal',
		label: 'Terminal and servers',
		note: 'The clio command, for servers, HPC and computers without a desktop. Then run clio.',
		unix: `curl -fsSL ${SCRIPT_SH} | bash`,
		windows: `irm ${SCRIPT_PS1} | iex`,
	},
] as const;

const PROMPT = 'Install CLIO on this computer by following https://clio.iowarp.ai/install.md';

/**
 * The terminal and AI-assistant ways to install, under the download cards.
 * Shows the visitor's own shell: PowerShell on Windows, a terminal elsewhere.
 */
export default function InstallMethods() {
	const [windows, setWindows] = useState(false);
	useEffect(() => {
		setWindows(/win/i.test(`${navigator.userAgent} ${navigator.platform}`));
	}, []);

	return (
		<Tabs defaultValue="desktop">
			<TabsList className="h-auto flex-wrap">
				{METHODS.map((method) => (
					<TabsTrigger key={method.id} value={method.id}>
						{method.label}
					</TabsTrigger>
				))}
				<TabsTrigger value="assistant">Your AI assistant</TabsTrigger>
			</TabsList>
			{METHODS.map((method) => (
				<TabsContent key={method.id} value={method.id} className="mt-3 flex flex-col gap-2">
					<Command
						command={windows ? method.windows : method.unix}
						language={windows ? 'powershell' : 'bash'}
						label={windows ? 'PowerShell' : 'macOS and Linux'}
					/>
					<p className="m-0 text-sm text-muted-foreground">
						{method.note}{' '}
						<button
							className="cursor-pointer border-0 bg-transparent p-0 text-sm text-foreground underline decoration-border underline-offset-4 hover:decoration-primary"
							onClick={() => setWindows(!windows)}
							type="button"
						>
							{windows ? 'Show the macOS and Linux command' : 'Show the Windows command'}
						</button>
					</p>
				</TabsContent>
			))}
			<TabsContent value="assistant" className="mt-3 flex flex-col gap-2">
				<Command command={PROMPT} language="text" label="Paste into your assistant" />
				<p className="m-0 text-sm text-muted-foreground">
					An assistant that can run commands, such as Claude Code or Codex, reads the instructions and installs
					CLIO for you.{' '}
					<a href="/docs/install-with-ai/" className="text-foreground">
						How it works
					</a>
				</p>
			</TabsContent>
		</Tabs>
	);
}
