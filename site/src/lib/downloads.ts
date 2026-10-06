/**
 * Desktop download resolution against a GitHub release's asset list.
 *
 * Pure functions only: the browser island fetches the release and calls
 * these, and the unit tests feed them real asset names.
 *
 * Release assets come in two variants. A "bundled" installer embeds the CLIO
 * backend and runs on its own; its name carries `-bundled` just before the
 * extension. A "lite" installer is attach-only and connects to a CLIO backend
 * installed separately. Some platforms ship only lite builds (Intel macOS,
 * Windows on ARM, every AppImage).
 */

export type OS = 'windows' | 'macos' | 'linux';
export type Arch = 'x64' | 'arm64';
export type Variant = 'bundled' | 'lite';
export type Format = 'exe' | 'msi' | 'dmg' | 'deb' | 'rpm' | 'appimage';

/** The subset of a GitHub release asset the resolver reads. */
export interface ReleaseAsset {
	name: string;
	browser_download_url: string;
}

/** A resolved download: the asset URL and which variant it is. */
export interface PickedAsset {
	url: string;
	name: string;
	variant: Variant;
}

/** The page every download control falls back to when no asset matches. */
export const RELEASES_PAGE = 'https://github.com/iowarp/clio-agent/releases/latest';

/**
 * Desktop-app assets start with `CLIO.Desktop` (or `clio-desktop` for the
 * Windows-on-ARM executable). Restricting to this family keeps terminal-UI
 * binaries (`clio-tui-…`) and the agent executable from ever being offered
 * as a desktop installer.
 */
const DESKTOP_ASSET_RE = /^clio.desktop/i;

const EXTENSIONS: Record<Format, string> = {
	exe: '.exe',
	msi: '.msi',
	dmg: '.dmg',
	deb: '.deb',
	rpm: '.rpm',
	appimage: '.appimage',
};

const ARCH_TOKENS: Record<Arch, readonly string[]> = {
	arm64: ['aarch64', 'arm64'],
	x64: ['x86_64', 'amd64', 'x64'],
};

/** Whether an asset name is a bundled (self-contained) build. */
export function isBundled(name: string): boolean {
	return name.toLowerCase().includes('-bundled');
}

/** The CPU architecture an asset name targets, or null when it names none. */
export function archOf(name: string): Arch | null {
	const lower = name.toLowerCase();
	for (const arch of ['arm64', 'x64'] as const) {
		if (ARCH_TOKENS[arch].some((token) => lower.includes(token))) return arch;
	}
	return null;
}

/**
 * Pick the best desktop installer of `format` for `arch`.
 *
 * A bundled build is preferred over a lite one. An asset built for a
 * different architecture is never returned: an Apple Silicon disk image
 * handed to an Intel Mac does not run. Asset names that carry no
 * architecture are treated as universal.
 *
 * @returns the asset, or null when the release has nothing that fits.
 */
export function pickAsset(assets: readonly ReleaseAsset[], format: Format, arch: Arch): PickedAsset | null {
	const ext = EXTENSIONS[format];
	const fits = assets.filter((asset) => {
		const name = asset.name.toLowerCase();
		if (!DESKTOP_ASSET_RE.test(asset.name) || !name.endsWith(ext)) return false;
		const target = archOf(asset.name);
		return target === null || target === arch;
	});
	const pick = fits.find((a) => isBundled(a.name)) ?? fits[0];
	if (!pick) return null;
	return {
		url: pick.browser_download_url,
		name: pick.name,
		variant: isBundled(pick.name) ? 'bundled' : 'lite',
	};
}

/** One-line description of what a variant needs, shown under the button. */
export function variantNote(variant: Variant): string {
	return variant === 'bundled'
		? 'Includes the CLIO backend. Nothing else to install.'
		: 'Connects to a CLIO backend you install separately.';
}

/** Navigator fields the platform detection reads. */
export interface NavigatorHints {
	userAgent?: string;
	platform?: string;
	/** `navigator.userAgentData.architecture` where the browser exposes it. */
	architecture?: string;
}

/** The visitor's desktop OS, or null on mobile and unknown platforms. */
export function detectOS(hints: NavigatorHints): OS | null {
	const ua = `${hints.userAgent ?? ''} ${hints.platform ?? ''}`.toLowerCase();
	if (/iphone|ipad|ipod|android/.test(ua)) return null;
	if (/mac|darwin/.test(ua)) return 'macos';
	if (/win/.test(ua)) return 'windows';
	if (/linux|x11/.test(ua)) return 'linux';
	return null;
}

/**
 * The visitor's CPU architecture.
 *
 * Browsers rarely report it. Client hints are used when present; otherwise
 * a Mac is assumed to be Apple Silicon (Safari reports "Intel" on every Mac)
 * and everything else x64. The macOS card always offers both disk images, so
 * a wrong guess there costs one click.
 */
export function detectArch(os: OS | null, hints: NavigatorHints): Arch {
	const hint = (hints.architecture ?? '').toLowerCase();
	if (/arm|aarch/.test(hint)) return 'arm64';
	if (/x86|amd|x64/.test(hint)) return 'x64';
	const ua = `${hints.userAgent ?? ''} ${hints.platform ?? ''}`.toLowerCase();
	if (/aarch64|arm64|armv8/.test(ua)) return 'arm64';
	if (os === 'macos') return 'arm64';
	return 'x64';
}

/** One download control on a platform card. */
export interface DownloadChoice {
	format: Format;
	label: string;
	/** Fixed architecture for this control; when omitted the detected one is used. */
	arch?: Arch;
}

/** A platform card: the main download and the alternative formats. */
export interface PlatformDownloads {
	os: OS;
	name: string;
	summary: string;
	primary: DownloadChoice;
	others: readonly DownloadChoice[];
}

/** The three platform cards, in default order. */
export const PLATFORMS: readonly PlatformDownloads[] = [
	{
		os: 'windows',
		name: 'Windows',
		summary: 'Windows 10 and 11, 64-bit',
		primary: { format: 'exe', label: 'Download installer' },
		others: [{ format: 'msi', label: '.msi' }],
	},
	{
		os: 'macos',
		name: 'macOS',
		summary: 'Apple Silicon and Intel',
		primary: { format: 'dmg', label: 'Download for Apple Silicon', arch: 'arm64' },
		others: [{ format: 'dmg', label: 'Intel', arch: 'x64' }],
	},
	{
		os: 'linux',
		name: 'Linux',
		summary: 'Debian, Ubuntu, Fedora, and more',
		primary: { format: 'deb', label: 'Download .deb' },
		others: [
			{ format: 'rpm', label: '.rpm' },
			{ format: 'appimage', label: 'AppImage' },
		],
	},
];
