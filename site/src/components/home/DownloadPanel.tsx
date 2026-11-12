import { Download } from 'lucide-react';
import { PLATFORMS, RELEASES_PAGE, pickAsset } from '@/lib/downloads';
import { useRelease } from '@/components/overview/use-release';
import { distributionHref, primaryHref, type PlatformIcons } from './HeroDownload';

/** Every desktop download, one column per system, with the visitor's own system marked. */
export default function DownloadPanel({ icons }: { icons: PlatformIcons }) {
	const state = useRelease();
	return (
		<div className="dp">
			{PLATFORMS.map((platform) => (
				<section key={platform.os} className="dp-card" data-current={state.os === platform.os || undefined}>
					<header>
						<img src={icons[platform.os]} alt="" width={28} height={28} />
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
					{platform.distributions ? (
						<ul className="dp-distros">
							{platform.distributions.map((item, index) => (
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
			))}
		</div>
	);
}
