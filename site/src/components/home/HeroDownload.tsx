import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Download } from 'lucide-react';
import { PLATFORMS, RELEASES_PAGE, pickAsset, type OS } from '@/lib/downloads';
import { useRelease, type ReleaseState } from '@/components/overview/use-release';

export type PlatformIcons = Record<OS, string>;

const NAMES: Record<OS, string> = { windows: 'Windows', macos: 'macOS', linux: 'Linux' };

/** The installer for a platform's main choice, or the releases page until the lookup finishes. */
export function primaryHref(state: ReleaseState, os: OS): string {
	const platform = PLATFORMS.find((item) => item.os === os);
	const choice = platform?.primary;
	if (!choice || state.status !== 'ready') return RELEASES_PAGE;
	return pickAsset(state.assets, choice.format, choice.arch ?? state.arch)?.url ?? RELEASES_PAGE;
}

/** The download link for one Linux distribution. */
export function distributionHref(state: ReleaseState, index: number): string {
	const distribution = PLATFORMS.find((item) => item.os === 'linux')?.distributions?.[index];
	if (!distribution) return RELEASES_PAGE;
	if (distribution.alternative) return distribution.alternative.href;
	if (!distribution.choice || state.status !== 'ready') return RELEASES_PAGE;
	return pickAsset(state.assets, distribution.choice.format, state.arch)?.url ?? RELEASES_PAGE;
}

function LinuxMenu({ state, icon }: { state: ReleaseState; icon: string }) {
	const [open, setOpen] = useState(false);
	const root = useRef<HTMLDivElement>(null);
	const distributions = PLATFORMS.find((item) => item.os === 'linux')?.distributions ?? [];

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

	return (
		<div className="hd-menu-root" ref={root}>
			<button
				type="button"
				className="h-button h-button-primary hd-main"
				aria-expanded={open}
				aria-haspopup="true"
				onClick={() => setOpen((value) => !value)}
			>
				<img src={icon} alt="" width={20} height={20} />
				Download for Linux
				<ChevronDown size={18} aria-hidden="true" className="hd-chevron" />
			</button>
			{open ? (
				<div className="hd-menu" role="menu" aria-label="Choose your distribution">
					{distributions.map((item, index) => (
						<a key={item.name} role="menuitem" href={distributionHref(state, index)}>
							<span>{item.name}</span>
							<small>{item.choice ? item.choice.label : 'Use CLIO in your browser'}</small>
						</a>
					))}
				</div>
			) : null}
		</div>
	);
}

/**
 * The hero's download button. It names the visitor's system and downloads the
 * installer directly; on Linux it asks for the distribution first.
 */
export default function HeroDownload({ icons }: { icons: PlatformIcons }) {
	const state = useRelease();
	const os = state.os;
	if (os === 'linux') return <LinuxMenu state={state} icon={icons.linux} />;
	if (os === null) {
		return (
			<a className="h-button h-button-primary hd-main" href="#download">
				<Download size={19} aria-hidden="true" />
				Download CLIO
			</a>
		);
	}
	return (
		<a className="h-button h-button-primary hd-main" href={primaryHref(state, os)}>
			<img src={icons[os]} alt="" width={20} height={20} />
			Download for {NAMES[os]}
		</a>
	);
}
