import { Download } from 'lucide-react';
import { Badge } from '@/components/reui/badge';
import { Frame, FramePanel } from '@/components/reui/frame';
import { Button } from '@/components/ui/button';
import {
	BROWSER_INSTALL,
	DOWNLOAD_NOTE,
	PLATFORMS,
	RELEASES_PAGE,
	pickAsset,
	type Alternative,
	type Distribution,
	type DownloadChoice,
	type OS,
	type PlatformDownloads,
} from '@/lib/downloads';
import { useRelease, type ReleaseState } from './use-release';

/** Platform marks, vendored from Devicon (see src/assets/platform-icons/README.md). */
export type PlatformIcons = Record<OS, string>;

interface Props {
	icons: PlatformIcons;
}

/**
 * The three desktop download cards.
 *
 * Server-rendered with every link pointing at the releases page, so the
 * section works without JavaScript and when GitHub cannot be reached. In the
 * browser the visitor's platform card moves first and each link resolves to
 * the exact installer in the latest release.
 */
export default function DownloadCards({ icons }: Props) {
	const release = useRelease();
	const ordered = [...PLATFORMS].sort((a, b) => Number(b.os === release.os) - Number(a.os === release.os));

	return (
		<div className="grid gap-4 md:grid-cols-3">
			{ordered.map((platform) => (
				<PlatformCard
					key={platform.os}
					platform={platform}
					icon={icons[platform.os]}
					release={release}
					detected={platform.os === release.os}
				/>
			))}
		</div>
	);
}

function resolve(release: ReleaseState, choice: DownloadChoice | undefined) {
	if (release.status !== 'ready' || !choice) return null;
	return pickAsset(release.assets, choice.format, choice.arch ?? release.arch);
}

interface CardProps {
	platform: PlatformDownloads;
	icon: string;
	release: ReleaseState;
	detected: boolean;
}

function PlatformCard({ platform, icon, release, detected }: CardProps) {
	const primary = resolve(release, platform.primary);
	const distributions = platform.distributions ?? [];
	const offered = primary !== null || distributions.some((d) => resolve(release, d.choice) !== null);
	return (
		<Frame className={detected ? '[--frame-border-color:var(--color-primary)]' : undefined}>
			<FramePanel className="flex h-full flex-col gap-4">
				<div className="flex items-start justify-between gap-3">
					<img
						src={icon}
						alt=""
						width={36}
						height={36}
						// The Apple and Linux marks are black; flip them to stay visible on the dark theme.
						className={platform.os === 'windows' ? 'size-9' : 'size-9 dark:invert'}
					/>
					{detected && (
						<Badge variant="primary-light" size="sm">
							Your platform
						</Badge>
					)}
				</div>
				<div>
					<h3 className="m-0 font-display text-2xl text-foreground">{platform.name}</h3>
					<p className="m-0 mt-1 text-sm text-muted-foreground">{platform.summary}</p>
				</div>
				<div className="mt-auto flex flex-col gap-3">
					{platform.primary && (
						<Button
							className="h-10 w-full gap-2"
							render={<a href={primary?.url ?? RELEASES_PAGE} />}
							nativeButton={false}
						>
							<Download aria-hidden="true" />
							{platform.primary.label}
						</Button>
					)}
					{distributions.length > 0 && (
						<ul className="m-0 flex list-none flex-col gap-2 p-0">
							{distributions.map((distribution) => (
								<DistributionRow key={distribution.name} distribution={distribution} release={release} />
							))}
						</ul>
					)}
					{platform.alternative && <AlternativeLink alternative={platform.alternative} />}
					{platform.others.length > 0 && (
						<div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
							<span className="text-muted-foreground">Also</span>
							{platform.others.map((choice) => {
								const asset = resolve(release, choice);
								return (
									<a
										key={choice.label}
										href={asset?.url ?? RELEASES_PAGE}
										className="text-foreground underline decoration-border underline-offset-4 hover:decoration-primary"
									>
										{choice.label}
									</a>
								);
							})}
						</div>
					)}
					<p className="m-0 min-h-10 text-xs text-muted-foreground">{offered ? DOWNLOAD_NOTE : ''}</p>
				</div>
			</FramePanel>
		</Frame>
	);
}

function AlternativeLink({ alternative }: { alternative: Alternative }) {
	return (
		<div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
			<span className="text-muted-foreground">{alternative.text}</span>
			<a
				href={alternative.href}
				className="text-foreground underline decoration-border underline-offset-4 hover:decoration-primary"
			>
				{alternative.link}
			</a>
		</div>
	);
}

/**
 * One distribution family and its package. Before the release is known the link is the
 * releases page; a release that lacks the package sends the visitor to the browser
 * install instead of to a page that does not have it.
 */
function DistributionRow({ distribution, release }: { distribution: Distribution; release: ReleaseState }) {
	const asset = resolve(release, distribution.choice);
	const missing = distribution.choice && release.status === 'ready' && asset === null;
	const alternative = distribution.alternative ?? (missing ? BROWSER_INSTALL : undefined);
	return (
		<li className="flex flex-col gap-1 border-t border-border pt-2 first:border-t-0 first:pt-0">
			<span className="text-sm text-foreground">{distribution.name}</span>
			{distribution.choice && !missing ? (
				<Button
					variant="outline"
					className="h-9 w-full gap-2 text-foreground hover:text-foreground"
					render={<a href={asset?.url ?? RELEASES_PAGE} />}
					nativeButton={false}
				>
					<Download aria-hidden="true" />
					Download {distribution.choice.label}
				</Button>
			) : (
				alternative && <AlternativeLink alternative={alternative} />
			)}
		</li>
	);
}
