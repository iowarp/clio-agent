import { useEffect, useState } from 'react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import Command from './Command';

/** Install one-liners. Source: install/README.md, install/install.sh, install/install.ps1. */
const UNIX = 'curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash';
const WINDOWS = 'irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex';

/**
 * Install-script tabs. Server-rendered on macOS / Linux; switches to the
 * Windows tab after load when the visitor is on Windows.
 */
export default function InstallTabs() {
	const [tab, setTab] = useState<string>('unix');
	useEffect(() => {
		if (/win/i.test(`${navigator.userAgent} ${navigator.platform}`)) setTab('windows');
	}, []);

	return (
		<Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
			<TabsList>
				<TabsTrigger value="unix">macOS and Linux</TabsTrigger>
				<TabsTrigger value="windows">Windows</TabsTrigger>
			</TabsList>
			<TabsContent value="unix" className="mt-3">
				<Command command={UNIX} label="Terminal" />
			</TabsContent>
			<TabsContent value="windows" className="mt-3">
				<Command command={WINDOWS} language="powershell" label="PowerShell" />
			</TabsContent>
		</Tabs>
	);
}
