import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Copy } from 'lucide-react';
import { detectOS } from '@/lib/downloads';

type Shell = 'powershell' | 'unix';

const COMMANDS: Record<Shell, { label: string; verb: string; url: string; pipe: string }> = {
	powershell: { label: 'Windows (PowerShell)', verb: 'irm', url: 'https://clio.iowarp.ai/desktop.ps1', pipe: 'iex' },
	unix: { label: 'macOS and Linux (Terminal)', verb: 'curl -fsSL', url: 'https://clio.iowarp.ai/desktop.sh', pipe: 'bash' },
};

/** "Get CLIO" with the one-line terminal install for the visitor's system, and a copy button. */
export default function InstallCommand() {
	const [shell, setShell] = useState<Shell>('unix');
	const [open, setOpen] = useState(false);
	const [copied, setCopied] = useState(false);
	const root = useRef<HTMLDivElement>(null);
	const command = COMMANDS[shell];
	const text = `${command.verb} ${command.url} | ${command.pipe}`;

	useEffect(() => {
		if (detectOS({ userAgent: navigator.userAgent, platform: navigator.platform }) === 'windows') setShell('powershell');
	}, []);

	useEffect(() => {
		if (!open) return;
		const close = (event: Event) => {
			if (event instanceof KeyboardEvent && event.key !== 'Escape') return;
			if (event instanceof PointerEvent && root.current?.contains(event.target as Node)) return;
			setOpen(false);
		};
		document.addEventListener('pointerdown', close);
		document.addEventListener('keydown', close);
		return () => {
			document.removeEventListener('pointerdown', close);
			document.removeEventListener('keydown', close);
		};
	}, [open]);

	const copy = async () => {
		await navigator.clipboard.writeText(text);
		setCopied(true);
		window.setTimeout(() => setCopied(false), 1800);
	};

	return (
		<div className="ic" ref={root}>
			<button
				type="button"
				className="h-button h-button-primary ic-get"
				aria-expanded={open}
				aria-haspopup="true"
				onClick={() => setOpen((value) => !value)}
			>
				Get CLIO <ChevronDown size={17} aria-hidden="true" />
			</button>
			{open ? (
				<div className="hd-menu ic-menu" role="menu">
					{(Object.keys(COMMANDS) as Shell[]).map((key) => (
						<button
							key={key}
							type="button"
							role="menuitemradio"
							aria-checked={shell === key}
							onClick={() => {
								setShell(key);
								setOpen(false);
							}}
						>
							<span>{COMMANDS[key].label}</span>
							{shell === key ? <Check size={15} aria-hidden="true" /> : null}
						</button>
					))}
					<a role="menuitem" href="#download">
						<span>Download the installer</span>
					</a>
				</div>
			) : null}
			<code className="ic-command" aria-label={`Install command for ${command.label}`}>
				<span className="ic-verb">{command.verb}</span> {command.url} <span className="ic-pipe">|</span>{' '}
				<span className="ic-verb">{command.pipe}</span>
			</code>
			<button type="button" className="ic-copy" onClick={copy} aria-label={copied ? 'Copied' : 'Copy the command'}>
				{copied ? <Check size={17} aria-hidden="true" /> : <Copy size={17} aria-hidden="true" />}
			</button>
		</div>
	);
}
