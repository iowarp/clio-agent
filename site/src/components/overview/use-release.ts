import { useEffect, useState } from 'react';
import { detectArch, detectOS, type Arch, type OS, type ReleaseAsset } from '@/lib/downloads';

const LATEST_RELEASE_API = 'https://api.github.com/repos/iowarp/clio-agent/releases/latest';

/** What the download islands know about the visitor and the latest release. */
export interface ReleaseState {
	status: 'loading' | 'ready' | 'unavailable';
	assets: readonly ReleaseAsset[];
	os: OS | null;
	arch: Arch;
}

let pending: Promise<readonly ReleaseAsset[]> | null = null;

/**
 * Fetch the latest release's assets once per page, shared by every island.
 *
 * Rejects when GitHub is unreachable or rate-limited; callers then keep the
 * server-rendered links, which point at the releases page.
 */
function fetchAssets(): Promise<readonly ReleaseAsset[]> {
	pending ??= fetch(LATEST_RELEASE_API, { headers: { Accept: 'application/vnd.github+json' } }).then(
		async (response) => {
			if (!response.ok) throw new Error(`GitHub release lookup failed: HTTP ${response.status}`);
			const body = (await response.json()) as { assets?: ReleaseAsset[] };
			return body.assets ?? [];
		},
	);
	return pending;
}

/**
 * Detect the visitor's platform and load the latest release's assets.
 *
 * A failed lookup is reported as `unavailable` and logged with its reason, so
 * a degraded download card is never silent.
 */
export function useRelease(): ReleaseState {
	const [state, setState] = useState<ReleaseState>({ status: 'loading', assets: [], os: null, arch: 'x64' });

	useEffect(() => {
		const nav = navigator as Navigator & { userAgentData?: { architecture?: string } };
		const hints = {
			userAgent: nav.userAgent,
			platform: nav.platform,
			architecture: nav.userAgentData?.architecture,
		};
		const os = detectOS(hints);
		const arch = detectArch(os, hints);
		let live = true;
		fetchAssets().then(
			(assets) => live && setState({ status: 'ready', assets, os, arch }),
			(error: unknown) => {
				console.warn('[clio-site] download links fall back to the releases page:', error);
				if (live) setState({ status: 'unavailable', assets: [], os, arch });
			},
		);
		return () => {
			live = false;
		};
	}, []);

	return state;
}
