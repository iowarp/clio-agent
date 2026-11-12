import { useState } from 'react';
import { Check, Copy, Download, Server } from 'lucide-react';
import { PLATFORMS, RELEASES_PAGE, pickAsset } from '@/lib/downloads';
import { useRelease } from '@/components/overview/use-release';
import { distributionHref, distributionsOf, primaryHref, type PlatformIcons } from './HeroDownload';

/** Every desktop download, one column per system, with the visitor's own system marked. */
export default function DownloadPanel({ icons }: { icons: PlatformIcons }) {
	const state = useRelease();
	return (
		<div className="dp">
			{PLATFORMS.map((platform) => {
				const distributions = distributionsOf(platform);
				return (
					<section
						key={platform.os}
						className="dp-card"
						data-os={platform.os}
						data-current={state.os === platform.os || undefined}
					>
						<header>
							<img
								src={icons[platform.os]}
								alt=""
								width={28}
								height={28}
								data-mono={platform.os !== 'windows' || undefined}
							/>
							<div>
								<h3>{platform.name}</h3>
								<p>{platform.summary}</p>
							</div>
							{state.os === platform.os ? <span className="dp-yours">Your system</span> : null}
						</header>
						{platform.primary ? (
							<a className="h-button h-button-primary dp-main" href={primaryHref(state, platform.os)}>
								<Download size={17} aria-hidden="true" />
								{platform.primary.label}
							</a>
						) : null}
						{platform.others.map((choice) => (
							<a
								key={choice.format}
								className="dp-other"
								href={
									state.status === 'ready'
										? (pickAsset(state.assets, choice.format, choice.arch ?? state.arch)?.url ?? RELEASES_PAGE)
										: RELEASES_PAGE
								}
							>
								Or download the {choice.label} package
							</a>
						))}
						{platform.alternative ? (
							<p className="dp-alt">
								{platform.alternative.text} <a href={platform.alternative.href}>{platform.alternative.link}</a>
							</p>
						) : null}
						{distributions ? (
							<ul className="dp-distros">
								{distributions.map((item, index) => (
									<li key={item.name}>
										<span>{item.name}</span>
										<a className="h-button h-button-quiet" href={distributionHref(state, index)}>
											{item.choice ? item.choice.label : (item.alternative?.link ?? 'Releases')}
										</a>
									</li>
								))}
							</ul>
						) : null}
					</section>
				);
			})}
			<ServerCard windows={state.os === 'windows'} />
		</div>
	);
}

/** CLIO without the desktop app: the service in a browser, or one of the two container images. */
function ServerCard({ windows }: { windows: boolean }) {
	const ways = [
		{
			name: 'In your browser',
			note: 'Installs the service. Run clio to open it.',
			command: windows
				? 'irm https://clio.iowarp.ai/install.ps1 | iex'
				: 'curl -fsSL https://clio.iowarp.ai/install.sh | bash',
		},
		{
			name: 'Docker with Web',
			note: 'Then open localhost:8080.',
			command: 'docker run --rm -p 8080:80 ghcr.io/iowarp/clio-web:latest',
		},
		{
			name: 'Headless agent',
			note: 'The agent API on port 8100, for scripts and other clients.',
			command: 'docker run --rm -p 8100:8100 ghcr.io/iowarp/clio-api:latest',
		},
	];
	return (
		<section className="dp-card dp-server">
			<header>
				<Server size={28} aria-hidden="true" />
				<div>
					<h3>Web, server and Docker</h3>
					<p>For servers, HPC login nodes, containers and systems without the desktop app.</p>
				</div>
			</header>
			<ul className="dp-ways">
				{ways.map((way) => (
					<li key={way.name}>
						<div>
							<strong>{way.name}</strong>
							<span>{way.note}</span>
						</div>
						<CopyLine text={way.command} />
					</li>
				))}
			</ul>
			<p className="dp-alt">
				<a href="/docs/install/#install-script">Install script</a> and{' '}
				<a href="/docs/server-setup/#docker">Docker images</a> in the docs.
			</p>
		</section>
	);
}

function CopyLine({ text }: { text: string }) {
	const [copied, setCopied] = useState(false);
	const copy = async () => {
		await navigator.clipboard.writeText(text);
		setCopied(true);
		window.setTimeout(() => setCopied(false), 1800);
	};
	return (
		<div className="dp-line">
			<code>{text}</code>
			<button type="button" onClick={copy} aria-label={copied ? 'Copied' : `Copy ${text}`}>
				{copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
			</button>
		</div>
	);
}
